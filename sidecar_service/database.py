"""Async SQLite persistence: atomic revision snapshots + active-revision tracking."""
import json
from datetime import datetime, timezone

import aiosqlite

from config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_revisions (
    discharge_id   INTEGER PRIMARY KEY,
    revision_id    INTEGER NOT NULL,
    revision_hash  TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id    TEXT PRIMARY KEY,
    discharge_id   INTEGER NOT NULL,
    revision_id    INTEGER NOT NULL,
    revision_hash  TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    payload        TEXT    NOT NULL,
    created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_dis_rev ON snapshots (discharge_id, revision_id);
"""


def make_snapshot_id(revision_hash: str, revision_id: int) -> str:
    """adb6cd43_Rev1 style id: first 8 hash chars + revision number."""
    return f"{revision_hash[:8]}_Rev{revision_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


async def get_active_revision(discharge_id: int) -> int | None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        async with db.execute(
            "SELECT revision_id FROM active_revisions WHERE discharge_id = ?",
            (discharge_id,),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def register_revision(
    discharge_id: int, revision_id: int, revision_hash: str
) -> bool:
    """Make revision_id the active one. Returns False if a NEWER revision is already active."""
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT revision_id FROM active_revisions WHERE discharge_id = ?",
            (discharge_id,),
        ) as cur:
            row = await cur.fetchone()
        if row and row[0] > revision_id:
            await db.rollback()
            return False
        await db.execute(
            "INSERT INTO active_revisions (discharge_id, revision_id, revision_hash, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(discharge_id) DO UPDATE SET "
            "revision_id=excluded.revision_id, revision_hash=excluded.revision_hash, "
            "updated_at=excluded.updated_at",
            (discharge_id, revision_id, revision_hash, _now()),
        )
        await db.commit()
    return True


async def verify_not_stale(discharge_id: int, revision_id: int) -> bool:
    """True only if revision_id is still the active revision for discharge_id."""
    return (await get_active_revision(discharge_id)) == revision_id


async def save_revision_snapshot(
    discharge_id: int,
    revision_id: int,
    revision_hash: str,
    status: str,
    payload: dict,
) -> str:
    """Atomically write the snapshot, but only if the revision is still active."""
    snapshot_id = make_snapshot_id(revision_hash, revision_id)
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT revision_id FROM active_revisions WHERE discharge_id = ?",
            (discharge_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row or row[0] != revision_id:
            await db.rollback()
            raise StaleRevisionError(discharge_id, revision_id, row[0] if row else None)
        await db.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(snapshot_id, discharge_id, revision_id, revision_hash, status, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                discharge_id,
                revision_id,
                revision_hash,
                status,
                json.dumps(payload),
                _now(),
            ),
        )
        await db.commit()
    return snapshot_id


async def clear_discharge_state(discharge_id: int) -> None:
    """Demo reset: forget active revision and snapshots for one discharge."""
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.execute("DELETE FROM active_revisions WHERE discharge_id = ?", (discharge_id,))
        await db.execute("DELETE FROM snapshots WHERE discharge_id = ?", (discharge_id,))
        await db.commit()


async def get_snapshot(discharge_id: int, revision_id: int) -> dict | None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        async with db.execute(
            "SELECT payload FROM snapshots WHERE discharge_id = ? AND revision_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (discharge_id, revision_id),
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row else None


class StaleRevisionError(Exception):
    def __init__(self, discharge_id: int, revision_id: int, active: int | None):
        super().__init__(
            f"Stale write rejected: discharge {discharge_id} Rev {revision_id} "
            f"(active Rev {active})"
        )
        self.discharge_id = discharge_id
        self.revision_id = revision_id
        self.active = active
