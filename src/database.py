from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models import StoredEpisode


class FeedStateStore:
    def __init__(self, path: str | Path) -> None:
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def known_bvids(self, uid: int) -> set[str]:
        rows = self.connection.execute(
            "SELECT bvid FROM known_videos WHERE uid = ?",
            (uid,),
        )
        return {str(row["bvid"]) for row in rows}

    def save_video(
        self,
        uid: int,
        bvid: str,
        episodes: list[StoredEpisode],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO known_videos (uid, bvid)
                VALUES (?, ?)
                ON CONFLICT (uid, bvid) DO NOTHING
                """,
                (uid, bvid),
            )
            self.connection.execute(
                "DELETE FROM episodes WHERE uid = ? AND bvid = ?",
                (uid, bvid),
            )
            self.connection.executemany(
                """
                INSERT INTO episodes (
                    uid, bvid, cid, title, description, published_at,
                    duration, image_url, media_length
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        uid,
                        episode.bvid,
                        episode.cid,
                        episode.title,
                        episode.description,
                        int(episode.published_at.timestamp()),
                        episode.duration,
                        episode.image_url,
                        episode.media_length,
                    )
                    for episode in episodes
                ],
            )

    def episodes_for_uid(self, uid: int, limit: int) -> list[StoredEpisode]:
        rows = self.connection.execute(
            """
            WITH selected_videos AS (
                SELECT bvid, MAX(published_at) AS latest_at
                FROM episodes
                WHERE uid = ?
                GROUP BY bvid
                ORDER BY latest_at DESC
                LIMIT ?
            )
            SELECT
                e.bvid, e.cid, e.title, e.description, e.published_at,
                e.duration, e.image_url, e.media_length
            FROM episodes AS e
            JOIN selected_videos AS selected ON selected.bvid = e.bvid
            WHERE e.uid = ?
            ORDER BY e.published_at DESC, e.cid ASC
            """,
            (uid, limit, uid),
        )
        return [
            StoredEpisode(
                bvid=str(row["bvid"]),
                cid=int(row["cid"]),
                title=str(row["title"]),
                description=str(row["description"]),
                published_at=datetime.fromtimestamp(
                    int(row["published_at"]), tz=timezone.utc
                ),
                duration=int(row["duration"]),
                image_url=str(row["image_url"]),
                media_length=int(row["media_length"]),
            )
            for row in rows
        ]

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS known_videos (
                    uid INTEGER NOT NULL,
                    bvid TEXT NOT NULL,
                    discovered_at INTEGER NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY (uid, bvid)
                );

                CREATE TABLE IF NOT EXISTS episodes (
                    uid INTEGER NOT NULL,
                    bvid TEXT NOT NULL,
                    cid INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    published_at INTEGER NOT NULL,
                    duration INTEGER NOT NULL,
                    image_url TEXT NOT NULL,
                    media_length INTEGER NOT NULL,
                    PRIMARY KEY (uid, bvid, cid),
                    FOREIGN KEY (uid, bvid)
                        REFERENCES known_videos (uid, bvid)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS episodes_uid_published
                    ON episodes (uid, published_at DESC);
                """
            )
