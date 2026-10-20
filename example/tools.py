"""The agent's tools. Rewind's decorators say whether each one can be undone, and how."""
from __future__ import annotations

import rewind  # Rewind
from example import billing


@rewind.read_only  # Rewind
def get_customer(customer_id: str) -> dict:
    return billing.get(customer_id)


def _balance(args: dict) -> int:  # Rewind: the state add_credit changes
    return billing.get(args["customer_id"])["balance_cents"]


# The fake billing has no idempotency keys, so these undos ignore `key`; tests/testbed/tools.py passes it on.
def _undo_credit(effect, key: str) -> None:  # Rewind
    billing.add_balance(effect.args["customer_id"], -effect.args["amount_cents"])


@rewind.reversible(state=_balance, undo=_undo_credit)  # Rewind
def add_credit(customer_id: str, amount_cents: int) -> dict:
    billing.add_balance(customer_id, amount_cents)
    return {"balance_cents": billing.get(customer_id)["balance_cents"]}


def _cancelled(args: dict) -> bool:  # Rewind: the state cancel_subscription changes
    return billing.get(args["customer_id"])["cancelled"]


def _undo_cancel(effect, key: str) -> None:  # Rewind
    billing.set_cancelled(effect.args["customer_id"], effect.before)


@rewind.reversible(state=_cancelled, undo=_undo_cancel)  # Rewind
def cancel_subscription(customer_id: str) -> dict:
    billing.set_cancelled(customer_id, True)
    return {"cancelled": True}


@rewind.irreversible()  # Rewind: can't be undone
def refund(customer_id: str, amount_cents: int) -> dict:
    billing.refund(customer_id, amount_cents)
    return {"refunded_cents": amount_cents}
