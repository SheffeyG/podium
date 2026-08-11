import json
from pathlib import Path

import httpx
import pytest

from bilibili import BilibiliClient, parse_bvid


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_bvid_from_identifier_and_url() -> None:
    assert parse_bvid("BV1ab411c7mD") == "BV1ab411c7mD"
    assert (
        parse_bvid("https://www.bilibili.com/video/BV1ab411c7mD/?p=2")
        == "BV1ab411c7mD"
    )
    with pytest.raises(ValueError):
        parse_bvid("https://example.com/not-a-video")


async def test_fetches_video_and_selects_highest_compatible_aac() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json=_load_fixture("video_view.json"))
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(
                200,
                json={
                    "code": -101,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd0849416f884e2e5c95fba0415e29b.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"
                        }
                    }
                },
            )
        if request.url.path == "/x/player/wbi/playurl":
            assert request.url.params.get("w_rid")
            assert request.url.params["fnval"] == "16"
            assert request.url.params["gaia_source"] == "view-card"
            return httpx.Response(200, json=_load_fixture("playurl.json"))
        if request.url.path == "/x/frontend/finger/spi":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"b_3": "test-buvid3", "b_4": "test-buvid4"},
                },
            )
        if request.url.path == "/x/space/wbi/arc/search":
            assert request.url.params["mid"] == "193147738"
            assert request.url.params["order"] == "pubdate"
            assert request.url.params["platform"] == "web"
            assert request.url.params["dm_img_list"] == "[]"
            assert request.url.params.get("w_rid")
            assert "buvid3=test-buvid3" in request.headers["cookie"]
            assert "buvid4=test-buvid4" in request.headers["cookie"]
            assert "DedeUserID=123" in request.headers["cookie"]
            return httpx.Response(200, json=_load_fixture("user_videos.json"))
        if request.url.host in {"cdn.example.com", "backup.example.com"}:
            assert request.headers["range"] == "bytes=0-0"
            assert "cookie" not in request.headers
            return httpx.Response(
                206,
                headers={
                    "Content-Range": "bytes 0-0/12345678",
                    "Content-Length": "1",
                    "Content-Type": "audio/mp4",
                },
                content=b"x",
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bilibili = BilibiliClient(
            client,
            sessdata="secret",
            cookie="SESSDATA=secret; DedeUserID=123",
        )
        video = await bilibili.get_video("BV1ab411c7mD")
        user_bvids = await bilibili.get_user_video_bvids(193147738, limit=2)
        stream = await bilibili.get_audio_stream(video.bvid, video.pages[0].cid)
        length = await bilibili.get_audio_length(video.bvid, video.pages[0].cid)

    assert video.title == "Example video"
    assert len(video.pages) == 2
    assert user_bvids == ("BV1ab411c7mD", "BV1GJ411x7h7")
    assert stream.bandwidth == 132000
    assert stream.codecs == "mp4a.40.2"
    assert length == 12345678
    assert any("SESSDATA=secret" in request.headers.get("cookie", "") for request in requests)
