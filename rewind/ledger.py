"""The ledger: a SQLite file that records each effect an agent's tools had on the world, and each correction a
rewind saves. It lives outside Temporal (at `REWIND_DB`, default `rewind.db`), so it survives runs and resets.

Rows are keyed by the workflow ID and `first_run_id` (Temporal's `first_execution_run_id`). A reset keeps
`first_run_id`, so a rewind sees the effects of every earlier run. A new workflow that reuses the workflow ID gets a
new one and starts empty.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    effect_id    TEXT PRIMARY KEY,
    seq          INTEGER NOT NULL,
    workflow_id  TEXT NOT NULL,
    first_run_id TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    step         INTEGER NOT NULL,
    tool         TEXT NOT NULL,
    args_json    TEXT NOT NULL,
    before_json  TEXT,
    after_json   TEXT,
    result_json  TEXT,
    status       TEXT NOT NULL,
    reason       TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS effects_wf ON effects(workflow_id, first_run_id, step);
CREATE TABLE IF NOT EXISTS corrections (
    rewind_id    TEXT PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    first_run_id TEXT NOT NULL,
    from_step    INTEGER NOT NULL,
    text         TEXT NOT NULL,
    created_at   REAL NOT NULL
);
"""

# Why an effect was flagged. Stored in the reason column.
IRREVERSIBLE_REASON = "irreversible"  # an irreversible tool finished
DRIFTED_REASON = "drifted"  # a rewind found someone else had changed the state since


