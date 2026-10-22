"""The support agent's tools, registered with Rewind. Money tools go to the fake Stripe. Replies go to the fake
outbox, where they wait until their send time, so a rewind can still cancel them.

  read-only     lookup_customer, get_incidents, search_docs
  reversible    add_credit, apply_discount, schedule_cancel, send_reply
  irreversible  refund
"""

from __future__ import annotations

import rewind
from testbed import data, outbox
from testbed.stripe import FakeStripe, StripeError


def _subscription(customer_id: str) -> dict:
    subscriptions = FakeStripe().list_subscriptions(customer_id)
    if not subscriptions:
        raise StripeError(f"customer {customer_id!r} has no subscription")
    return subscriptions[0]


def _usd(cents: int) -> str:
    return f"${cents / 100:,.0f}"


# -- read-only ----------------------------------------------------------------------------------------------
@rewind.read_only
def lookup_customer(customer_id: str) -> dict:
    stripe = FakeStripe()
    customer = stripe.retrieve_customer(customer_id)
    sub = _subscription(customer_id)
    charges = stripe.list_charges(customer_id)
    return {
        "customer_id": customer_id,
        "name": customer["name"],
        "balance_cents": customer["balance_cents"],
        "plan_cents": sub["amount_cents"],
        "discount_pct": sub["discount_pct"],
        "cancel_at_period_end": sub["cancel_at_period_end"],
        "status": sub["status"],
        "last_charge_cents": charges[0]["amount_cents"] if charges else 0,
    }


@rewind.read_only
def get_incidents(customer_id: str) -> list[dict]:
    """Incidents in the last 7 days."""
    return data.INCIDENTS.get(customer_id, [])


@rewind.read_only
def search_docs(query: str) -> list[dict]:
    return [{"title": f"Help center: {query}", "url": f"https://help.example.com/search?q={query.replace(' ', '+')}"}]


# -- reversible ---------------------------------------------------------------------------------------------
def _balance(args: dict) -> dict:
    return {"balance_cents": FakeStripe().retrieve_customer(args["customer_id"])["balance_cents"]}


def _undo_credit(effect, key: str) -> dict:
    return FakeStripe().create_balance_transaction(
        effect.args["customer_id"], -effect.args["amount_cents"], "undo agent credit", idempotency_key=key
    )


@rewind.reversible(
    state=_balance,
    undo=_undo_credit,
    describe=lambda e: f"add_credit {_usd(e.args['amount_cents'])} to {e.args['customer_id']}",
)
def add_credit(
    customer_id: str, amount_cents: int, reason: str = "goodwill", idempotency_key: str | None = None
) -> dict:
    txn = FakeStripe().create_balance_transaction(customer_id, amount_cents, reason, idempotency_key=idempotency_key)
    return {"balance_transaction_id": txn["id"], "ending_balance_cents": txn["ending_balance_cents"]}


def _discount(args: dict) -> dict:
    return {"discount_pct": _subscription(args["customer_id"])["discount_pct"]}


def _undo_discount(effect, key: str) -> dict:
    return FakeStripe().set_discount(
        effect.result["subscription_id"], effect.before["discount_pct"], idempotency_key=key
    )


@rewind.reversible(
    state=_discount,
    undo=_undo_discount,
    describe=lambda e: f"apply_discount {e.args['percent']}% for {e.args['customer_id']}",
)
def apply_discount(customer_id: str, percent: int, idempotency_key: str | None = None) -> dict:
    sub = _subscription(customer_id)
    FakeStripe().set_discount(sub["id"], percent, idempotency_key=idempotency_key)
    return {"subscription_id": sub["id"], "discount_pct": percent}


def _cancel_flag(args: dict) -> dict:
    return {"cancel_at_period_end": _subscription(args["customer_id"])["cancel_at_period_end"]}


def _undo_cancel(effect, key: str) -> dict:
    return FakeStripe().set_cancel_at_period_end(
        effect.result["subscription_id"], effect.before["cancel_at_period_end"], idempotency_key=key
    )


@rewind.reversible(
    state=_cancel_flag, undo=_undo_cancel, describe=lambda e: f"schedule_cancel for {e.args['customer_id']}"
)
def schedule_cancel(customer_id: str, idempotency_key: str | None = None) -> dict:
    """Cancels at the end of the billing period."""
    sub = _subscription(customer_id)
    FakeStripe().set_cancel_at_period_end(sub["id"], True, idempotency_key=idempotency_key)
    return {"subscription_id": sub["id"], "cancel_at_period_end": True}


def _scheduled_replies(args: dict) -> dict:
    return {"scheduled": [m["id"] for m in outbox.messages(args["ticket_id"]) if m["status"] == "scheduled"]}


def _undo_reply(effect, key: str) -> dict:
    outbox.cancel(effect.result["message_id"])
    return {"message_id": effect.result["message_id"], "status": "cancelled"}


@rewind.reversible(
    state=_scheduled_replies,
    undo=_undo_reply,
    describe=lambda e: f"send_reply to ticket {e.args['ticket_id']} (sends {e.result['send_at']})",
)
def send_reply(
    customer_id: str, ticket_id: int, body: str, send_at: str = data.REPLY_AT, idempotency_key: str | None = None
) -> dict:
    """Schedules the reply. It goes out at `send_at`."""
    return {"message_id": outbox.schedule(ticket_id, customer_id, body, send_at, idempotency_key), "send_at": send_at}


# -- irreversible -------------------------------------------------------------------------------------------
@rewind.irreversible(describe=lambda e: f"refund {_usd(e.args['amount_cents'])} to {e.args['customer_id']}")
def refund(customer_id: str, amount_cents: int, idempotency_key: str | None = None) -> dict:
    stripe = FakeStripe()
    charges = stripe.list_charges(customer_id)
    if not charges:
        raise StripeError(f"customer {customer_id!r} has no charge")
    r = stripe.create_refund(charges[0]["id"], amount_cents, idempotency_key=idempotency_key)
    return {"refund_id": r["id"], "amount_cents": r["amount_cents"]}
