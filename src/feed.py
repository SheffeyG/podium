from __future__ import annotations

import asyncio
import logging

from bilibili import NoCompatibleAudioError
from models import Episode, FeedConfig, FeedSnapshot, StoredEpisode
from protocols import EpisodeEditor, FeedStore, VideoSource
from rss import RssRenderer


logger = logging.getLogger(__name__)

USER_SCAN_LIMIT = 100


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
