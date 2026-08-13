from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
class SponsorBlockConfig:
    enabled: bool = False
    server_url: str = "https://bsbsb.top"
    categories: tuple[str, ...] = (
        "sponsor",
        "selfpromo",
        "interaction",
        "intro",
        "outro",
    )


@dataclass(frozen=True, slots=True)
class AppConfig:
    base_url: str
    feeds: tuple[FeedConfig, ...]
    sponsorblock: SponsorBlockConfig = SponsorBlockConfig()
    sessdata: str | None = field(default=None, repr=False)
    bilibili_cookie: str | None = field(default=None, repr=False)

    def feed_by_slug(self, slug: str) -> FeedConfig | None:
        return next((feed for feed in self.feeds if feed.slug == slug), None)


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_UID_RE = re.compile(r"(?:uid)?(\d+)", re.IGNORECASE)
_SPACE_URL_RE = re.compile(r"space\.bilibili\.com/(\d+)", re.IGNORECASE)


class ConfigError(ValueError):
    pass


def _required_string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _parse_uid(value: Any, context: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{context} must be a positive Bilibili UID")
    if isinstance(value, int):
        uid = value
    elif isinstance(value, str):
        text = value.strip()
        match = _UID_RE.fullmatch(text) or _SPACE_URL_RE.search(text)
        if match is None:
            raise ConfigError(f"{context} must be a Bilibili UID or space URL")
        uid = int(match.group(1))
    else:
        raise ConfigError(f"{context} must be a Bilibili UID or space URL")
    if uid <= 0:
        raise ConfigError(f"{context} must be a positive Bilibili UID")
    return uid


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path or os.getenv("PODIUM_CONFIG", "config.yaml"))
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a mapping")

    base_url = _required_string(raw, "base_url", "config").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("config.base_url must start with http:// or https://")

    raw_feeds = raw.get("feeds", [])
    if not isinstance(raw_feeds, list):
        raise ConfigError("config.feeds must be a list")

    feeds: list[FeedConfig] = []
    slugs: set[str] = set()
    for index, item in enumerate(raw_feeds):
        context = f"config.feeds[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{context} must be a mapping")

        slug = _required_string(item, "slug", context)
        if not _SLUG_RE.fullmatch(slug):
            raise ConfigError(f"{context}.slug contains unsupported characters")
        if slug in slugs:
            raise ConfigError(f"duplicate feed slug: {slug}")

        if "videos" in item:
            raise ConfigError(
                f"{context}.videos is no longer supported; configure users instead"
            )

        raw_users = item.get("users", [])
        if not isinstance(raw_users, list):
            raise ConfigError(f"{context}.users must be a list")
        users: list[UserSource] = []
        user_ids: set[int] = set()
        for user_index, raw_user in enumerate(raw_users):
            user_context = f"{context}.users[{user_index}]"
            if isinstance(raw_user, dict):
                uid = _parse_uid(raw_user.get("uid"), f"{user_context}.uid")
                limit = raw_user.get("limit", 20)
            else:
                uid = _parse_uid(raw_user, user_context)
                limit = 20
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise ConfigError(f"{user_context}.limit must be between 1 and 100")
            if uid in user_ids:
                raise ConfigError(f"duplicate user UID in feed {slug}: {uid}")
            users.append(UserSource(uid=uid, limit=limit))
            user_ids.add(uid)
        if not users:
            raise ConfigError(f"{context}.users must contain at least one user")

        feeds.append(
            FeedConfig(
                slug=slug,
                title=_required_string(item, "title", context),
                description=_required_string(item, "description", context),
                users=tuple(users),
                author=str(item.get("author", "Podium")).strip() or "Podium",
                language=str(item.get("language", "zh-cn")).strip() or "zh-cn",
            )
        )
        slugs.add(slug)

    bilibili = raw.get("bilibili", {}) or {}
    if not isinstance(bilibili, dict):
        raise ConfigError("config.bilibili must be a mapping")
    configured_sessdata = bilibili.get("sessdata")
    sessdata = os.getenv("BILIBILI_SESSDATA") or configured_sessdata
    if sessdata is not None and not isinstance(sessdata, str):
        raise ConfigError("config.bilibili.sessdata must be a string or null")
    configured_cookie = bilibili.get("cookie")
    cookie = os.getenv("BILIBILI_COOKIE") or configured_cookie
    if cookie is not None and not isinstance(cookie, str):
        raise ConfigError("config.bilibili.cookie must be a string or null")

    raw_sponsorblock = raw.get("sponsorblock", {}) or {}
    if not isinstance(raw_sponsorblock, dict):
        raise ConfigError("config.sponsorblock must be a mapping")
    enabled = raw_sponsorblock.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("config.sponsorblock.enabled must be a boolean")
    server_url = str(
        raw_sponsorblock.get("server_url", "https://bsbsb.top")
    ).rstrip("/")
    if not server_url.startswith(("http://", "https://")):
        raise ConfigError("config.sponsorblock.server_url must be an HTTP URL")
    categories = raw_sponsorblock.get(
        "categories",
        ["sponsor", "selfpromo", "interaction", "intro", "outro"],
    )
    if not isinstance(categories, list) or not all(
        isinstance(category, str) and category for category in categories
    ):
        raise ConfigError("config.sponsorblock.categories must be a list of strings")

    return AppConfig(
        base_url=base_url,
        feeds=tuple(feeds),
        sponsorblock=SponsorBlockConfig(
            enabled=enabled,
            server_url=server_url,
            categories=tuple(dict.fromkeys(categories)),
        ),
        sessdata=sessdata or None,
        bilibili_cookie=cookie or None,
    )
