from __future__ import annotations

from email.utils import format_datetime

from lxml import etree

from bilibili import BilibiliClient
from cache import TTLCache
from models import Episode, FeedConfig


ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"


class PodcastService:
    def __init__(self, bilibili: BilibiliClient, base_url: str) -> None:
        self.bilibili = bilibili
        self.base_url = base_url.rstrip("/")
        self.feed_cache: TTLCache[str, bytes] = TTLCache()

    async def build_feed(self, feed: FeedConfig) -> bytes:
        return await self.feed_cache.get_or_set(
            feed.slug,
            lambda: self._build_feed(feed),
            ttl=5 * 60,
        )

    async def _build_feed(self, feed: FeedConfig) -> bytes:
        episodes: list[Episode] = []
        feed_image = ""
        bvids: list[str] = []
        for user in feed.users:
            bvids.extend(
                await self.bilibili.get_user_video_bvids(user.uid, user.limit)
            )

        for bvid in dict.fromkeys(bvids):
            video = await self.bilibili.get_video(bvid)
            feed_image = feed_image or video.image_url
            multiple_pages = len(video.pages) > 1
            for page in video.pages:
                length = await self.bilibili.get_audio_length(video.bvid, page.cid)
                title = f"{video.title} - {page.title}" if multiple_pages else video.title
                episodes.append(
                    Episode(
                        bvid=video.bvid,
                        cid=page.cid,
                        title=title,
                        description=video.description,
                        published_at=video.published_at,
                        duration=page.duration,
                        image_url=video.image_url,
                        media_url=(
                            f"{self.base_url}/media/{video.bvid}/{page.cid}.m4a"
                        ),
                        media_length=length,
                    )
                )

        episodes.sort(key=lambda episode: episode.published_at, reverse=True)
        return self._render(feed, episodes, feed_image)

    def _render(
        self,
        feed: FeedConfig,
        episodes: list[Episode],
        feed_image: str,
    ) -> bytes:
        rss = etree.Element(
            "rss",
            version="2.0",
            nsmap={"itunes": ITUNES_NS, "atom": ATOM_NS},
        )
        channel = etree.SubElement(rss, "channel")
        self._text(channel, "title", feed.title)
        self._text(channel, "link", self.base_url)
        self._text(channel, "description", feed.description)
        self._text(channel, "language", feed.language)
        self._text(channel, f"{{{ITUNES_NS}}}author", feed.author)
        self._text(channel, f"{{{ITUNES_NS}}}explicit", "false")
        etree.SubElement(
            channel,
            f"{{{ATOM_NS}}}link",
            href=f"{self.base_url}/feeds/{feed.slug}.xml",
            rel="self",
            type="application/rss+xml",
        )
        if feed_image:
            etree.SubElement(channel, f"{{{ITUNES_NS}}}image", href=feed_image)

        for episode in episodes:
            item = etree.SubElement(channel, "item")
            self._text(item, "title", episode.title)
            guid = self._text(item, "guid", episode.guid)
            guid.set("isPermaLink", "false")
            self._text(item, "pubDate", format_datetime(episode.published_at))
            self._text(item, "description", episode.description or episode.title)
            self._text(item, f"{{{ITUNES_NS}}}duration", str(episode.duration))
            self._text(item, f"{{{ITUNES_NS}}}explicit", "false")
            if episode.image_url:
                etree.SubElement(
                    item,
                    f"{{{ITUNES_NS}}}image",
                    href=episode.image_url,
                )
            etree.SubElement(
                item,
                "enclosure",
                url=episode.media_url,
                length=str(episode.media_length),
                type=episode.media_type,
            )

        return etree.tostring(
            rss,
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
        )

    @staticmethod
    def _text(parent: etree._Element, tag: str, value: str) -> etree._Element:
        element = etree.SubElement(parent, tag)
        element.text = value
        return element
