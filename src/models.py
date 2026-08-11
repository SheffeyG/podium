from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class UserSource:
    uid: int
    limit: int = 20


@dataclass(frozen=True, slots=True)
class FeedConfig:
    slug: str
    title: str
    description: str
    users: tuple[UserSource, ...]
    author: str = "Podium"
    language: str = "zh-cn"


@dataclass(frozen=True, slots=True)
class AppConfig:
    base_url: str
    feeds: tuple[FeedConfig, ...]
    sessdata: str | None = field(default=None, repr=False)
    bilibili_cookie: str | None = field(default=None, repr=False)

    def feed_by_slug(self, slug: str) -> FeedConfig | None:
        return next((feed for feed in self.feeds if feed.slug == slug), None)


@dataclass(frozen=True, slots=True)
class VideoPage:
    cid: int
    page: int
    title: str
    duration: int


@dataclass(frozen=True, slots=True)
class VideoInfo:
    bvid: str
    title: str
    description: str
    owner: str
    image_url: str
    published_at: datetime
    pages: tuple[VideoPage, ...]


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


@dataclass(frozen=True, slots=True)
class AudioStream:
    url: str
    backup_urls: tuple[str, ...]
    mime_type: str
    codecs: str
    bandwidth: int

    @property
    def urls(self) -> tuple[str, ...]:
        return (self.url, *self.backup_urls)
