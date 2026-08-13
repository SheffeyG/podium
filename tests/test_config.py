from pathlib import Path

import pytest

from podium.config import ConfigError, load_config


def test_example_config_is_valid() -> None:
    config = load_config(Path(__file__).parents[1] / "example.yaml")

    assert config.feeds[0].users[0].uid == 123456


def test_load_config_and_environment_cookie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
base_url: https://podium.example.com/
bilibili:
  sessdata: from-file
sponsorblock:
  enabled: true
  categories: [sponsor, intro, sponsor]
feeds:
  - slug: talks
    title: Talks
    description: Selected talks
    users:
      - uid: uid193147738
        limit: 12
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BILIBILI_SESSDATA", "from-environment")
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=full; DedeUserID=123")

    config = load_config(path)

    assert config.base_url == "https://podium.example.com"
    assert config.sessdata == "from-environment"
    assert config.bilibili_cookie == "SESSDATA=full; DedeUserID=123"
    assert config.sponsorblock.enabled is True
    assert config.sponsorblock.categories == ("sponsor", "intro")
    feed = config.feed_by_slug("talks")
    assert feed is not None
    assert feed.users[0].uid == 193147738
    assert feed.users[0].limit == 12


def test_duplicate_feed_slug_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
base_url: http://localhost:8000
feeds:
  - {slug: same, title: One, description: One, users: [193147738]}
  - {slug: same, title: Two, description: Two, users: [193147739]}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate feed slug"):
        load_config(path)


def test_manual_videos_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
base_url: http://localhost:8000
feeds:
  - slug: talks
    title: Talks
    description: Selected talks
    users: [193147738]
    videos: [BV1ab411c7mD]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="videos is no longer supported"):
        load_config(path)
