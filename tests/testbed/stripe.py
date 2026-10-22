"""Fake Stripe: a small Stripe-shaped client backed by a SQLite file shared across processes.

Objects: customers (balance_cents, where positive means credit owed to the customer), subscriptions (amount_cents,
discount_pct, cancel_at_period_end, status), charges (amount_cents, refunded_cents), balance transactions, refunds.
Customer IDs come from the seed. Other IDs are deterministic counters (sub_001, ch_001, txn_001, re_001).

Idempotency (the semantics Rewind depends on): every mutating call takes `idempotency_key`. Same key + same operation +
same params returns the stored response without running again. The same key with a different operation or params
raises IdempotencyError. A new key runs. Key expiry is NOT modeled. `idempotency_key=None` disables the check.

Multi-process safety: a connection per call, a busy timeout, and BEGIN IMMEDIATE for writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class StripeError(Exception):
    pass


class IdempotencyError(StripeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, balance_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, discount_pct INTEGER NOT NULL,
    cancel_at_period_end INTEGER NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS charges (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, refunded_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS balance_transactions (
    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS refunds (id TEXT PRIMARY KEY, charge_id TEXT NOT NULL, amount_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY, operation TEXT NOT NULL, params TEXT NOT NULL, response TEXT NOT NULL);
"""

# The tables snapshot() dumps. Idempotency records are left out on purpose.
SNAPSHOT_TABLES = ["customers", "subscriptions", "charges", "balance_transactions", "refunds"]


