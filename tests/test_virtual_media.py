import struct

import httpx
from starlette.requests import Request

from models import AudioStream
from mp4 import build_manifest, patch_fragment
from virtual_media import VirtualMediaProxy


def box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def full_box(box_type: bytes, version: int, payload: bytes) -> bytes:
    return box(box_type, bytes((version, 0, 0, 0)) + payload)


def fragment(decode_time: int, sequence: int) -> bytes:
    mfhd = full_box(b"mfhd", 0, struct.pack(">I", sequence))
    tfdt = full_box(b"tfdt", 1, struct.pack(">Q", decode_time))
    return box(b"moof", mfhd + box(b"traf", tfdt)) + box(
        b"mdat", bytes([sequence]) * 20
    )


def source_file() -> tuple[bytes, tuple[bytes, ...]]:
    fragments = tuple(fragment(index * 1000, index + 1) for index in range(3))
    references = b"".join(
        struct.pack(">III", len(item), 1000, 1 << 31) for item in fragments
    )
    sidx_payload = (
        struct.pack(">II", 1, 1000)
        + struct.pack(">QQ", 0, 0)
        + struct.pack(">HH", 0, len(fragments))
        + references
    )
    prefix = (
        box(b"ftyp", b"isom")
        + box(b"moov", b"")
        + full_box(b"sidx", 1, sidx_payload)
    )
    return prefix + b"".join(fragments), fragments


class StaticStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self):
        yield self.data


class FakeBilibili:
    media_headers = {"Accept-Encoding": "identity"}

    async def get_audio_stream(self, bvid: str, cid: int) -> AudioStream:
        return AudioStream(
            url="https://cdn.example.com/audio.m4s",
            backup_urls=(),
            mime_type="audio/mp4",
            codecs="mp4a.40.2",
            bandwidth=128000,
        )

    def invalidate_audio(self, bvid: str, cid: int) -> None:
        raise AssertionError("audio URL should not be invalidated")


class FakeStore:
    def __init__(self, manifest) -> None:
        self.manifest = manifest

    def get_manifest(self, manifest_id: str, bvid: str, cid: int):
        assert (manifest_id, bvid, cid) == ("manifest", "BV1ab411c7mD", 1001)
        return self.manifest


def request(method: str = "GET", range_header: str | None = None) -> Request:
    headers = [] if range_header is None else [(b"range", range_header.encode())]
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/media/test",
            "headers": headers,
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 1234),
            "scheme": "http",
        }
    )


async def test_virtual_proxy_removes_fragment_and_serves_ranges() -> None:
    source, fragments = source_file()
    prefix_length = len(source) - sum(len(item) for item in fragments)
    manifest = build_manifest(source[:prefix_length], ((1.1, 1.9),))
    upstream_requests: list[str] = []

    def handler(upstream_request: httpx.Request) -> httpx.Response:
        range_value = upstream_request.headers["range"]
        upstream_requests.append(range_value)
        start, end = (int(value) for value in range_value[6:].split("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{end}/{len(source)}"},
            stream=StaticStream(source[start : end + 1]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        proxy = VirtualMediaProxy(FakeBilibili(), http, FakeStore(manifest))
        response = await proxy.handle(
            "BV1ab411c7mD", 1001, "manifest", request()
        )
        content = b"".join([chunk async for chunk in response.body_iterator])

    expected = (
        manifest.prefix
        + patch_fragment(
            fragments[0], 0, 1, source_timescale=manifest.timescale
        )
        + patch_fragment(
            fragments[2], 1000, 2, source_timescale=manifest.timescale
        )
    )
    assert response.status_code == 200
    assert content == expected
    assert len(content) == manifest.output_length
    assert len(upstream_requests) == 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        proxy = VirtualMediaProxy(FakeBilibili(), http, FakeStore(manifest))
        partial = await proxy.handle(
            "BV1ab411c7mD",
            1001,
            "manifest",
            request(range_header="bytes=10-99"),
        )
        partial_content = b"".join(
            [chunk async for chunk in partial.body_iterator]
        )

    assert partial.status_code == 206
    assert partial.headers["content-range"] == (
        f"bytes 10-99/{manifest.output_length}"
    )
    assert partial_content == expected[10:100]
