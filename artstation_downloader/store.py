"""Durable download state and project cache.

The first version of this tool rewrote a single ``state.json`` document after
every asset, which is O(n^2) work: a 2 000-image gallery rewrote megabytes of
JSON per image. State now lives in SQLite with WAL, so each finished asset is
one small durable write, readers and writers do not block each other, and an
interrupted run never loses progress.

Existing ``state.json`` files are imported once (the original file is left
untouched so downgrading stays possible).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
ASSET_COLUMNS = (
    "asset_key",
    "profile",
    "project_hash_id",
    "project_position",
    "project_title",
    "asset_id",
    "asset_position",
    "source_url",
    "path",
    "status",
    "bytes",
    "sha256",
    "width",
    "height",
    "content_type",
    "selected_url",
    "error",
    "updated_at",
)
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS assets (
        asset_key TEXT PRIMARY KEY,
        profile TEXT NOT NULL,
        project_hash_id TEXT,
        project_position INTEGER DEFAULT 0,
        project_title TEXT,
        asset_id TEXT,
        asset_position INTEGER DEFAULT 0,
        source_url TEXT,
        path TEXT,
        status TEXT,
        bytes INTEGER,
        sha256 TEXT,
        width INTEGER,
        height INTEGER,
        content_type TEXT,
        selected_url TEXT,
        error TEXT,
        updated_at REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_assets_profile ON assets(profile)",
    "CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(profile, status)",
    """
    CREATE TABLE IF NOT EXISTS projects (
        profile TEXT NOT NULL,
        hash_id TEXT NOT NULL,
        position INTEGER DEFAULT 0,
        title TEXT,
        updated_at TEXT,
        assets_count INTEGER,
        fetched_at REAL,
        snapshot TEXT,
        PRIMARY KEY (profile, hash_id)
    )
    """,
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
)
_SCHEMA_SCRIPT = ";\n".join(statement.strip() for statement in _SCHEMA)


def default_state_path(output_root: Path) -> Path:
    return Path(output_root) / "state.sqlite3"


class DownloadState:
    """SQLite-backed record of every asset handled for an output directory."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.in_memory = str(self.path) == ":memory:"
        if not self.in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: set[sqlite3.Connection] = set()
        self._lock = threading.Lock()
        self._journal_mode = "memory" if self.in_memory else "wal"
        # A named shared-cache memory database keeps every worker thread on the
        # same data without touching the disk.
        self._dsn = "file:artstation-dl-memory?mode=memory&cache=shared" if self.in_memory else str(self.path)
        self._setup()
        if not self.in_memory:
            self._import_legacy_json()

    # -- connection management -------------------------------------------
    def _setup(self) -> None:
        try:
            self._connect().executescript(_SCHEMA_SCRIPT)
        except sqlite3.DatabaseError:
            # A truncated or foreign file must not brick the download. Keep it
            # for forensics and start clean.
            self._close_connections()
            with suppress(OSError):
                self.path.replace(self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}"))
            self._connect().executescript(_SCHEMA_SCRIPT)
        self._connect().execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        connection = sqlite3.connect(
            self._dsn,
            uri=self.in_memory,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            try:
                connection.execute(f"PRAGMA journal_mode = {self._journal_mode}")
            except sqlite3.OperationalError:
                # WAL needs shared-memory support, which some network shares refuse.
                self._journal_mode = "delete"
                with suppress(sqlite3.OperationalError):
                    connection.execute("PRAGMA journal_mode = delete")
            with suppress(sqlite3.OperationalError):
                connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA busy_timeout = 10000")
        except sqlite3.DatabaseError:
            # Release the handle before anyone tries to move a corrupt file.
            with suppress(sqlite3.Error):
                connection.close()
            raise
        with self._lock:
            self._connections.add(connection)
        self._local.connection = connection
        return connection

    def _close_connections(self) -> None:
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            with suppress(sqlite3.Error):
                connection.close()
        self._local = threading.local()

    def close(self) -> None:
        self._close_connections()

    # -- assets ----------------------------------------------------------
    def get(self, asset_key: str) -> dict[str, Any] | None:
        row = self._connect().execute("SELECT * FROM assets WHERE asset_key = ?", (asset_key,)).fetchone()
        return dict(row) if row else None

    def upsert(self, values: dict[str, Any]) -> None:
        payload = {column: values.get(column) for column in ASSET_COLUMNS}
        payload["updated_at"] = payload.get("updated_at") or time.time()
        placeholders = ", ".join("?" for _ in ASSET_COLUMNS)
        updates = ", ".join(
            f"{column} = excluded.{column}" for column in ASSET_COLUMNS if column != "asset_key"
        )
        statement = (
            f"INSERT INTO assets ({', '.join(ASSET_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(asset_key) DO UPDATE SET {updates}"
        )
        connection = self._connect()
        for attempt in range(4):
            try:
                connection.execute(statement, tuple(payload[column] for column in ASSET_COLUMNS))
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def rows_for_profile(self, profile: str) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            "SELECT * FROM assets WHERE profile = ? ORDER BY project_position, asset_position",
            (profile,),
        ).fetchall()
        return [dict(row) for row in rows]

    def counts(self, profile: str) -> dict[str, int]:
        rows = self._connect().execute(
            "SELECT status, COUNT(*) AS total FROM assets WHERE profile = ? GROUP BY status",
            (profile,),
        ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def total_bytes(self, profile: str) -> int:
        row = self._connect().execute(
            "SELECT COALESCE(SUM(bytes), 0) AS total FROM assets "
            "WHERE profile = ? AND status = 'completed'",
            (profile,),
        ).fetchone()
        return int(row["total"] or 0)

    # -- project cache ---------------------------------------------------
    def get_project(self, profile: str, hash_id: str) -> dict[str, Any] | None:
        row = self._connect().execute(
            "SELECT * FROM projects WHERE profile = ? AND hash_id = ?", (profile, hash_id)
        ).fetchone()
        return dict(row) if row else None

    def upsert_project(
        self,
        *,
        profile: str,
        hash_id: str,
        position: int,
        title: str,
        updated_at: str | None,
        assets_count: int | None,
        snapshot: dict[str, Any] | None,
    ) -> None:
        self._connect().execute(
            "INSERT INTO projects "
            "(profile, hash_id, position, title, updated_at, assets_count, fetched_at, snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(profile, hash_id) DO UPDATE SET "
            "position = excluded.position, title = excluded.title, updated_at = excluded.updated_at, "
            "assets_count = excluded.assets_count, fetched_at = excluded.fetched_at, "
            "snapshot = excluded.snapshot",
            (
                profile,
                hash_id,
                position,
                title,
                updated_at,
                assets_count,
                time.time(),
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True) if snapshot is not None else None,
            ),
        )

    def get_meta(self, key: str) -> str | None:
        row = self._connect().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._connect().execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- legacy import ---------------------------------------------------
    def _import_legacy_json(self) -> None:
        legacy = self.path.with_name("state.json")
        if not legacy.is_file() or self.get_meta("legacy_state_imported"):
            return
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        records = payload.get("downloads") if isinstance(payload, dict) else None
        if not isinstance(records, dict):
            return
        imported = 0
        for key, row in records.items():
            if not isinstance(row, dict):
                continue
            values = dict(row)
            values.setdefault("asset_key", key)
            values.setdefault("profile", "")
            with suppress(sqlite3.Error, TypeError):
                self.upsert(values)
                imported += 1
        # Only remember the attempt when it produced something (or when there
        # was nothing to import), so a partially failed migration retries on
        # the next run instead of silently losing every record.
        if imported or not records:
            self.set_meta("legacy_state_imported", str(imported))
