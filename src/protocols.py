from __future__ import annotations

from typing import Protocol

from models import StoredEpisode, VideoInfo


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
