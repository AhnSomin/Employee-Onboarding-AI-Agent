"""SQLite state store for local development and tests.

Each row keeps the full model as JSON; `meeting_id` and `status` are copied
into columns for filtering. Rows come back in insertion order, matching the
Sheets backend.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..meeting.models import ActionItem, Meeting
from .base import NotFoundError, apply_update

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_items (
    item_id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_action_items_meeting ON action_items (meeting_id);
CREATE INDEX IF NOT EXISTS ix_action_items_status ON action_items (status);
"""

_UPSERT_MEETING = """
INSERT INTO meetings (meeting_id, data) VALUES (?, ?)
ON CONFLICT (meeting_id) DO UPDATE SET data = excluded.data
"""

_UPSERT_ITEM = """
INSERT INTO action_items (item_id, meeting_id, status, data) VALUES (?, ?, ?, ?)
ON CONFLICT (item_id) DO UPDATE SET
    meeting_id = excluded.meeting_id, status = excluded.status, data = excluded.data
"""


class SqliteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # executescript commits on its own, so it runs outside _transaction().
        with self._read() as conn:
            conn.executescript(_SCHEMA)

    # --- meetings ---

    def save_meeting(self, meeting: Meeting) -> None:
        with self._transaction() as conn:
            conn.execute(_UPSERT_MEETING, (meeting.meeting_id, meeting.model_dump_json()))

    def get_meeting(self, meeting_id: str) -> Meeting | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT data FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
        return Meeting.model_validate_json(row[0]) if row else None

    def list_meetings(self) -> list[Meeting]:
        with self._read() as conn:
            rows = conn.execute("SELECT data FROM meetings ORDER BY rowid").fetchall()
        return [Meeting.model_validate_json(row[0]) for row in rows]

    def update_meeting(self, meeting_id: str, **fields: Any) -> Meeting:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT data FROM meetings WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(meeting_id)
            updated = apply_update(Meeting.model_validate_json(row[0]), fields)
            conn.execute(_UPSERT_MEETING, (updated.meeting_id, updated.model_dump_json()))
        return updated

    # --- action items ---

    def upsert_items(self, items: list[ActionItem]) -> None:
        with self._transaction() as conn:
            conn.executemany(
                _UPSERT_ITEM,
                [(i.item_id, i.meeting_id, i.status, i.model_dump_json()) for i in items],
            )

    def get_item(self, item_id: str) -> ActionItem | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT data FROM action_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return ActionItem.model_validate_json(row[0]) if row else None

    def list_items(
        self, *, status: str | None = None, meeting_id: str | None = None
    ) -> list[ActionItem]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if meeting_id is not None:
            clauses.append("meeting_id = ?")
            params.append(meeting_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT data FROM action_items {where} ORDER BY rowid", params
            ).fetchall()
        return [ActionItem.model_validate_json(row[0]) for row in rows]

    def update_item(self, item_id: str, **fields: Any) -> ActionItem:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT data FROM action_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(item_id)
            updated = apply_update(ActionItem.model_validate_json(row[0]), fields)
            conn.execute(
                _UPSERT_ITEM,
                (updated.item_id, updated.meeting_id, updated.status, updated.model_dump_json()),
            )
        return updated

    # --- maintenance (scripts/reset_demo.py) ---
    # Every row in this local database was written by this app.

    def app_row_counts(self) -> dict[str, int]:
        with self._read() as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("meetings", "action_items")
            }

    def delete_app_rows(self) -> dict[str, int]:
        counts = self.app_row_counts()
        with self._transaction() as conn:
            conn.execute("DELETE FROM action_items")
            conn.execute("DELETE FROM meetings")
        return counts

    # --- connections ---
    # A fresh connection per call: Streamlit runs each session in its own thread.

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
