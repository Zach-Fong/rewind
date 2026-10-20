"""Fake outbox: replies wait here until their send time, so a rewind can still cancel them. A table in `STRIPE_DB`.

Message status: scheduled, then sent (`deliver()`, the send time passing) or cancelled (an undo).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(os.environ.get("STRIPE_DB", "stripe.db"), timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        c.execute("CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, key TEXT UNIQUE, ticket_id INTEGER,"
                  " customer_id TEXT, body TEXT, send_at TEXT, status TEXT)")
        yield c
    finally:
        c.close()


@contextmanager
def _write() -> Iterator[sqlite3.Connection]:
    with _connect() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def schedule(ticket_id: int, customer_id: str, body: str, send_at: str, idempotency_key: str | None) -> str:
    """Returns the message ID. The same key returns the message it scheduled the first time."""
    with _write() as c:
        if idempotency_key:
            existing = c.execute("SELECT id FROM outbox WHERE key=?", (idempotency_key,)).fetchone()
            if existing:
                return existing["id"]
        message_id = f"msg_{c.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] + 1:03d}"
        c.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?)",
                  (message_id, idempotency_key, ticket_id, customer_id, body, send_at, "scheduled"))
        return message_id


def cancel(message_id: str) -> None:
    """Only a message that hasn't gone out yet."""
    with _write() as c:
        c.execute("UPDATE outbox SET status='cancelled' WHERE id=? AND status='scheduled'", (message_id,))


def deliver(message_id: str) -> None:
    """Simulate the send time passing: the reply goes out and can no longer be cancelled."""
    with _write() as c:
        c.execute("UPDATE outbox SET status='sent' WHERE id=?", (message_id,))


def messages(ticket_id: int) -> list[dict]:
    with _connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM outbox WHERE ticket_id=? ORDER BY rowid", (ticket_id,))]


def clear() -> None:
    with _write() as c:
        c.execute("DELETE FROM outbox")
