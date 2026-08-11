import httpx
from starlette.requests import Request

from media import MediaProxy
from models import AudioStream


class StaticAsyncStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"0123456789"


class FakeBilibili:
    media_headers = {
        "User-Agent": "Podium test",
        "Referer": "https://www.bilibili.com/",
    }

    def __init__(self) -> None:
        self.invalidations = 0

    async def get_audio_stream(self, bvid: str, cid: int) -> AudioStream:
        return AudioStream(
            url="https://cdn.example.com/audio.m4s",
            backup_urls=(),
            mime_type="audio/mp4",
            codecs="mp4a.40.2",
            bandwidth=132000,
        )

    async def get_audio_length(self, bvid: str, cid: int) -> int:
        return 12345678

    def invalidate_audio(self, bvid: str, cid: int) -> None:
        self.invalidations += 1


def make_request(method: str, range_header: str | None = None) -> Request:
    headers = []
    if range_header:
        headers.append((b"range", range_header.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/media/BV1ab411c7mD/1001.m4a",
            "headers": headers,
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 1234),
            "scheme": "http",
        }
    )


async def test_proxies_range_and_response_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=10-19"
        return httpx.Response(
            206,
            headers={
                "Content-Type": "audio/mp4",
                "Content-Range": "bytes 10-19/12345678",
                "Content-Length": "10",
                "Accept-Ranges": "bytes",
                "Connection": "keep-alive",
            },
            stream=StaticAsyncStream(),
        )

    fake = FakeBilibili()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await MediaProxy(fake, client).handle(  # type: ignore[arg-type]
            "BV1ab411c7mD",
            1001,
            make_request("GET", "bytes=10-19"),
        )
        content = b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 10-19/12345678"
    assert "connection" not in response.headers
    assert content == b"0123456789"


async def test_head_returns_metadata_without_opening_media_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HEAD should use cached metadata")

    fake = FakeBilibili()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await MediaProxy(fake, client).handle(  # type: ignore[arg-type]
            "BV1ab411c7mD",
            1001,
            make_request("HEAD"),
        )

    assert response.status_code == 200
    assert response.headers["content-length"] == "12345678"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.body == b""
