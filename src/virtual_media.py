from __future__ import annotations

import logging
import struct
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from bilibili import BilibiliClient
from models import Episode
from mp4 import (
    Mp4Error,
    build_manifest,
    parse_sidx,
    parse_top_level_boxes,
    patch_fragment,
)
from protocols import ManifestStore
from sponsorblock import (
    SponsorBlockClient,
    SponsorBlockError,
    normalize_segments,
    segment_hash,
)


logger = logging.getLogger(__name__)

INITIAL_PREFIX_SIZE = 128 * 1024
MAX_PREFIX_SIZE = 1024 * 1024


class VirtualMediaError(RuntimeError):
    pass


class VirtualMediaService:
    def __init__(
        self,
        sponsorblock: SponsorBlockClient,
        bilibili: BilibiliClient,
        client: httpx.AsyncClient,
        store: ManifestStore,
        base_url: str,
    ) -> None:
        self.sponsorblock = sponsorblock
        self.bilibili = bilibili
        self.client = client
        self.store = store
        self.base_url = base_url.rstrip("/")

    async def edit_episode(self, episode: Episode) -> Episode:
        try:
            segments = await self.sponsorblock.get_segments(
                episode.bvid, episode.cid
            )
            normalized = normalize_segments(segments, float(episode.duration))
            if not normalized:
                return episode

            manifest_id = segment_hash(episode.bvid, episode.cid, normalized)
            manifest = self.store.get_manifest(
                manifest_id, episode.bvid, episode.cid
            )
            if manifest is None:
                source_prefix = await self._fetch_source_prefix(
                    episode.bvid, episode.cid
                )
                manifest = build_manifest(source_prefix, normalized)
                if len(manifest.fragments) == len(parse_sidx(source_prefix).references):
                    return episode
                self.store.save_manifest(
                    manifest_id,
                    episode.bvid,
                    episode.cid,
                    manifest,
                )

            return replace(
                episode,
                duration=max(0, round(manifest.output_duration)),
                media_url=(
                    f"{self.base_url}/media/{episode.bvid}/{episode.cid}/"
                    f"{manifest_id}.m4a"
                ),
                media_length=manifest.output_length,
            )
        except (SponsorBlockError, Mp4Error, VirtualMediaError, httpx.HTTPError) as exc:
            logger.warning(
                "use original audio for %s/%s: %s",
                episode.bvid,
                episode.cid,
                exc,
            )
            return episode

    async def _fetch_source_prefix(self, bvid: str, cid: int) -> bytes:
        stream = await self.bilibili.get_audio_stream(bvid, cid)
        size = INITIAL_PREFIX_SIZE
        while size <= MAX_PREFIX_SIZE:
            data = await self._fetch_range(stream.urls, 0, size - 1)
            prefix = _extract_source_prefix(data)
            if prefix is not None:
                return prefix
            size *= 2
        raise VirtualMediaError("fMP4 init prefix exceeds the supported size")

    async def _fetch_range(
        self, urls: tuple[str, ...], start: int, end: int
    ) -> bytes:
        last_status: int | None = None
        for url in urls:
            try:
                response = await self.client.get(
                    url,
                    headers={
                        **self.bilibili.media_headers,
                        "Range": f"bytes={start}-{end}",
                    },
                )
            except httpx.HTTPError:
                continue
            last_status = response.status_code
            if response.status_code == 206:
                return response.content
        raise VirtualMediaError(
            f"audio prefix request failed with HTTP {last_status or 'network error'}"
        )


