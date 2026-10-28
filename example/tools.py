"""The agent's tools. Rewind's decorators say whether each one can be undone, and how."""

from __future__ import annotations

import rewind  # Rewind
from example import billing


@rewind.read_only  # Rewind
def get_customer(customer_id: str) -> dict:
    return billing.get(customer_id)


# The fake billing has no idempotency keys, so these undos ignore `key`. A real undo passes it on to its API, as
# tests/testbed/tools.py does.
def _discount_state(args: dict) -> int:  # Rewind: the state apply_discount changes
    return billing.get(args["customer_id"])["discount_percent"]


def _undo_discount(effect: rewind.Effect, key: str) -> None:  # Rewind: restores the discount from before the call
    billing.set_discount(effect.args["customer_id"], effect.before)


@rewind.reversible(state=_discount_state, undo=_undo_discount)  # Rewind
def apply_discount(customer_id: str, percent: int) -> dict:
    billing.set_discount(customer_id, percent)
    return {"discount_percent": percent}


def _credit_state(args: dict) -> int:  # Rewind: the state add_credit changes
    return billing.get(args["customer_id"])["balance_cents"]


def _undo_credit(effect: rewind.Effect, key: str) -> None:  # Rewind: undoes one add_credit call
    billing.add_balance(effect.args["customer_id"], -effect.args["amount_cents"])


@rewind.reversible(state=_credit_state, undo=_undo_credit)  # Rewind
def add_credit(customer_id: str, amount_cents: int) -> dict:
    billing.add_balance(customer_id, amount_cents)
    return {"balance_cents": billing.get(customer_id)["balance_cents"]}


def _cancel_state(args: dict) -> bool:  # Rewind: the state cancel_subscription changes
    return billing.get(args["customer_id"])["cancelled"]


def _undo_cancel(effect: rewind.Effect, key: str) -> None:  # Rewind: restores the cancelled flag from before the call
    billing.set_cancelled(effect.args["customer_id"], effect.before)


@rewind.reversible(state=_cancel_state, undo=_undo_cancel)  # Rewind
def cancel_subscription(customer_id: str) -> dict:
    billing.set_cancelled(customer_id, True)
    return {"cancelled": True}


def _undo_reply(effect: rewind.Effect, key: str) -> None:  # Rewind: cancels the scheduled reply
    billing.cancel_reply(effect.result["message_id"])


@rewind.reversible(undo=_undo_reply)  # Rewind
def schedule_reply(customer_id: str, body: str) -> dict:
    return {"message_id": billing.schedule_reply(customer_id, body)}


@rewind.irreversible()  # Rewind: can't be undone
def refund(customer_id: str, amount_cents: int) -> dict:
    billing.refund(customer_id, amount_cents)
    return {"refunded_cents": amount_cents}
