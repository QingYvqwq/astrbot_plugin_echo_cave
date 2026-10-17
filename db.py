"""Independent SQLite persistence layer with explicit schema versioning.

The plugin deliberately does not use AstrBot's internal ORM. This keeps the
data model stable across framework upgrades and lets the import tool read
legacy databases without depending on the host application.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from .content import ContentError, parse_content
from .models import CaveRecord, MediaRefs, SendFailure, json_list, parse_datetime, utc_now

CURRENT_SCHEMA_VERSION = 1
T = TypeVar("T")

CAVE_COLUMNS = {
    "public_id",
    "channel_id",
    "create_time",
    "user_id",
    "origin_user_id",
    "type",
    "content",
    "related_users",
    "draw_count",
    "pic_draw_count",
    "file_urls",
    "image_urls",
    "record_urls",
    "video_urls",
    "source_key",
}


class DatabaseError(RuntimeError):
    pass


class DuplicateCaveError(DatabaseError):
    def __init__(self, public_id: int) -> None:
        super().__init__(f"duplicate cave submission (existing public id: {public_id})")
        self.public_id = public_id


class BackupValidationError(DatabaseError):
    pass


class SnapshotVerificationError(DatabaseError):
    pass


def _dump_list(values: list[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _dump_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class CaveDatabase:
    """Async facade over a single SQLite connection.

    All access is serialized with an :class:`asyncio.Lock`; blocking sqlite
    calls run in a worker thread so they never stall the bot event loop.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await self._run(self._initialize_sync)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    async def _run(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        async with self._lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _initialize_sync(self) -> None:
        conn = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn

        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ( key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        current = int(row["value"]) if row else 0
        if current > CURRENT_SCHEMA_VERSION:
            raise DatabaseError(
                f"database schema version {current} is newer than supported "
                f"version {CURRENT_SCHEMA_VERSION}"
            )
        if current == 0:
            self._migrate_0_to_1(conn)
        elif current != CURRENT_SCHEMA_VERSION:
            raise DatabaseError(f"missing migration path from schema {current}")

    @staticmethod
    def _migrate_0_to_1(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS caves (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id INTEGER NOT NULL,
                    channel_id TEXT NOT NULL,
                    create_time TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    origin_user_id TEXT NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('forward','msg')),
                    content TEXT NOT NULL,
                    related_users TEXT NOT NULL DEFAULT '[]',
                    draw_count INTEGER NOT NULL DEFAULT 0,
                    pic_draw_count INTEGER NOT NULL DEFAULT 0,
                    file_urls TEXT NOT NULL DEFAULT '[]',
                    image_urls TEXT NOT NULL DEFAULT '[]',
                    record_urls TEXT NOT NULL DEFAULT '[]',
                    video_urls TEXT NOT NULL DEFAULT '[]',
                    source_key TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_caves_public_id ON caves(public_id)")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_caves_channel_source "
                "ON caves(channel_id, source_key)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_caves_channel ON caves(channel_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_caves_channel_origin "
                "ON caves(channel_id, origin_user_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_caves_channel_user ON caves(channel_id, user_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS send_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cave_id INTEGER NOT NULL,
                    channel_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL DEFAULT '',
                    platform_id TEXT NOT NULL DEFAULT '',
                    self_id TEXT NOT NULL DEFAULT '',
                    fail_time TEXT NOT NULL,
                    error_message TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_states (
                    user_id TEXT PRIMARY KEY,
                    has_completed_forward_selection INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES "
                "('schema_version', ?), ('created_at', ?)",
                (str(CURRENT_SCHEMA_VERSION), utc_now().isoformat()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("database is not initialized")
        return self._conn

    # ------------------------------------------------------------------
    # Row mapping
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_cave(row: sqlite3.Row) -> CaveRecord:
        return CaveRecord(
            entry_id=row["entry_id"],
            public_id=row["public_id"],
            channel_id=row["channel_id"],
            create_time=parse_datetime(row["create_time"]),
            user_id=row["user_id"],
            origin_user_id=row["origin_user_id"],
            type=row["type"],
            content=row["content"],
            related_users=json_list(row["related_users"]),
            draw_count=int(row["draw_count"] or 0),
            pic_draw_count=int(row["pic_draw_count"] or 0),
            file_urls=json_list(row["file_urls"]),
            image_urls=json_list(row["image_urls"]),
            record_urls=json_list(row["record_urls"]),
            video_urls=json_list(row["video_urls"]),
            source_key=row["source_key"],
        )

    @staticmethod
    def _row_to_failure(row: sqlite3.Row) -> SendFailure:
        return SendFailure(
            id=row["id"],
            cave_id=row["cave_id"],
            channel_id=row["channel_id"],
            guild_id=row["guild_id"],
            platform_id=row["platform_id"],
            self_id=row["self_id"],
            fail_time=parse_datetime(row["fail_time"]),
            error_message=row["error_message"],
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    async def create_cave(self, cave: CaveRecord, *, allow_duplicate: bool = False) -> CaveRecord:
        return await self._run(self._create_cave_sync, cave, allow_duplicate)

    def _create_cave_sync(self, cave: CaveRecord, allow_duplicate: bool) -> CaveRecord:
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if cave.source_key:
                existing = conn.execute(
                    "SELECT public_id FROM caves WHERE channel_id=? AND source_key=?",
                    (cave.channel_id, cave.source_key),
                ).fetchone()
                if existing:
                    if not allow_duplicate:
                        conn.execute("ROLLBACK")
                        raise DuplicateCaveError(int(existing["public_id"]))
                    # The unique (channel, source_key) index cannot hold two
                    # identical fingerprints; explicit duplicate imports are
                    # stored without the dedup key.
                    cave.source_key = None
            if cave.public_id <= 0:
                row = conn.execute(
                    "SELECT COALESCE(MAX(public_id), 0) AS max_id FROM caves"
                ).fetchone()
                cave.public_id = int(row["max_id"]) + 1
            cursor = conn.execute(
                """
                INSERT INTO caves(
                    public_id, channel_id, create_time, user_id, origin_user_id,
                    type, content, related_users, draw_count, pic_draw_count,
                    file_urls, image_urls, record_urls, video_urls, source_key,
                    created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cave.public_id,
                    cave.channel_id,
                    _dump_dt(cave.create_time),
                    cave.user_id,
                    cave.origin_user_id,
                    cave.type,
                    cave.content,
                    _dump_list(cave.related_users),
                    cave.draw_count,
                    cave.pic_draw_count,
                    _dump_list(cave.file_urls),
                    _dump_list(cave.image_urls),
                    _dump_list(cave.record_urls),
                    _dump_list(cave.video_urls),
                    cave.source_key,
                    utc_now().isoformat(),
                ),
            )
            cave.entry_id = int(cursor.lastrowid)
            conn.execute("COMMIT")
            return cave
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    async def get_cave(self, public_id: int, channel_id: str | None = None) -> CaveRecord | None:
        return await self._run(self._get_cave_sync, public_id, channel_id)

    def _get_cave_sync(self, public_id: int, channel_id: str | None) -> CaveRecord | None:
        conn = self._connection()
        if channel_id is None:
            row = conn.execute("SELECT * FROM caves WHERE public_id=?", (public_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM caves WHERE public_id=? AND channel_id=?",
                (public_id, channel_id),
            ).fetchone()
        return self._row_to_cave(row) if row else None

    async def get_caves_by_public_ids(
        self, public_ids: list[int], channel_id: str | None = None
    ) -> list[CaveRecord]:
        return await self._run(self._get_caves_by_ids_sync, public_ids, channel_id)

    def _get_caves_by_ids_sync(
        self, public_ids: list[int], channel_id: str | None
    ) -> list[CaveRecord]:
        ids = sorted({int(value) for value in public_ids})
        if not ids:
            return []
        conn = self._connection()
        placeholders = ",".join("?" for _ in ids)
        params: list[Any] = list(ids)
        sql = f"SELECT * FROM caves WHERE public_id IN ({placeholders})"
        if channel_id is not None:
            sql += " AND channel_id=?"
            params.append(channel_id)
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_cave(row) for row in rows]

    async def get_caves_by_channel(self, channel_id: str) -> list[CaveRecord]:
        return await self._run(self._get_caves_by_channel_sync, channel_id)

    def _get_caves_by_channel_sync(self, channel_id: str) -> list[CaveRecord]:
        rows = (
            self._connection()
            .execute(
                "SELECT * FROM caves WHERE channel_id=? ORDER BY public_id",
                (channel_id,),
            )
            .fetchall()
        )
        return [self._row_to_cave(row) for row in rows]

    async def get_all_caves(self) -> list[CaveRecord]:
        return await self._run(self._get_all_caves_sync)

    def _get_all_caves_sync(self) -> list[CaveRecord]:
        rows = self._connection().execute("SELECT * FROM caves ORDER BY public_id").fetchall()
        return [self._row_to_cave(row) for row in rows]

    async def get_caves_by_user(
        self, field: str, user_id: str, channel_id: str
    ) -> list[CaveRecord]:
        if field not in ("user_id", "origin_user_id"):
            raise ValueError(f"unsupported user field: {field}")
        return await self._run(self._get_caves_by_user_sync, field, user_id, channel_id)

    def _get_caves_by_user_sync(
        self, field: str, user_id: str, channel_id: str
    ) -> list[CaveRecord]:
        rows = (
            self._connection()
            .execute(
                f"SELECT * FROM caves WHERE {field}=? AND channel_id=? ORDER BY public_id",
                (user_id, channel_id),
            )
            .fetchall()
        )
        return [self._row_to_cave(row) for row in rows]

    async def count_caves(self) -> int:
        return await self._run(self._count_caves_sync)

    def _count_caves_sync(self) -> int:
        row = self._connection().execute("SELECT COUNT(*) AS n FROM caves").fetchone()
        return int(row["n"])

    async def update_cave(self, entry_id: int, **fields: Any) -> None:
        await self._run(self._update_cave_sync, entry_id, fields)

    def _update_cave_sync(self, entry_id: int, fields: dict[str, Any]) -> None:
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key in ("related_users", "file_urls", "image_urls", "record_urls", "video_urls"):
                updates[key] = _dump_list(list(value))
            elif key == "create_time":
                updates[key] = _dump_dt(parse_datetime(value))
            elif key == "public_id":
                updates[key] = int(value)
            elif key in CAVE_COLUMNS:
                updates[key] = value
            else:
                raise ValueError(f"unknown cave column: {key}")
        if not updates:
            return
        assignments = ", ".join(f"{key}=?" for key in updates)
        self._connection().execute(
            f"UPDATE caves SET {assignments} WHERE entry_id=?",
            (*updates.values(), entry_id),
        )

    async def increment_draw(self, entry_id: int, field: str = "draw_count") -> None:
        if field not in ("draw_count", "pic_draw_count"):
            raise ValueError(f"unsupported draw field: {field}")
        await self._run(self._increment_draw_sync, entry_id, field)

    def _increment_draw_sync(self, entry_id: int, field: str) -> None:
        self._connection().execute(
            f"UPDATE caves SET {field}={field}+1 WHERE entry_id=?", (entry_id,)
        )

    async def delete_cave(self, entry_id: int) -> bool:
        return await self._run(self._delete_cave_sync, entry_id)

    def _delete_cave_sync(self, entry_id: int) -> bool:
        cursor = self._connection().execute("DELETE FROM caves WHERE entry_id=?", (entry_id,))
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # User state / send failures
    # ------------------------------------------------------------------
    async def get_forward_selection_done(self, user_id: str) -> bool:
        return await self._run(self._get_forward_selection_done_sync, user_id)

    def _get_forward_selection_done_sync(self, user_id: str) -> bool:
        row = (
            self._connection()
            .execute(
                "SELECT has_completed_forward_selection FROM user_states WHERE user_id=?",
                (user_id,),
            )
            .fetchone()
        )
        return bool(row and row["has_completed_forward_selection"])

    async def set_forward_selection_done(self, user_id: str) -> None:
        await self._run(self._set_forward_selection_done_sync, user_id)

    def _set_forward_selection_done_sync(self, user_id: str) -> None:
        self._connection().execute(
            "INSERT INTO user_states(user_id, has_completed_forward_selection) "
            "VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET "
            "has_completed_forward_selection=1",
            (user_id,),
        )

    async def add_send_failure(
        self,
        *,
        cave_id: int,
        channel_id: str,
        guild_id: str,
        platform_id: str,
        self_id: str,
        error_message: str,
    ) -> int:
        return await self._run(
            self._add_send_failure_sync,
            cave_id,
            channel_id,
            guild_id,
            platform_id,
            self_id,
            error_message,
        )

    def _add_send_failure_sync(
        self,
        cave_id: int,
        channel_id: str,
        guild_id: str,
        platform_id: str,
        self_id: str,
        error_message: str,
    ) -> int:
        cursor = self._connection().execute(
            "INSERT INTO send_failures(cave_id, channel_id, guild_id, platform_id, "
            "self_id, fail_time, error_message) VALUES (?,?,?,?,?,?,?)",
            (
                cave_id,
                channel_id,
                guild_id,
                platform_id,
                self_id,
                utc_now().isoformat(),
                error_message,
            ),
        )
        return int(cursor.lastrowid)

    async def list_send_failures(self) -> list[SendFailure]:
        return await self._run(self._list_send_failures_sync)

    def _list_send_failures_sync(self) -> list[SendFailure]:
        rows = (
            self._connection()
            .execute("SELECT * FROM send_failures ORDER BY fail_time, id")
            .fetchall()
        )
        return [self._row_to_failure(row) for row in rows]

    async def delete_send_failures(self, ids: list[int]) -> None:
        await self._run(self._delete_send_failures_sync, ids)

    def _delete_send_failures_sync(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self._connection().execute(
            f"DELETE FROM send_failures WHERE id IN ({placeholders})",
            list(ids),
        )

    # ------------------------------------------------------------------
    # Reindex / backup / restore
    # ------------------------------------------------------------------
    async def write_reindex_backup(
        self,
        backup_dir: str | Path,
        caves: list[CaveRecord],
        mapping: list[dict[str, int]],
    ) -> Path:
        return await self._run(self._write_reindex_backup_sync, backup_dir, caves, mapping)

    @staticmethod
    def _write_reindex_backup_sync(
        backup_dir: str | Path,
        caves: list[CaveRecord],
        mapping: list[dict[str, int]],
    ) -> Path:
        directory = Path(backup_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y-%m-%dT%H-%M-%S-%fZ")
        target = directory / f"echo-cave-reindex-{stamp}.json"
        payload = {
            "createdAt": utc_now().isoformat(),
            "records": [cave.to_backup() for cave in caves],
            "mapping": mapping,
        }
        fd, temp_name = tempfile.mkstemp(
            prefix=".echo-cave-backup-", suffix=".tmp", dir=str(directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
        return target

    async def apply_reindex(self, caves: list[CaveRecord]) -> None:
        """Rewrite public IDs to 1..N inside a single transaction."""
        await self._run(self._apply_reindex_sync, caves)

    def _apply_reindex_sync(self, caves: list[CaveRecord]) -> None:
        conn = self._connection()
        ordered = sorted(caves, key=lambda item: item.public_id)
        expected: list[CaveRecord] = []
        for index, cave in enumerate(ordered, start=1):
            clone = CaveRecord(**{**cave.__dict__})
            clone.public_id = index
            expected.append(clone)

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE caves SET public_id = -entry_id")
            for cave in expected:
                if cave.entry_id is None:
                    raise DatabaseError(f"missing entry_id for cave {cave.public_id}")
                conn.execute(
                    "UPDATE caves SET public_id=? WHERE entry_id=?",
                    (cave.public_id, cave.entry_id),
                )
            self._verify_snapshot_sync(expected)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _verify_restored_sync(self, expected: list[CaveRecord]) -> None:
        """Verify a restore without requiring sequential public IDs."""
        actual = [
            self._row_to_cave(row)
            for row in self._connection()
            .execute("SELECT * FROM caves ORDER BY public_id")
            .fetchall()
        ]
        expected_sorted = sorted(expected, key=lambda item: item.public_id)
        if len(actual) != len(expected_sorted):
            raise SnapshotVerificationError("record_count_mismatch")
        for actual_cave, expected_cave in zip(actual, expected_sorted, strict=False):
            if actual_cave.normalized() != expected_cave.normalized():
                raise SnapshotVerificationError(
                    f"record_content_mismatch at public id {actual_cave.public_id}"
                )

    def _verify_snapshot_sync(self, expected: list[CaveRecord]) -> None:
        actual = [
            self._row_to_cave(row)
            for row in self._connection()
            .execute("SELECT * FROM caves ORDER BY public_id")
            .fetchall()
        ]
        expected_sorted = sorted(expected, key=lambda item: item.public_id)
        if len(actual) != len(expected_sorted):
            raise SnapshotVerificationError("record_count_mismatch")
        for index, (actual_cave, expected_cave) in enumerate(
            zip(actual, expected_sorted, strict=False)
        ):
            if actual_cave.public_id != index + 1:
                raise SnapshotVerificationError("id_sequence_mismatch")
            if actual_cave.normalized() != expected_cave.normalized():
                raise SnapshotVerificationError(
                    f"record_content_mismatch at public id {actual_cave.public_id}"
                )

    async def import_records(
        self, records: list[CaveRecord], *, overwrite: bool = False
    ) -> dict[str, int]:
        """Import externally sourced records, preserving public/entry IDs.

        Args:
            records: Validated records (``entry_id`` and ``public_id`` set).
            overwrite: Update rows whose public ID already exists.

        Returns:
            Counts of inserted, updated and skipped records.
        """
        return await self._run(self._import_records_sync, records, overwrite)

    def _import_records_sync(self, records: list[CaveRecord], overwrite: bool) -> dict[str, int]:
        conn = self._connection()
        inserted = updated = skipped = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing_entry_ids = {
                int(row["entry_id"])
                for row in conn.execute("SELECT entry_id FROM caves").fetchall()
            }
            for record in records:
                existing = conn.execute(
                    "SELECT entry_id FROM caves WHERE public_id=?",
                    (record.public_id,),
                ).fetchone()
                if existing is not None:
                    if not overwrite:
                        skipped += 1
                        continue
                    conn.execute(
                        """
                        UPDATE caves SET channel_id=?, create_time=?, user_id=?,
                            origin_user_id=?, type=?, content=?, related_users=?,
                            draw_count=?, pic_draw_count=?, file_urls=?,
                            image_urls=?, record_urls=?, video_urls=?, source_key=?
                        WHERE public_id=?
                        """,
                        (
                            record.channel_id,
                            _dump_dt(record.create_time),
                            record.user_id,
                            record.origin_user_id,
                            record.type,
                            record.content,
                            _dump_list(record.related_users),
                            record.draw_count,
                            record.pic_draw_count,
                            _dump_list(record.file_urls),
                            _dump_list(record.image_urls),
                            _dump_list(record.record_urls),
                            _dump_list(record.video_urls),
                            record.source_key,
                            record.public_id,
                        ),
                    )
                    updated += 1
                    continue

                entry_id = record.entry_id
                if entry_id is not None and entry_id in existing_entry_ids:
                    entry_id = None
                columns = (
                    "entry_id, public_id, channel_id, create_time, user_id, "
                    "origin_user_id, type, content, related_users, draw_count, "
                    "pic_draw_count, file_urls, image_urls, record_urls, "
                    "video_urls, source_key, created_at"
                )
                placeholders = ",".join("?" for _ in columns.split(","))
                values = (
                    entry_id,
                    record.public_id,
                    record.channel_id,
                    _dump_dt(record.create_time),
                    record.user_id,
                    record.origin_user_id,
                    record.type,
                    record.content,
                    _dump_list(record.related_users),
                    record.draw_count,
                    record.pic_draw_count,
                    _dump_list(record.file_urls),
                    _dump_list(record.image_urls),
                    _dump_list(record.record_urls),
                    _dump_list(record.video_urls),
                    record.source_key,
                    utc_now().isoformat(),
                )
                cursor = conn.execute(
                    f"INSERT INTO caves({columns}) VALUES ({placeholders})", values
                )
                if entry_id is None:
                    record.entry_id = int(cursor.lastrowid)
                    existing_entry_ids.add(record.entry_id)
                else:
                    existing_entry_ids.add(entry_id)
                inserted += 1
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    async def replace_with_snapshot(self, records: list[CaveRecord]) -> None:
        """Atomically replace all cave rows with the provided snapshot."""
        await self._run(self._replace_with_snapshot_sync, records)

    def _replace_with_snapshot_sync(self, records: list[CaveRecord]) -> None:
        conn = self._connection()
        existing = [
            self._row_to_cave(row)
            for row in conn.execute("SELECT * FROM caves ORDER BY public_id").fetchall()
        ]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM caves")
            for record in records:
                if record.entry_id is None:
                    raise BackupValidationError(f"missing entryId for cave {record.public_id}")
                conn.execute(
                    """
                    INSERT INTO caves(
                        entry_id, public_id, channel_id, create_time, user_id,
                        origin_user_id, type, content, related_users, draw_count,
                        pic_draw_count, file_urls, image_urls, record_urls,
                        video_urls, source_key, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record.entry_id,
                        record.public_id,
                        record.channel_id,
                        _dump_dt(record.create_time),
                        record.user_id,
                        record.origin_user_id,
                        record.type,
                        record.content,
                        _dump_list(record.related_users),
                        record.draw_count,
                        record.pic_draw_count,
                        _dump_list(record.file_urls),
                        _dump_list(record.image_urls),
                        _dump_list(record.record_urls),
                        _dump_list(record.video_urls),
                        record.source_key,
                        utc_now().isoformat(),
                    ),
                )
            self._verify_restored_sync(records)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            # Defensive: if the transaction was already committed before a
            # verification error surfaced, restore the previous rows.
            committed = conn.execute("SELECT COUNT(*) AS n FROM caves").fetchone()["n"]
            if int(committed) != len(existing):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("DELETE FROM caves")
                    for record in existing:
                        self._insert_existing_sync(conn, record)
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            raise

    @staticmethod
    def _insert_existing_sync(conn: sqlite3.Connection, record: CaveRecord) -> None:
        conn.execute(
            """
            INSERT INTO caves(
                entry_id, public_id, channel_id, create_time, user_id,
                origin_user_id, type, content, related_users, draw_count,
                pic_draw_count, file_urls, image_urls, record_urls,
                video_urls, source_key, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.entry_id,
                record.public_id,
                record.channel_id,
                _dump_dt(record.create_time),
                record.user_id,
                record.origin_user_id,
                record.type,
                record.content,
                _dump_list(record.related_users),
                record.draw_count,
                record.pic_draw_count,
                _dump_list(record.file_urls),
                _dump_list(record.image_urls),
                _dump_list(record.record_urls),
                _dump_list(record.video_urls),
                record.source_key,
                utc_now().isoformat(),
            ),
        )


# ----------------------------------------------------------------------
# Reindex backup parsing (shared by the admin service and tests)
# ----------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BackupValidationError(message)


def _has_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def parse_reindex_backup(payload_text: str) -> tuple[str, list[CaveRecord], list[dict[str, int]]]:
    """Parse and strictly validate a reindex backup.

    Args:
        payload_text: JSON text of the backup file.

    Returns:
        ``(created_at, records, mapping)``.

    Raises:
        BackupValidationError: On malformed or inconsistent data.
    """
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise BackupValidationError(f"invalid_backup_json: {exc}") from exc
    _require(isinstance(payload, dict), "invalid_backup_payload")
    created_at = payload.get("createdAt")
    _require(isinstance(created_at, str) and bool(created_at), "invalid_backup_created_at")
    raw_records = payload.get("records")
    _require(isinstance(raw_records, list), "invalid_backup_records")

    records: list[CaveRecord] = []
    seen_entry_ids: set[int] = set()
    seen_public_ids: set[int] = set()
    for raw in raw_records:
        _require(isinstance(raw, dict), "invalid_backup_record")
        entry_id = raw.get("entryId")
        public_id = raw.get("id")
        _require(isinstance(entry_id, int) and entry_id > 0, "invalid_backup_entry_id")
        _require(
            isinstance(public_id, int) and public_id > 0,
            f"invalid_backup_public_id for entry {entry_id}",
        )
        _require(entry_id not in seen_entry_ids, f"duplicate_entry_id: {entry_id}")
        _require(public_id not in seen_public_ids, f"duplicate_public_id: {public_id}")
        seen_entry_ids.add(entry_id)
        seen_public_ids.add(public_id)
        cave_type = raw.get("type")
        _require(cave_type in ("msg", "forward"), f"invalid_type for entry {entry_id}")
        content = raw.get("content")
        _require(isinstance(content, str), f"invalid_content for entry {entry_id}")
        try:
            parse_content(content)
        except ContentError as exc:
            raise BackupValidationError(
                f"invalid_content_json for entry {entry_id}: {exc}"
            ) from exc
        for field in ("channelId", "userId", "originUserId"):
            _require(
                isinstance(raw.get(field), str),
                f"invalid_{field} for entry {entry_id}",
            )
        for field in ("relatedUsers", "fileUrls", "imageUrls", "recordUrls", "videoUrls"):
            _require(
                _has_string_list(raw.get(field, [])),
                f"invalid_{field} for entry {entry_id}",
            )
        _require(
            isinstance(raw.get("drawCount", 0), int)
            and isinstance(raw.get("picDrawCount", 0), int),
            f"invalid_draw_count for entry {entry_id}",
        )
        try:
            create_time = parse_datetime(raw.get("createTime"))
        except (ValueError, TypeError) as exc:
            raise BackupValidationError(f"invalid_create_time for entry {entry_id}") from exc
        records.append(
            CaveRecord(
                entry_id=entry_id,
                public_id=public_id,
                channel_id=raw["channelId"],
                create_time=create_time,
                user_id=raw["userId"],
                origin_user_id=raw["originUserId"],
                type=cave_type,
                content=content,
                related_users=list(raw.get("relatedUsers", [])),
                draw_count=int(raw.get("drawCount", 0)),
                pic_draw_count=int(raw.get("picDrawCount", 0)),
                file_urls=list(raw.get("fileUrls", [])),
                image_urls=list(raw.get("imageUrls", [])),
                record_urls=list(raw.get("recordUrls", [])),
                video_urls=list(raw.get("videoUrls", [])),
                source_key=None,
            )
        )

    raw_mapping = payload.get("mapping")
    _require(isinstance(raw_mapping, list), "invalid_backup_mapping")
    mapping: list[dict[str, int]] = []
    for item in raw_mapping:
        _require(isinstance(item, dict), "invalid_backup_mapping_item")
        _require(
            all(isinstance(item.get(key), int) for key in ("oldId", "newId", "tempId")),
            "invalid_backup_mapping_item",
        )
        mapping.append(
            {
                "oldId": int(item["oldId"]),
                "newId": int(item["newId"]),
                "tempId": int(item["tempId"]),
            }
        )
    return created_at, records, mapping


def build_reindex_mapping(caves: list[CaveRecord]) -> list[dict[str, int]]:
    """Build the reindex mapping structure (temp IDs are metadata only)."""
    ordered = sorted(caves, key=lambda item: item.public_id)
    max_id = max((cave.public_id for cave in ordered), default=0)
    offset = max_id + len(ordered) + 1_000_000
    return [
        {
            "oldId": cave.public_id,
            "newId": index,
            "tempId": cave.public_id + offset,
        }
        for index, cave in enumerate(ordered, start=1)
    ]


def caves_are_sequential(caves: list[CaveRecord]) -> bool:
    ordered = sorted(caves, key=lambda item: item.public_id)
    return all(cave.public_id == index for index, cave in enumerate(ordered, start=1))


def media_refs_from_lists(
    file_urls: list[str],
    image_urls: list[str],
    record_urls: list[str],
    video_urls: list[str],
) -> MediaRefs:
    return MediaRefs(file_urls, image_urls, record_urls, video_urls)
