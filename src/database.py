from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models import StoredEpisode
from mp4 import ManifestFragment, VirtualMediaManifest


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

    def save_manifest(
        self,
        manifest_id: str,
        bvid: str,
        cid: int,
        manifest: VirtualMediaManifest,
    ) -> None:
        fragments = json.dumps(
            [
                {
                    "source_index": fragment.source_index,
                    "source_start": fragment.source_start,
                    "size": fragment.size,
                    "output_start": fragment.output_start,
                    "duration": fragment.duration,
                    "new_decode_time": fragment.new_decode_time,
                }
                for fragment in manifest.fragments
            ],
            separators=(",", ":"),
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO media_manifests (
                    manifest_id, bvid, cid, prefix, fragments, timescale,
                    output_length, output_duration
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (manifest_id) DO UPDATE SET
                    prefix = excluded.prefix,
                    fragments = excluded.fragments,
                    timescale = excluded.timescale,
                    output_length = excluded.output_length,
                    output_duration = excluded.output_duration
                """,
                (
                    manifest_id,
                    bvid,
                    cid,
                    manifest.prefix,
                    fragments,
                    manifest.timescale,
                    manifest.output_length,
                    manifest.output_duration,
                ),
            )

    def get_manifest(
        self, manifest_id: str, bvid: str, cid: int
    ) -> VirtualMediaManifest | None:
        row = self.connection.execute(
            """
            SELECT prefix, fragments, timescale, output_length, output_duration
            FROM media_manifests
            WHERE manifest_id = ? AND bvid = ? AND cid = ?
            """,
            (manifest_id, bvid, cid),
        ).fetchone()
        if row is None:
            return None
        fragments = tuple(
            ManifestFragment(
                source_index=int(item["source_index"]),
                source_start=int(item["source_start"]),
                size=int(item["size"]),
                output_start=int(item["output_start"]),
                duration=int(item["duration"]),
                new_decode_time=int(item["new_decode_time"]),
            )
            for item in json.loads(str(row["fragments"]))
        )
        return VirtualMediaManifest(
            prefix=bytes(row["prefix"]),
            fragments=fragments,
            timescale=int(row["timescale"]),
            output_length=int(row["output_length"]),
            output_duration=float(row["output_duration"]),
        )

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

                CREATE TABLE IF NOT EXISTS media_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    bvid TEXT NOT NULL,
                    cid INTEGER NOT NULL,
                    prefix BLOB NOT NULL,
                    fragments TEXT NOT NULL,
                    timescale INTEGER NOT NULL,
                    output_length INTEGER NOT NULL,
                    output_duration REAL NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch())
                );

                CREATE INDEX IF NOT EXISTS media_manifests_media
                    ON media_manifests (bvid, cid);
                """
            )
