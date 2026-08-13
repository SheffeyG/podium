from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx

from .cache import TTLCache


SEGMENT_CACHE_TTL = 6 * 60 * 60


class SponsorBlockError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SkipSegment:
    start: float
    end: float
    category: str
    uuid: str


class SponsorBlockClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        server_url: str,
        categories: tuple[str, ...],
    ) -> None:
        self.client = client
        self.server_url = server_url.rstrip("/")
        self.categories = categories
        self.cache: TTLCache[tuple[str, int], tuple[SkipSegment, ...]] = TTLCache()

    async def get_segments(self, bvid: str, cid: int) -> tuple[SkipSegment, ...]:
        key = (bvid, cid)
        return await self.cache.get_or_set(
            key,
            lambda: self._fetch_segments(bvid, cid),
            ttl=SEGMENT_CACHE_TTL,
        )

    async def _fetch_segments(
        self, bvid: str, cid: int
    ) -> tuple[SkipSegment, ...]:
        try:
            response = await self.client.get(
                f"{self.server_url}/api/skipSegments",
                params={
                    "videoID": bvid,
                    "cid": str(cid),
                    "categories": json.dumps(self.categories, separators=(",", ":")),
                    "actionTypes": '["skip"]',
                },
                headers={
                    "Origin": (
                        "chrome-extension://eaoelafamejbnggahofapllmfhlhajdd"
                    ),
                    "X-Ext-Version": "0.5.0",
                    "User-Agent": "Podium/0.1",
                },
            )
            if response.status_code == 404:
                return ()
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SponsorBlockError("SponsorBlock request failed") from exc

        if not isinstance(payload, list):
            raise SponsorBlockError("SponsorBlock returned an invalid response")

        segments: list[SkipSegment] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if str(item.get("cid")) != str(cid):
                continue
            if item.get("actionType") != "skip":
                continue
            segment = item.get("segment")
            if (
                not isinstance(segment, list)
                or len(segment) != 2
                or not all(isinstance(value, (int, float)) for value in segment)
            ):
                continue
            start, end = float(segment[0]), float(segment[1])
            if start < 0 or end <= start:
                continue
            segments.append(
                SkipSegment(
                    start=start,
                    end=end,
                    category=str(item.get("category") or "unknown"),
                    uuid=str(item.get("UUID") or ""),
                )
            )
        return tuple(sorted(segments, key=lambda value: (value.start, value.end)))


def normalize_segments(
    segments: tuple[SkipSegment, ...], duration: float
) -> tuple[tuple[float, float], ...]:
    merged: list[tuple[float, float]] = []
    for segment in segments:
        start = min(max(segment.start, 0.0), duration)
        end = min(max(segment.end, 0.0), duration)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def segment_hash(bvid: str, cid: int, segments: tuple[tuple[float, float], ...]) -> str:
    payload = json.dumps(
        [bvid, cid, [[round(start, 3), round(end, 3)] for start, end in segments]],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