class FakeStripe:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("STRIPE_DB", "stripe.db")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with self._connect() as c:
            c.executescript(SCHEMA)

    # -- plumbing ---------------------------------------------------------------------------------------------
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
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    @staticmethod
    def _next_id(c: sqlite3.Connection, prefix: str) -> str:
        row = c.execute("SELECT value FROM counters WHERE name=?", (prefix,)).fetchone()
        value = (row["value"] if row else 0) + 1
        c.execute("INSERT OR REPLACE INTO counters(name, value) VALUES (?, ?)", (prefix, value))
        return f"{prefix}_{value:03d}"

    def _mutate(self, operation: str, params: dict, key: str | None, fn: Callable[[sqlite3.Connection], dict]) -> dict:
        """Run a mutation atomically with Stripe idempotency semantics."""
        canonical = json.dumps(params, sort_keys=True)
        with self._write() as c:
            if key is not None:
                row = c.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
                if row is not None:
                    if row["operation"] != operation or row["params"] != canonical:
                        raise IdempotencyError(
                            f"Keys for idempotent requests can only be used with the same parameters they were first "
                            f"used with (key={key!r}, first used for {row['operation']} {row['params']})"
                        )
                    return json.loads(row["response"])
            response = fn(c)
            if key is not None:
                c.execute(
                    "INSERT INTO idempotency VALUES (?, ?, ?, ?)",
                    (key, operation, canonical, json.dumps(response, sort_keys=True)),
                )
            return response

    # -- retrieve ---------------------------------------------------------------------------------------------
    def retrieve_customer(self, customer_id: str) -> dict:
        with self._connect() as c:
            row = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise StripeError(f"No such customer: {customer_id!r}")
        return dict(row)

    def list_subscriptions(self, customer_id: str) -> list[dict]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM subscriptions WHERE customer_id=? ORDER BY id", (customer_id,)).fetchall()
        return [{**dict(r), "cancel_at_period_end": bool(r["cancel_at_period_end"])} for r in rows]

    def list_charges(self, customer_id: str) -> list[dict]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM charges WHERE customer_id=? ORDER BY id", (customer_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- mutations --------------------------------------------------------------------------------------------
    def create_balance_transaction(
        self, customer_id: str, amount_cents: int, description: str = "", idempotency_key: str | None = None
    ) -> dict:
        params = {"customer_id": customer_id, "amount_cents": amount_cents, "description": description}

        def run(c: sqlite3.Connection) -> dict:
            cust = c.execute("SELECT balance_cents FROM customers WHERE id=?", (customer_id,)).fetchone()
            if cust is None:
                raise StripeError(f"No such customer: {customer_id!r}")
            txn_id = self._next_id(c, "txn")
            c.execute(
                "INSERT INTO balance_transactions VALUES (?, ?, ?, ?)", (txn_id, customer_id, amount_cents, description)
            )
            balance = cust["balance_cents"] + amount_cents
            c.execute("UPDATE customers SET balance_cents=? WHERE id=?", (balance, customer_id))
            return {
                "id": txn_id,
                "customer_id": customer_id,
                "amount_cents": amount_cents,
                "description": description,
                "ending_balance_cents": balance,
            }

        return self._mutate("create_balance_transaction", params, idempotency_key, run)

    def _update_subscription(
        self, operation: str, subscription_id: str, column: str, value: int, idempotency_key: str | None
    ) -> dict:
        def run(c: sqlite3.Connection) -> dict:
            if c.execute(f"UPDATE subscriptions SET {column}=? WHERE id=?", (value, subscription_id)).rowcount == 0:
                raise StripeError(f"No such subscription: {subscription_id!r}")
            return {"id": subscription_id, column: value}

        return self._mutate(operation, {"subscription_id": subscription_id, column: value}, idempotency_key, run)

    def set_discount(self, subscription_id: str, percent: int, idempotency_key: str | None = None) -> dict:
        """Attach a percent-off coupon to the subscription (percent=0 removes it)."""
        if not 0 <= percent <= 100:
            raise StripeError("percent must be between 0 and 100")
        return self._update_subscription("set_discount", subscription_id, "discount_pct", percent, idempotency_key)

    def set_cancel_at_period_end(self, subscription_id: str, value: bool, idempotency_key: str | None = None) -> dict:
        return self._update_subscription(
            "set_cancel_at_period_end", subscription_id, "cancel_at_period_end", int(value), idempotency_key
        )

    def create_refund(self, charge_id: str, amount_cents: int, idempotency_key: str | None = None) -> dict:
        params = {"charge_id": charge_id, "amount_cents": amount_cents}

        def run(c: sqlite3.Connection) -> dict:
            ch = c.execute("SELECT * FROM charges WHERE id=?", (charge_id,)).fetchone()
            if ch is None:
                raise StripeError(f"No such charge: {charge_id!r}")
            remaining = ch["amount_cents"] - ch["refunded_cents"]
            if not 0 < amount_cents <= remaining:
                raise StripeError(f"Refund amount {amount_cents} invalid; refundable amount is {remaining}")
            re_id = self._next_id(c, "re")
            c.execute("INSERT INTO refunds VALUES (?, ?, ?)", (re_id, charge_id, amount_cents))
            c.execute("UPDATE charges SET refunded_cents=refunded_cents+? WHERE id=?", (amount_cents, charge_id))
            return {"id": re_id, "charge_id": charge_id, "amount_cents": amount_cents}

        return self._mutate("create_refund", params, idempotency_key, run)

    # -- test hooks -------------------------------------------------------------------------------------------
    def reset_to_seed(self, customers: list[tuple[str, str, int, int]]) -> None:
        """Wipe everything and create `customers`: (customer_id, name, plan_cents, last_charge_cents), each with an
        active subscription and one paid charge."""
        with self._write() as c:
            for table in ["counters", "idempotency", *SNAPSHOT_TABLES]:
                c.execute(f"DELETE FROM {table}")
            for cus, name, plan_cents, charge_cents in customers:
                c.execute(
                    "INSERT INTO customers VALUES (?, ?, ?, 0)", (cus, name, f"{name.split()[0].lower()}@example.com")
                )
                c.execute(
                    "INSERT INTO subscriptions VALUES (?, ?, ?, 0, 0, 'active')",
                    (self._next_id(c, "sub"), cus, plan_cents),
                )
                c.execute("INSERT INTO charges VALUES (?, ?, ?, 0)", (self._next_id(c, "ch"), cus, charge_cents))

    def snapshot(self) -> dict[str, Any]:
        """Full, deterministic dump of Stripe objects (idempotency records excluded)."""
        with self._connect() as c:
            return {t: [dict(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY id")] for t in SNAPSHOT_TABLES}

    def tamper(self, table: str, id_: str, **fields: Any) -> None:
        """Simulate a manual change made outside the agent (bypasses idempotency)."""
        if table not in SNAPSHOT_TABLES:
            raise StripeError(f"Cannot tamper with {table!r}")
        for name in fields:
            if not name.isidentifier():
                raise StripeError(f"bad field {name!r}")
        assignments = ", ".join(f"{k}=?" for k in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        with self._write() as c:
            if c.execute(f"UPDATE {table} SET {assignments} WHERE id=?", (*values, id_)).rowcount == 0:
                raise StripeError(f"No such row {table}/{id_}")
