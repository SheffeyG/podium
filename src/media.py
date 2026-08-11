from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from bilibili import BilibiliClient, BilibiliError
from models import AudioStream


_PASSTHROUGH_HEADERS = {
    "accept-ranges",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
}
_EXPIRED_STATUSES = {401, 403, 404}


class MediaProxy:
    def __init__(self, bilibili: BilibiliClient, client: httpx.AsyncClient) -> None:
        self.bilibili = bilibili
        self.client = client

    async def handle(
        self,
        bvid: str,
        cid: int,
        request: Request,
    ) -> Response:
        if request.method == "HEAD":
            stream = await self.bilibili.get_audio_stream(bvid, cid)
            length = await self.bilibili.get_audio_length(bvid, cid)
            return Response(
                status_code=200,
                headers={
                    "Content-Type": stream.mime_type,
                    "Content-Length": str(length),
                    "Accept-Ranges": "bytes",
                },
            )

        range_header = request.headers.get("range")
        upstream = await self._open_with_refresh(bvid, cid, range_header)
        headers = {
            name.lower(): value
            for name, value in upstream.headers.items()
            if name.lower() in _PASSTHROUGH_HEADERS
        }
        headers["content-type"] = "audio/mp4"
        headers.setdefault("accept-ranges", "bytes")

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            headers=headers,
            media_type=None,
        )

    async def _open_with_refresh(
        self,
        bvid: str,
        cid: int,
        range_header: str | None,
    ) -> httpx.Response:
        stream = await self.bilibili.get_audio_stream(bvid, cid)
        response, last_status = await self._open_stream(stream, range_header)
        if response is not None:
            return response

        if last_status in _EXPIRED_STATUSES:
            self.bilibili.invalidate_audio(bvid, cid)
            stream = await self.bilibili.get_audio_stream(bvid, cid)
            response, last_status = await self._open_stream(stream, range_header)
            if response is not None:
                return response

        if last_status == 416:
            raise HTTPException(status_code=416, detail="requested range is not satisfiable")
        raise BilibiliError(
            f"all Bilibili audio endpoints failed with HTTP {last_status or 'network error'}"
        )

    async def _open_stream(
        self,
        stream: AudioStream,
        range_header: str | None,
    ) -> tuple[httpx.Response | None, int | None]:
        headers = dict(self.bilibili.media_headers)
        if range_header:
            headers["Range"] = range_header

        last_status: int | None = None
        for url in stream.urls:
            response: httpx.Response | None = None
            try:
                request = self.client.build_request("GET", url, headers=headers)
                response = await self.client.send(request, stream=True)
                last_status = response.status_code
                if response.status_code in (200, 206):
                    return response, response.status_code
            except httpx.HTTPError:
                pass
            finally:
                if response is not None and response.status_code not in (200, 206):
                    await response.aclose()

        return None, last_status
