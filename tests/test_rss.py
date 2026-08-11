from datetime import datetime, timezone

from lxml import etree

from bilibili import NoCompatibleAudioError
from models import FeedConfig, UserSource, VideoInfo, VideoPage
from rss import ITUNES_NS, PodcastService


class FakeBilibili:
    async def get_user_avatar(self, uid: int) -> str:
        assert uid == 193147738
        return "https://example.com/avatar.jpg"

    async def get_user_video_bvids(self, uid: int, limit: int) -> tuple[str, ...]:
        assert uid == 193147738
        assert limit == 100
        return ("BV1ab411c7mD", "BV1GJ411x7h7")

    async def get_video(self, bvid: str) -> VideoInfo:
        return VideoInfo(
            bvid=bvid,
            title="Example video",
            description="Description",
            owner="Author",
            image_url="https://example.com/cover.jpg",
            published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            pages=(
                VideoPage(cid=1001, page=1, title="Part one", duration=120),
                VideoPage(cid=1002, page=2, title="Part two", duration=180),
            ),
        )

    async def get_audio_length(self, bvid: str, cid: int) -> int:
        return cid * 10


async def test_builds_valid_rss_with_one_episode_per_page() -> None:
    feed = FeedConfig(
        slug="talks",
        title="Talks",
        description="Selected talks",
        users=(UserSource(uid=193147738, limit=2),),
        author="Example author",
    )
    service = PodcastService(FakeBilibili(), "https://podium.example.com")  # type: ignore[arg-type]

    document = etree.fromstring(await service.build_feed(feed))
    items = document.xpath("/rss/channel/item")

    assert len(items) == 4
    assert document.find(f"./channel/{{{ITUNES_NS}}}image").get("href") == (
        "https://example.com/avatar.jpg"
    )
    assert document.findtext("./channel/image/url") == "https://example.com/avatar.jpg"
    assert items[0].findtext("guid") == "bilibili:BV1ab411c7mD:1001"
    assert items[0].find(f"{{{ITUNES_NS}}}image").get("href") == (
        "https://example.com/cover.jpg"
    )
    assert items[0].find("guid").get("isPermaLink") == "false"
    assert items[0].find("enclosure").get("url") == (
        "https://podium.example.com/media/BV1ab411c7mD/1001.m4a"
    )
    assert items[0].find("enclosure").get("length") == "10010"
    assert items[0].findtext(f"{{{ITUNES_NS}}}duration") == "120"


class SkippingFakeBilibili:
    async def get_user_avatar(self, uid: int) -> str:
        return "https://example.com/avatar.jpg"

    async def get_user_video_bvids(self, uid: int, limit: int) -> tuple[str, ...]:
        assert limit == 100
        return ("BV1bad111111", "BV1good11111", "BV1good22222")

    async def get_video(self, bvid: str) -> VideoInfo:
        cid = {
            "BV1bad111111": 1,
            "BV1good11111": 2,
            "BV1good22222": 3,
        }[bvid]
        return VideoInfo(
            bvid=bvid,
            title=bvid,
            description="Description",
            owner="Author",
            image_url="https://example.com/cover.jpg",
            published_at=datetime(2024, 1, cid, tzinfo=timezone.utc),
            pages=(VideoPage(cid=cid, page=1, title="Part one", duration=120),),
        )

    async def get_audio_length(self, bvid: str, cid: int) -> int:
        if cid == 1:
            raise NoCompatibleAudioError("no compatible audio")
        return cid * 100


async def test_skips_incompatible_candidates_until_limit_is_filled() -> None:
    feed = FeedConfig(
        slug="talks",
        title="Talks",
        description="Selected talks",
        users=(UserSource(uid=193147738, limit=2),),
    )
    service = PodcastService(  # type: ignore[arg-type]
        SkippingFakeBilibili(), "https://podium.example.com"
    )

    document = etree.fromstring(await service.build_feed(feed))
    guids = [item.findtext("guid") for item in document.xpath("/rss/channel/item")]

    assert guids == [
        "bilibili:BV1good22222:3",
        "bilibili:BV1good11111:2",
    ]
