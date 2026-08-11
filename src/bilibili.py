from __future__ import annotations

import hashlib
import base64
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from cache import TTLCache
from models import AudioStream, VideoInfo, VideoPage


_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)
_WBI_MIXIN_ORDER = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
_WBI_FILTER = re.compile(r"[!'()*]")
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_DM_COVER_SOURCE = (
    "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) "
    "(0x0000C0XX)), SwiftShader driver)Google Inc. (Google)"
)
_DM_COVER = base64.b64encode(_DM_COVER_SOURCE.encode()).decode()[:-2]


class BilibiliError(RuntimeError):
    pass


class VideoNotFoundError(BilibiliError):
    pass


class NoCompatibleAudioError(BilibiliError):
    pass


def parse_bvid(value: str) -> str:
    match = _BVID_RE.search(value.strip())
    if match is None:
        raise ValueError(f"invalid Bilibili video identifier: {value}")
    bvid = match.group(1)
    return "BV" + bvid[2:]


class BilibiliClient:
    API_BASE = "https://api.bilibili.com"

    def __init__(
        self,
        client: httpx.AsyncClient,
        sessdata: str | None = None,
        cookie: str | None = None,
    ) -> None:
        self.client = client
        self.sessdata = sessdata
        self.cookie = cookie or (f"SESSDATA={sessdata}" if sessdata else None)
        self.video_cache: TTLCache[str, VideoInfo] = TTLCache()
        self.audio_cache: TTLCache[tuple[str, int], AudioStream] = TTLCache()
        self.length_cache: TTLCache[tuple[str, int], int] = TTLCache()
        self.user_video_cache: TTLCache[tuple[int, int], tuple[str, ...]] = TTLCache()
        self.space_cookie_cache: TTLCache[str, str] = TTLCache()
        self.wbi_cache: TTLCache[str, str] = TTLCache()

    def invalidate_audio(self, bvid: str, cid: int) -> None:
        self.audio_cache.delete((bvid, cid))

    async def get_video(self, bvid: str) -> VideoInfo:
        bvid = parse_bvid(bvid)
        return await self.video_cache.get_or_set(
            bvid,
            lambda: self._fetch_video(bvid),
            ttl=30 * 60,
        )

    async def get_audio_stream(self, bvid: str, cid: int) -> AudioStream:
        bvid = parse_bvid(bvid)
        key = (bvid, cid)
        return await self.audio_cache.get_or_set(
            key,
            lambda: self._fetch_audio_stream(bvid, cid),
            ttl=60 * 60,
        )

    async def get_audio_length(self, bvid: str, cid: int) -> int:
        bvid = parse_bvid(bvid)
        key = (bvid, cid)

        async def fetch() -> int:
            stream = await self.get_audio_stream(bvid, cid)
            return await self.probe_audio_length(stream)

        return await self.length_cache.get_or_set(key, fetch, ttl=24 * 60 * 60)

    async def get_user_video_bvids(self, uid: int, limit: int = 20) -> tuple[str, ...]:
        if uid <= 0:
            raise ValueError("Bilibili UID must be positive")
        if not 1 <= limit <= 100:
            raise ValueError("user video limit must be between 1 and 100")
        key = (uid, limit)
        return await self.user_video_cache.get_or_set(
            key,
            lambda: self._fetch_user_video_bvids(uid, limit),
            ttl=10 * 60,
        )

    async def _fetch_user_video_bvids(self, uid: int, limit: int) -> tuple[str, ...]:
        bvids: list[str] = []
        page_number = 1
        total: int | None = None

        while len(bvids) < limit:
            page_size = min(30, limit - len(bvids))
            params = await self._sign_wbi(
                {
                    "mid": uid,
                    "pn": page_number,
                    "ps": page_size,
                    "order": "pubdate",
                    "tid": 0,
                    "keyword": "",
                    "platform": "web",
                    "web_location": "1550101",
                    "order_avoided": "true",
                    "dm_img_list": "[]",
                    "dm_img_str": "",
                    "dm_cover_img_str": _DM_COVER,
                    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
                }
            )
            cookie = await self.space_cookie_cache.get_or_set(
                "space-cookie",
                self._fetch_space_cookie,
                ttl=24 * 60 * 60,
            )
            data = await self._api_get(
                "/x/space/wbi/arc/search",
                params,
                headers={
                    "Cookie": cookie,
                    "Origin": "https://space.bilibili.com",
                    "Referer": f"https://space.bilibili.com/{uid}/upload/video",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            try:
                page = data["page"]
                videos = data["list"]["vlist"]
                total = int(page["count"])
                if not isinstance(videos, list):
                    raise TypeError
            except (KeyError, TypeError, ValueError) as exc:
                raise BilibiliError("Bilibili returned malformed user videos") from exc

            page_bvids = []
            for video in videos:
                if isinstance(video, dict) and isinstance(video.get("bvid"), str):
                    try:
                        page_bvids.append(parse_bvid(video["bvid"]))
                    except ValueError:
                        continue
            bvids.extend(page_bvids)

            if not videos or len(bvids) >= total:
                break
            page_number += 1

        return tuple(dict.fromkeys(bvids))[:limit]

    async def _fetch_space_cookie(self) -> str:
        data = await self._api_get("/x/frontend/finger/spi", {})
        try:
            buvid3 = str(data["b_3"])
            buvid4 = str(data["b_4"])
        except (KeyError, TypeError) as exc:
            raise BilibiliError("Bilibili returned malformed browser identifiers") from exc
        parts = [self.cookie] if self.cookie else []
        cookie_names = {
            part.split("=", 1)[0].strip()
            for part in (self.cookie or "").split(";")
            if "=" in part
        }
        if "buvid3" not in cookie_names:
            parts.append(f"buvid3={buvid3}")
        if "buvid4" not in cookie_names:
            parts.append(f"buvid4={buvid4}")
        return "; ".join(parts)

    async def _fetch_video(self, bvid: str) -> VideoInfo:
        data = await self._api_get("/x/web-interface/view", {"bvid": bvid})
        try:
            pages = tuple(
                VideoPage(
                    cid=int(page["cid"]),
                    page=int(page["page"]),
                    title=str(page.get("part") or f"P{page['page']}"),
                    duration=int(page.get("duration") or 0),
                )
                for page in data["pages"]
            )
            owner = data.get("owner") or {}
            return VideoInfo(
                bvid=str(data["bvid"]),
                title=str(data["title"]),
                description=str(data.get("desc") or ""),
                owner=str(owner.get("name") or ""),
                image_url=str(data.get("pic") or ""),
                published_at=datetime.fromtimestamp(
                    int(data["pubdate"]), tz=timezone.utc
                ),
                pages=pages,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BilibiliError("Bilibili returned malformed video metadata") from exc

    async def _fetch_audio_stream(self, bvid: str, cid: int) -> AudioStream:
        params: dict[str, Any] = {
            "bvid": bvid,
            "cid": cid,
            "fnval": 16,
            "fnver": 0,
            "fourk": 1,
            "gaia_source": "view-card",
        }
        signed = await self._sign_wbi(params)
        data = await self._api_get("/x/player/wbi/playurl", signed)
        dash = data.get("dash") or {}
        candidates = []
        for item in dash.get("audio") or []:
            mime_type = item.get("mimeType") or item.get("mime_type") or ""
            codecs = str(item.get("codecs") or "")
            if mime_type == "audio/mp4" and codecs.startswith("mp4a"):
                candidates.append(item)

        if not candidates:
            raise NoCompatibleAudioError(
                f"no compatible AAC audio stream for {bvid}/{cid}"
            )

        item = max(candidates, key=lambda value: int(value.get("bandwidth") or 0))
        url = item.get("baseUrl") or item.get("base_url")
        backup_urls = item.get("backupUrl") or item.get("backup_url") or []
        if not isinstance(url, str) or not url:
            raise BilibiliError("Bilibili returned an audio stream without a URL")

        return AudioStream(
            url=url,
            backup_urls=tuple(str(value) for value in backup_urls if value),
            mime_type=str(item.get("mimeType") or item.get("mime_type")),
            codecs=str(item.get("codecs")),
            bandwidth=int(item.get("bandwidth") or 0),
        )

    async def probe_audio_length(self, stream: AudioStream) -> int:
        last_error: Exception | None = None
        for url in stream.urls:
            request = self.client.build_request(
                "GET",
                url,
                headers={**self.media_headers, "Range": "bytes=0-0"},
            )
            response: httpx.Response | None = None
            try:
                response = await self.client.send(request, stream=True)
                if response.status_code not in (200, 206):
                    raise BilibiliError(
                        f"audio length probe returned HTTP {response.status_code}"
                    )
                content_range = response.headers.get("content-range", "")
                if "/" in content_range:
                    total = content_range.rsplit("/", 1)[1]
                    if total.isdigit():
                        return int(total)
                content_length = response.headers.get("content-length")
                if response.status_code == 200 and content_length and content_length.isdigit():
                    return int(content_length)
                raise BilibiliError("audio server did not report the media length")
            except (httpx.HTTPError, BilibiliError) as exc:
                last_error = exc
            finally:
                if response is not None:
                    await response.aclose()
        raise BilibiliError("unable to determine audio stream length") from last_error

    async def _sign_wbi(self, params: dict[str, Any]) -> dict[str, Any]:
        mixin_key = await self.wbi_cache.get_or_set(
            "mixin-key",
            self._fetch_wbi_mixin_key,
            ttl=6 * 60 * 60,
        )
        signed = dict(params)
        signed["wts"] = int(time.time())
        cleaned = {
            key: _WBI_FILTER.sub("", str(value))
            for key, value in sorted(signed.items())
        }
        query = urlencode(cleaned)
        signed["w_rid"] = hashlib.md5(
            f"{query}{mixin_key}".encode(), usedforsecurity=False
        ).hexdigest()
        return signed

    async def _fetch_wbi_mixin_key(self) -> str:
        data = await self._api_get(
            "/x/web-interface/nav",
            {},
            accepted_codes=(0, -101),
        )
        try:
            wbi_img = data["wbi_img"]
            img_key = urlparse(wbi_img["img_url"]).path.rsplit("/", 1)[-1].split(".")[0]
            sub_key = urlparse(wbi_img["sub_url"]).path.rsplit("/", 1)[-1].split(".")[0]
            source = img_key + sub_key
            return "".join(source[index] for index in _WBI_MIXIN_ORDER)[:32]
        except (KeyError, IndexError, TypeError) as exc:
            raise BilibiliError("Bilibili returned malformed Wbi keys") from exc

    async def _api_get(
        self,
        path: str,
        params: dict[str, Any],
        accepted_codes: tuple[int, ...] = (0,),
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self.client.get(
                f"{self.API_BASE}{path}",
                params=params,
                headers={**self.api_headers, **(headers or {})},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            if path == "/x/space/wbi/arc/search" and exc.response.status_code == 412:
                raise BilibiliError(
                    "Bilibili blocked the user-space request; configure or refresh "
                    "BILIBILI_COOKIE"
                ) from exc
            raise BilibiliError("Bilibili API request failed") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliError("Bilibili API request failed") from exc

        if not isinstance(payload, dict):
            raise BilibiliError("Bilibili returned an invalid API response")
        code = payload.get("code")
        if code not in accepted_codes:
            message = str(payload.get("message") or "unknown error")
            if path == "/x/space/wbi/arc/search" and code in (-352, -412):
                raise BilibiliError(
                    "Bilibili blocked the user-space request; configure or refresh "
                    "BILIBILI_COOKIE"
                )
            if code == -404:
                raise VideoNotFoundError(message)
            raise BilibiliError(f"Bilibili API error {code}: {message}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BilibiliError("Bilibili API response has no data object")
        return data

    @property
    def api_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": _BROWSER_USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    @property
    def media_headers(self) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 Podium/0.1",
            "Referer": "https://www.bilibili.com/",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