@dataclass
class Effect:
    """One call to a reversible or irreversible tool.

    `status` is `pending` before the tool runs. It then becomes `done`, `failed`, or `flagged` for an irreversible
    tool. A rewind moves a `done` effect to `reverting` and then `reverted`, or to `flagged` if it has drifted.
    """

    effect_id: str
    seq: int  # the order effects were recorded in, across all workflows
    workflow_id: str
    first_run_id: str
    run_id: str
    step: int
    tool: str
    args: dict
    before: Any
    after: Any
    result: Any
    status: str
    reason: str | None
    created_at: float
    updated_at: float


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def _load(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _effect(row: sqlite3.Row) -> Effect:
    return Effect(
        effect_id=row["effect_id"],
        seq=row["seq"],
        workflow_id=row["workflow_id"],
        first_run_id=row["first_run_id"],
        run_id=row["run_id"],
        step=row["step"],
        tool=row["tool"],
        args=_load(row["args_json"]),
        before=_load(row["before_json"]),
        after=_load(row["after_json"]),
        result=_load(row["result_json"]),
        status=row["status"],
        reason=row["reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Ledger:
    def __init__(self, path: str | None = None):
        # Creating a Ledger doesn't touch its SQLite file. Async activities create one on the event loop, where
        # blocking I/O would stall the worker.
        self.path = path or os.environ.get("REWIND_DB", "rewind.db")
        self._has_schema = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a connection to the SQLite file at `self.path`, with a busy timeout so the worker and your scripts
        can share it. The first connection also creates the file, its folder and its tables."""
        if not self._has_schema:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if not self._has_schema:
                conn.executescript(SCHEMA)
                self._has_schema = True
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Open a transaction that takes the write lock at once. That way a read followed by a write can't interleave
        with another writer."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # -- effects ------------------------------------------------------------------------------------------
    def insert_pending(
        self,
        effect_id: str,
        workflow_id: str,
        first_run_id: str,
        run_id: str,
        step: int,
        tool: str,
        args: dict,
        before: Any,
    ) -> Effect:
        """Write a `pending` row before the tool runs, and return it. A retry has the same `effect_id`, so it keeps
        the existing row. A `failed` row goes back to `pending`."""
        now = time.time()
        with self._write() as conn:
            row = conn.execute("SELECT status FROM effects WHERE effect_id=?", (effect_id,)).fetchone()
            if row is None:
                seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM effects").fetchone()[0]
                conn.execute(
                    "INSERT INTO effects (effect_id, seq, workflow_id, first_run_id, run_id, step, tool,"
                    " args_json, before_json, status, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                    (
                        effect_id,
                        seq,
                        workflow_id,
                        first_run_id,
                        run_id,
                        step,
                        tool,
                        _dump(args),
                        _dump(before),
                        now,
                        now,
                    ),
                )
            elif row["status"] == "failed":
                conn.execute(
                    "UPDATE effects SET status='pending', reason=NULL, updated_at=? WHERE effect_id=?",
                    (now, effect_id),
                )
            return _effect(conn.execute("SELECT * FROM effects WHERE effect_id=?", (effect_id,)).fetchone())

    def record_result(self, effect_id: str, after: Any, result: Any, *, status: str, reason: str | None = None) -> None:
        """Save what the tool returned. The row becomes `done`, or `flagged` for an irreversible tool."""
        with self._write() as conn:
            conn.execute(
                "UPDATE effects SET after_json=?, result_json=?, status=?, reason=?, updated_at=? WHERE effect_id=?",
                (_dump(after), _dump(result), status, reason, time.time(), effect_id),
            )

    def _set_status(self, effect_id: str, status: str, reason: str | None = None) -> None:
        """Change an effect's status. The existing `reason` is kept unless a new one is given."""
        with self._write() as conn:
            conn.execute(
                "UPDATE effects SET status=?, reason=COALESCE(?, reason), updated_at=? WHERE effect_id=?",
                (status, reason, time.time(), effect_id),
            )

    def mark_failed(self, effect_id: str, error: str) -> None:
        """Mark a `pending` row as `failed`. A row that is already `done` or `flagged` stays as it is. An earlier
        attempt finished, so the effect really happened, and a rewind still has to undo or report it."""
        with self._write() as conn:
            conn.execute(
                "UPDATE effects SET status='failed', reason=?, updated_at=? WHERE effect_id=? AND status='pending'",
                (error, time.time(), effect_id),
            )

    def mark_reverting(self, effect_id: str) -> None:
        self._set_status(effect_id, "reverting")

    def mark_reverted(self, effect_id: str) -> None:
        self._set_status(effect_id, "reverted")

    def mark_flagged(self, effect_id: str, reason: str) -> None:
        self._set_status(effect_id, "flagged", reason)

    def get_effect(self, effect_id: str) -> Effect | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM effects WHERE effect_id=?", (effect_id,)).fetchone()
        return _effect(row) if row else None

    def list_effects(
        self,
        workflow_id: str,
        first_run_id: str,
        *,
        from_step: int | None = None,
        status: str | None = None,
        run_id: str | None = None,
    ) -> list[Effect]:
        """Return the workflow's effects in the order they were recorded. They come from all of its runs unless
        `run_id` is given."""
        sql = "SELECT * FROM effects WHERE workflow_id=? AND first_run_id=?"
        params: list[Any] = [workflow_id, first_run_id]
        if from_step is not None:
            sql += " AND step>=?"
            params.append(from_step)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        if run_id is not None:
            sql += " AND run_id=?"
            params.append(run_id)
        with self._connect() as conn:
            return [_effect(row) for row in conn.execute(sql + " ORDER BY seq", params).fetchall()]

    # -- corrections --------------------------------------------------------------------------------------
    def add_correction(self, rewind_id: str, workflow_id: str, first_run_id: str, from_step: int, text: str) -> None:
        """Save a rewind's correction. There's one per rewind, so saving it again on a retry changes nothing."""
        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO corrections VALUES (?,?,?,?,?,?)",
                (rewind_id, workflow_id, first_run_id, from_step, text, time.time()),
            )

    def corrections(self, workflow_id: str, first_run_id: str, step: int) -> list[str]:
        """Return the corrections that apply to `step`, oldest first. These are all the ones saved for this workflow
        at `step` or an earlier step."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT text FROM corrections WHERE workflow_id=? AND first_run_id=? AND from_step<=?"
                " ORDER BY created_at",
                (workflow_id, first_run_id, step),
            ).fetchall()
        return [row["text"] for row in rows]

    def clear(self) -> None:
        """Delete every effect and correction, to start a scenario or a test from scratch."""
        with self._write() as conn:
            conn.execute("DELETE FROM effects")
            conn.execute("DELETE FROM corrections")
