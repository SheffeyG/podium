from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from typing import Protocol

from lxml import etree

from .bilibili import NoCompatibleAudioError, VideoInfo
from .config import FeedConfig


@dataclass(frozen=True, slots=True)
class Episode:
    bvid: str
    cid: int
    title: str
    description: str
    published_at: datetime
    duration: int
    image_url: str
    media_url: str
    media_length: int
    media_type: str = "audio/mp4"

    @property
    def guid(self) -> str:
        return f"bilibili:{self.bvid}:{self.cid}"


@dataclass(frozen=True, slots=True)
class StoredEpisode:
    bvid: str
    cid: int
    title: str
    description: str
    published_at: datetime
    duration: int
    image_url: str
    media_length: int


@dataclass(frozen=True, slots=True)
class FeedSnapshot:
    episodes: tuple[Episode, ...]
    image_url: str


class VideoSource(Protocol):
    async def get_user_avatar(self, uid: int) -> str: ...

    async def get_new_user_video_bvids(
        self,
        uid: int,
        known_bvids: set[str],
        scan_limit: int,
    ) -> tuple[str, ...]: ...

    async def get_video(self, bvid: str) -> VideoInfo: ...

    async def get_audio_length(self, bvid: str, cid: int) -> int: ...


class FeedStore(Protocol):
    def known_bvids(self, uid: int) -> set[str]: ...

    def save_video(
        self, uid: int, bvid: str, episodes: list[StoredEpisode]
    ) -> None: ...

    def episodes_for_uid(self, uid: int, limit: int) -> list[StoredEpisode]: ...


class EpisodeEditor(Protocol):
    async def edit_episode(self, episode: Episode) -> Episode: ...


logger = logging.getLogger(__name__)

USER_SCAN_LIMIT = 100
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"


class FeedRefresher:
    def __init__(
        self,
        source: VideoSource,
        store: FeedStore,
        base_url: str,
        editor: EpisodeEditor | None = None,
    ) -> None:
        self.source = source
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.editor = editor

    async def refresh(self, feed: FeedConfig) -> FeedSnapshot:
        episodes: list[Episode] = []
        image_url = await self.source.get_user_avatar(feed.users[0].uid)

        for user in feed.users:
            known_bvids = self.store.known_bvids(user.uid)
            initializing = not known_bvids
            accepted_videos = 0
            candidates = await self.source.get_new_user_video_bvids(
                user.uid,
                known_bvids,
                scan_limit=USER_SCAN_LIMIT,
            )
            for bvid in candidates:
                video = await self.source.get_video(bvid)
                multiple_pages = len(video.pages) > 1
                video_episodes: list[StoredEpisode] = []
                for page in video.pages:
                    try:
                        length = await self.source.get_audio_length(
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
                        StoredEpisode(
                            bvid=video.bvid,
                            cid=page.cid,
                            title=title,
                            description=video.description,
                            published_at=video.published_at,
                            duration=page.duration,
                            image_url=video.image_url,
                            media_length=length,
                        )
                    )

                self.store.save_video(user.uid, bvid, video_episodes)
                if video_episodes:
                    accepted_videos += 1
                    if initializing and accepted_videos >= user.limit:
                        break

            stored_episodes = self.store.episodes_for_uid(user.uid, user.limit)
            public_episodes = [
                self._public_episode(episode) for episode in stored_episodes
            ]
            if self.editor is not None:
                public_episodes = [
                    await self.editor.edit_episode(episode)
                    for episode in public_episodes
                ]
            episodes.extend(public_episodes)
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
        return FeedSnapshot(tuple(sorted_episodes), image_url)

    def _public_episode(self, episode: StoredEpisode) -> Episode:
        return Episode(
            bvid=episode.bvid,
            cid=episode.cid,
            title=episode.title,
            description=episode.description,
            published_at=episode.published_at,
            duration=episode.duration,
            image_url=episode.image_url,
            media_url=(f"{self.base_url}/media/{episode.bvid}/{episode.cid}.m4a"),
            media_length=episode.media_length,
        )


class RssRenderer:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def render(
        self,
        feed: FeedConfig,
        episodes: Sequence[Episode],
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


class PodcastService:
    def __init__(self, refresher: FeedRefresher, renderer: RssRenderer) -> None:
        self.refresher = refresher
        self.renderer = renderer
        self.feed_locks: dict[str, asyncio.Lock] = {}

    async def build_feed(self, feed: FeedConfig) -> bytes:
        lock = self.feed_locks.setdefault(feed.slug, asyncio.Lock())
        async with lock:
            snapshot = await self.refresher.refresh(feed)
            return self.renderer.render(
                feed,
                snapshot.episodes,
                snapshot.image_url,
            )
