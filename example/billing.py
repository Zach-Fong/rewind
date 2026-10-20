"""A tiny fake billing system (think Stripe): one customer table in a SQLite file, `BILLING_DB`.
This is the "real world" the agent changes. It knows nothing about Rewind."""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing

CUSTOMER = "cus_42"

SCHEMA = """
DROP TABLE IF EXISTS customers;
CREATE TABLE customers (
    id                TEXT PRIMARY KEY,
    name              TEXT,
    plan_cents        INT,
    last_charge_cents INT,
    balance_cents     INT,
    cancelled         INT,
    refunded_cents    INT
);
"""


def _db() -> sqlite3.Connection:
    path = os.environ.get("BILLING_DB", "billing.db")
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _execute(sql: str, *params) -> None:
    with closing(_db()) as conn:
        conn.execute(sql, params)


def reset() -> None:
    """One customer: Northwind Freight, $2,000/month, last charged $240."""
    with closing(_db()) as conn:
        conn.executescript(SCHEMA)
    _execute(
        "INSERT INTO customers VALUES (?, ?, ?, ?, 0, 0, 0)",
        CUSTOMER, "Northwind Freight", 200000, 24000,
    )


def get(customer_id: str) -> dict:
    with closing(_db()) as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise KeyError(f"no customer {customer_id!r}")

    customer = dict(row)
    customer["cancelled"] = bool(customer["cancelled"])
    return customer


def add_balance(customer_id: str, cents: int) -> None:
    _execute("UPDATE customers SET balance_cents = balance_cents + ? WHERE id = ?", cents, customer_id)


def set_cancelled(customer_id: str, cancelled: bool) -> None:
    _execute("UPDATE customers SET cancelled = ? WHERE id = ?", int(cancelled), customer_id)


def refund(customer_id: str, cents: int) -> None:
    _execute("UPDATE customers SET refunded_cents = refunded_cents + ? WHERE id = ?", cents, customer_id)
