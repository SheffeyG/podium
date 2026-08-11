from __future__ import annotations

import asyncio
import logging
from email.utils import format_datetime

from lxml import etree

from bilibili import BilibiliClient, NoCompatibleAudioError
from database import FeedStateStore
from models import Episode, FeedConfig


logger = logging.getLogger(__name__)


ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"


class PodcastService:
    def __init__(
        self,
        bilibili: BilibiliClient,
        base_url: str,
        store: FeedStateStore,
    ) -> None:
        self.bilibili = bilibili
        self.base_url = base_url.rstrip("/")
        self.store = store
        self.feed_locks: dict[str, asyncio.Lock] = {}

    async def build_feed(self, feed: FeedConfig) -> bytes:
        lock = self.feed_locks.setdefault(feed.slug, asyncio.Lock())
        async with lock:
            return await self._build_feed(feed)

    async def _build_feed(self, feed: FeedConfig) -> bytes:
        episodes: list[Episode] = []
        feed_image = await self.bilibili.get_user_avatar(feed.users[0].uid)
        for user in feed.users:
            known_bvids = self.store.known_bvids(user.uid)
            initializing = not known_bvids
            accepted_videos = 0
            candidates = await self.bilibili.get_new_user_video_bvids(
                user.uid,
                known_bvids,
                scan_limit=100,
            )
            for bvid in candidates:
                video = await self.bilibili.get_video(bvid)
                multiple_pages = len(video.pages) > 1
                video_episodes: list[Episode] = []
                for page in video.pages:
                    try:
                        length = await self.bilibili.get_audio_length(
                            video.bvid, page.cid
                        )
                    except NoCompatibleAudioError:
                        logger.warning(
                            "skip %s/%s: no compatible AAC audio available",
                            video.bvid,
                            page.cid,
                        )
                        continue
                    title = (
                        f"{video.title} - {page.title}"
                        if multiple_pages
                        else video.title
                    )
                    video_episodes.append(
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

                self.store.save_video(user.uid, bvid, video_episodes)
                if video_episodes:
                    accepted_videos += 1
                    if initializing and accepted_videos >= user.limit:
                        break

            stored_episodes = self.store.episodes_for_uid(
                user.uid,
                user.limit,
                self.base_url,
            )
            episodes.extend(stored_episodes)
            stored_videos = len({episode.bvid for episode in stored_episodes})
            if stored_videos < user.limit:
                logger.warning(
                    "feed %s only found %s/%s compatible videos for UID %s",
                    feed.slug,
                    stored_videos,
                    user.limit,
                    user.uid,
                )

        unique_episodes = {episode.guid: episode for episode in episodes}
        sorted_episodes = sorted(
            unique_episodes.values(),
            key=lambda episode: episode.published_at,
            reverse=True,
        )
        return self._render(feed, sorted_episodes, feed_image)

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
            image = etree.SubElement(channel, "image")
            self._text(image, "url", feed_image)
            self._text(image, "title", feed.title)
            self._text(image, "link", self.base_url)

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
