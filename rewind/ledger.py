"""SQLite ledger: the effects an agent's tools had in the world, and the corrections rewinds leave behind.

Lives outside Temporal (file `REWIND_DB`, default `rewind.db`), so it survives runs and resets. One connection per
call with a busy timeout, so workers, scripts and an API can share the file. The rewinds themselves are Temporal
workflows (`rewind.Rewind`), so their record is in Temporal.

Rows are keyed by workflow ID and `first_run_id`, Temporal's `first_execution_run_id`. A reset keeps it, so a rewind
sees the effects of every run that earlier rewinds made; a new workflow that reuses the workflow ID gets its own.
Continue-as-new keeps it too but starts the step numbers again, so a rewind can't reach back past one.

Effect status: pending (written before the tool runs), done, reverting (written before its undo runs), reverted,
flagged (still done in the world, and the agent is told: irreversible, or drifted), failed. `insert` is idempotent by
`effect_id`: an activity retry reuses the row and its recorded before-state.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS effects (
    effect_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, workflow_id TEXT NOT NULL, first_run_id TEXT NOT NULL,
    run_id TEXT NOT NULL, step INTEGER NOT NULL, tool TEXT NOT NULL, args_json TEXT NOT NULL, before_json TEXT,
    after_json TEXT, result_json TEXT, status TEXT NOT NULL, reason TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS effects_wf ON effects(workflow_id, first_run_id, step);
CREATE TABLE IF NOT EXISTS corrections (
    rewind_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, first_run_id TEXT NOT NULL, from_step INTEGER NOT NULL,
    text TEXT NOT NULL, created_at REAL NOT NULL
);
"""


@dataclass
class Effect:
    effect_id: str
    seq: int          # global order in which effects were recorded
    workflow_id: str
    first_run_id: str
    run_id: str
    step: int
    tool: str
    args: dict
    before: Any
    after: Any
    result: Any
    status: str       # see the module docstring
    reason: str | None
    created_at: float
    updated_at: float


def _dump(v: Any) -> str | None:
    return None if v is None else json.dumps(v, sort_keys=True)


def _load(s: str | None) -> Any:
    return None if s is None else json.loads(s)


def _effect(r: sqlite3.Row) -> Effect:
    return Effect(r["effect_id"], r["seq"], r["workflow_id"], r["first_run_id"], r["run_id"], r["step"], r["tool"],
                  _load(r["args_json"]), _load(r["before_json"]), _load(r["after_json"]), _load(r["result_json"]),
                  r["status"], r["reason"], r["created_at"], r["updated_at"])


class Ledger:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("REWIND_DB", "rewind.db")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with self._connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One transaction, holding the write lock from the start, so a read-then-write can't interleave."""
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # -- effects ------------------------------------------------------------------------------------------
    def insert(self, effect_id: str, workflow_id: str, first_run_id: str, run_id: str, step: int, tool: str,
               args: dict, before: Any) -> Effect:
        """A `pending` row, before the tool runs. A retry (same `effect_id`) keeps the existing row; a `failed` one is
        reopened."""
        now = time.time()
        with self._write() as c:
            row = c.execute("SELECT status FROM effects WHERE effect_id=?", (effect_id,)).fetchone()
            if row is None:
                seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM effects").fetchone()[0]
                c.execute("INSERT INTO effects (effect_id, seq, workflow_id, first_run_id, run_id, step, tool,"
                          " args_json, before_json, status, created_at, updated_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                          (effect_id, seq, workflow_id, first_run_id, run_id, step, tool, _dump(args), _dump(before),
                           now, now))
            elif row["status"] == "failed":
                c.execute("UPDATE effects SET status='pending', reason=NULL, updated_at=? WHERE effect_id=?",
                          (now, effect_id))
            return _effect(c.execute("SELECT * FROM effects WHERE effect_id=?", (effect_id,)).fetchone())

    def mark_done(self, effect_id: str, after: Any, result: Any, *, status: str = "done",
                  reason: str | None = None) -> None:
        """After the tool returns. An irreversible tool's effect is marked `flagged` instead of `done`."""
        with self._write() as c:
            c.execute("UPDATE effects SET after_json=?, result_json=?, status=?, reason=?, updated_at=?"
                      " WHERE effect_id=?", (_dump(after), _dump(result), status, reason, time.time(), effect_id))

    def _set_status(self, effect_id: str, status: str, reason: str | None = None) -> None:
        """`reason` is kept as it was if none is given."""
        with self._write() as c:
            c.execute("UPDATE effects SET status=?, reason=COALESCE(?, reason), updated_at=? WHERE effect_id=?",
                      (status, reason, time.time(), effect_id))

    def mark_failed(self, effect_id: str, error: str) -> None:
        self._set_status(effect_id, "failed", error)

    def mark_reverting(self, effect_id: str) -> None:
        self._set_status(effect_id, "reverting")

    def mark_reverted(self, effect_id: str) -> None:
        self._set_status(effect_id, "reverted")

    def mark_flagged(self, effect_id: str, reason: str) -> None:
        self._set_status(effect_id, "flagged", reason)

    def get_effect(self, effect_id: str) -> Effect | None:
        with self._connect() as c:
            r = c.execute("SELECT * FROM effects WHERE effect_id=?", (effect_id,)).fetchone()
        return _effect(r) if r else None

    def list_effects(self, workflow_id: str, first_run_id: str, *, from_step: int | None = None,
                     status: str | None = None, run_id: str | None = None) -> list[Effect]:
        """In the order they were recorded, across all of the workflow's runs unless `run_id` is given."""
        sql, params = "SELECT * FROM effects WHERE workflow_id=? AND first_run_id=?", [workflow_id, first_run_id]
        for column, op, value in (("step", ">=", from_step), ("status", "=", status), ("run_id", "=", run_id)):
            if value is not None:
                sql += f" AND {column}{op}?"
                params.append(value)
        with self._connect() as c:
            return [_effect(r) for r in c.execute(sql + " ORDER BY seq", params).fetchall()]

    # -- corrections --------------------------------------------------------------------------------------
    def add_correction(self, rewind_id: str, workflow_id: str, first_run_id: str, from_step: int, text: str) -> None:
        """One per rewind: saving it again (a retry) changes nothing."""
        with self._write() as c:
            c.execute("INSERT OR IGNORE INTO corrections VALUES (?,?,?,?,?,?)",
                      (rewind_id, workflow_id, first_run_id, from_step, text, time.time()))

    def corrections(self, workflow_id: str, first_run_id: str, step: int) -> list[str]:
        """Corrections that apply to `step`: every one saved for this workflow from a step at or before it."""
        with self._connect() as c:
            rows = c.execute("SELECT text FROM corrections WHERE workflow_id=? AND first_run_id=? AND from_step<=?"
                             " ORDER BY created_at", (workflow_id, first_run_id, step)).fetchall()
        return [r["text"] for r in rows]

    def clear(self) -> None:
        """Delete everything. For fresh scenarios and tests."""
        with self._write() as c:
            c.execute("DELETE FROM effects")
            c.execute("DELETE FROM corrections")