class VirtualMediaProxy:
    def __init__(
        self,
        bilibili: BilibiliClient,
        client: httpx.AsyncClient,
        store: ManifestStore,
    ) -> None:
        self.bilibili = bilibili
        self.client = client
        self.store = store

    async def handle(
        self,
        bvid: str,
        cid: int,
        manifest_id: str,
        request: Request,
    ) -> Response:
        manifest = self.store.get_manifest(manifest_id, bvid, cid)
        if manifest is None:
            raise HTTPException(status_code=404, detail="media manifest not found")

        total = manifest.output_length
        byte_range = _parse_range(request.headers.get("range"), total)
        if byte_range is None:
            start, end, partial = 0, total - 1, False
        else:
            start, end = byte_range
            partial = True

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "audio/mp4",
            "Content-Length": str(end - start + 1),
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        status_code = 206 if partial else 200

        if request.method == "HEAD":
            return Response(status_code=status_code, headers=headers)

        relevant = [
            (index, fragment)
            for index, fragment in enumerate(manifest.fragments)
            if fragment.output_start <= end
            and fragment.output_start + fragment.size - 1 >= start
        ]
        prefix_start = max(start, 0)
        prefix_end = min(end + 1, len(manifest.prefix))
        prefix_data = (
            manifest.prefix[prefix_start:prefix_end]
            if prefix_start < prefix_end
            else b""
        )

        if not relevant:
            return Response(
                content=prefix_data,
                status_code=status_code,
                headers=headers,
            )

        first_fragment = relevant[0][1]
        last_fragment = relevant[-1][1]
        upstream = await self._open_upstream(
            bvid,
            cid,
            first_fragment.source_start,
            last_fragment.source_start + last_fragment.size - 1,
        )

        async def body() -> AsyncIterator[bytes]:
            try:
                if prefix_data:
                    yield prefix_data
                reader = _AsyncByteReader(upstream.aiter_raw())
                source_position = first_fragment.source_start
                for sequence, fragment in relevant:
                    gap = fragment.source_start - source_position
                    if gap > 0:
                        await reader.discard(gap)
                    fragment_data = await reader.read_exact(fragment.size)
                    source_position = fragment.source_start + fragment.size
                    patched = patch_fragment(
                        fragment_data,
                        fragment.new_decode_time,
                        sequence + 1,
                        source_timescale=manifest.timescale,
                    )
                    local_start = max(start - fragment.output_start, 0)
                    local_end = min(end - fragment.output_start + 1, fragment.size)
                    if local_start < local_end:
                        yield patched[local_start:local_end]
            finally:
                await upstream.aclose()

        return StreamingResponse(
            body(),
            status_code=status_code,
            headers=headers,
            media_type=None,
        )

    async def _open_upstream(
        self,
        bvid: str,
        cid: int,
        start: int,
        end: int,
    ) -> httpx.Response:
        for attempt in range(2):
            stream = await self.bilibili.get_audio_stream(bvid, cid)
            last_status: int | None = None
            for url in stream.urls:
                response: httpx.Response | None = None
                try:
                    upstream_request = self.client.build_request(
                        "GET",
                        url,
                        headers={
                            **self.bilibili.media_headers,
                            "Range": f"bytes={start}-{end}",
                        },
                    )
                    response = await self.client.send(upstream_request, stream=True)
                    last_status = response.status_code
                    if response.status_code == 206:
                        return response
                except httpx.HTTPError:
                    pass
                finally:
                    if response is not None and response.status_code != 206:
                        await response.aclose()
            if attempt == 0 and last_status in {401, 403, 404}:
                self.bilibili.invalidate_audio(bvid, cid)
                continue
            break
        raise VirtualMediaError(
            f"virtual audio source failed with HTTP {last_status or 'network error'}"
        )


class _AsyncByteReader:
    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self.chunks = chunks
        self.buffer = bytearray()

    async def read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            try:
                self.buffer.extend(await anext(self.chunks))
            except StopAsyncIteration as exc:
                raise VirtualMediaError("audio source ended before the fragment") from exc
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    async def discard(self, size: int) -> None:
        remaining = size
        while remaining:
            chunk = await self.read_exact(min(remaining, 64 * 1024))
            remaining -= len(chunk)


def _extract_source_prefix(data: bytes) -> bytes | None:
    offset = 0
    while offset + 8 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        if size == 1:
            if offset + 16 > len(data):
                return None
            size = struct.unpack_from(">Q", data, offset + 8)[0]
        elif size == 0:
            return None
        if size < 8 or offset + size > len(data):
            return None
        box_type = data[offset + 4 : offset + 8]
        end = offset + size
        if box_type == b"sidx":
            sidx_data = data[:end]
            sidx = parse_sidx(sidx_data)
            prefix_end = end + sidx.first_offset
            if prefix_end > len(data):
                return None
            prefix = data[:prefix_end]
            parse_top_level_boxes(prefix)
            return prefix
        offset = end
    return None


def _parse_range(value: str | None, total: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise HTTPException(status_code=416, detail="invalid byte range")
    raw_start, separator, raw_end = value[6:].partition("-")
    if not separator:
        raise HTTPException(status_code=416, detail="invalid byte range")
    try:
        if raw_start:
            start = int(raw_start)
            end = int(raw_end) if raw_end else total - 1
        else:
            suffix = int(raw_end)
            if suffix <= 0:
                raise ValueError
            start = max(total - suffix, 0)
            end = total - 1
    except ValueError as exc:
        raise HTTPException(status_code=416, detail="invalid byte range") from exc
    if start < 0 or start >= total or end < start:
        raise HTTPException(
            status_code=416,
            detail="requested range is not satisfiable",
            headers={"Content-Range": f"bytes */{total}"},
        )
    return start, min(end, total - 1)
