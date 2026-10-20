"""Library checks without Temporal: tool recording, idempotency, undo, drift, corrections, plus the test bed's fake
Stripe semantics. Throwaway DBs. Usage: .venv/bin/python tests/check_tools.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

_tmp = tempfile.mkdtemp(prefix="rewind-tools-")
os.environ.update(STRIPE_DB=f"{_tmp}/stripe.db", REWIND_DB=f"{_tmp}/rewind.db")

import rewind  # noqa: E402
import testbed  # noqa: E402
from testbed import outbox, tools as support_tools  # noqa: E402, F401  (registers the tools)
from testbed.stripe import FakeStripe, IdempotencyError  # noqa: E402
from rewind import activities, tools  # noqa: E402

FAILS: list[str] = []
HERO = "cus_4417"
FIRST = "run-a"  # wf-1's first_execution_run_id; run-b below is a reset of it


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


def call(name: str, args: dict, key: str, step: int = 1, run: str = "run-a") -> object:
    return tools.run_and_record(name, args, step=step, workflow_id="wf-1", first_run_id=FIRST, run_id=run, key=key)


def main() -> int:
    testbed.seed()
    led = rewind.Ledger()
    stripe = FakeStripe()

    # fake Stripe idempotency (the semantics the key design depends on)
    a = stripe.create_balance_transaction(HERO, 100, "x", idempotency_key="k1")
    b = stripe.create_balance_transaction(HERO, 100, "x", idempotency_key="k1")
    check("stripe: same key + params replays, no second effect",
          a == b and stripe.retrieve_customer(HERO)["balance_cents"] == 100)
    try:
        stripe.create_balance_transaction(HERO, 999, "x", idempotency_key="k1")
        check("stripe: same key + different params raises", False)
    except IdempotencyError:
        check("stripe: same key + different params raises", True)
    testbed.seed()

    # read-only tools are not recorded
    call("lookup_customer", {"customer_id": HERO}, "wf-1:run-a:1")
    check("read-only tool not recorded", led.list_effects("wf-1", FIRST) == [])

    # reversible: row with before/after
    call("add_credit", {"customer_id": HERO, "amount_cents": 300000}, "wf-1:run-a:2", step=3)
    [e] = led.list_effects("wf-1", FIRST)
    check("reversible tool recorded done with before/after",
          e.status == "done" and e.before == {"balance_cents": 0} and e.after == {"balance_cents": 300000}
          and e.step == 3)

    # retry with the same key: one row, one Stripe effect
    call("add_credit", {"customer_id": HERO, "amount_cents": 300000}, "wf-1:run-a:2", step=3)
    check("retry: one row, one Stripe effect",
          len(led.list_effects("wf-1", FIRST)) == 1 and stripe.retrieve_customer(HERO)["balance_cents"] == 300000)

    # a new key (a reset gives the same activity a new run ID): really executes
    call("add_credit", {"customer_id": HERO, "amount_cents": 300000}, "wf-1:run-b:2", step=3, run="run-b")
    check("a new key for the same call executes again",
          len(led.list_effects("wf-1", FIRST)) == 2 and stripe.retrieve_customer(HERO)["balance_cents"] == 600000)

    # undo round trip for each reversible tool, and drift detection
    testbed.seed()
    cases = [("add_credit", {"customer_id": HERO, "amount_cents": 5000}),
             ("apply_discount", {"customer_id": HERO, "percent": 10}),
             ("schedule_cancel", {"customer_id": HERO}),
             ("send_reply", {"customer_id": HERO, "ticket_id": 4, "body": "hi"})]
    for i, (name, args) in enumerate(cases):
        before = tools.get_tool(name).state(args)
        call(name, args, f"wf-1:run-a:{10 + i}")
        e = led.list_effects("wf-1", FIRST)[-1]
        drifted = tools.has_drifted(e)
        tools.undo(e)
        after_undo = tools.get_tool(name).state(args)
        check(f"{name}: drift check passes untouched, undo restores the before-state",
              not drifted and after_undo == before, f"{before} -> {after_undo}")

    call("add_credit", {"customer_id": HERO, "amount_cents": 700}, "wf-1:run-a:20")
    e = led.list_effects("wf-1", FIRST)[-1]
    stripe.tamper("customers", HERO, balance_cents=12345)
    check("drift: a manual change is detected", tools.has_drifted(e))

    reply = call("send_reply", {"customer_id": HERO, "ticket_id": 4, "body": "later"}, "wf-1:run-a:21")
    e = led.list_effects("wf-1", FIRST)[-1]
    outbox.deliver(reply["message_id"])
    check("drift: a reply that already went out can't be cancelled", tools.has_drifted(e))

    # irreversible: recorded flagged
    call("refund", {"customer_id": HERO, "amount_cents": 24000}, "wf-1:run-a:22", step=4)
    e = led.list_effects("wf-1", FIRST)[-1]
    check("irreversible tool recorded flagged",
          e.tool == "refund" and e.status == "flagged" and e.reason == "irreversible")

    # reversible without state: no drift check, the undo always runs
    notes: list[str] = []

    @rewind.reversible(undo=lambda effect, key: notes.remove(effect.args["text"]))
    def add_note(text: str) -> None:
        notes.append(text)

    call("add_note", {"text": "x"}, "wf-1:run-a:40")
    e = led.list_effects("wf-1", FIRST)[-1]
    notes.append("someone else's")
    drifted = tools.has_drifted(e)
    tools.undo(e)
    check("reversible without state: recorded done, undone despite other changes",
          e.status == "done" and e.before is None and not drifted and notes == ["someone else's"])

    # failure and crash
    testbed.seed()
    try:
        call("refund", {"customer_id": "cus_none", "amount_cents": 100}, "wf-1:run-a:30")
    except Exception:  # noqa: BLE001
        pass
    [e] = led.list_effects("wf-1", FIRST)
    check("a tool that raises leaves a failed row", e.status == "failed", e.reason or "")
    for name, key in (("add_credit", "wf-1:run-a:31"), ("refund", "wf-1:run-a:32")):
        with mock.patch.object(rewind.Ledger, "record_result", side_effect=SystemExit("crash")):
            try:
                call(name, {"customer_id": HERO, "amount_cents": 100}, key)
            except SystemExit:
                pass
    check("a crash after the tool ran leaves a pending row, for an irreversible tool too",
          [x.status for x in led.list_effects("wf-1", FIRST)] == ["failed", "pending", "pending"])

    # an undo retried after it took effect
    testbed.seed()
    call("add_credit", {"customer_id": HERO, "amount_cents": 500}, "wf-1:run-a:50")
    [e] = led.list_effects("wf-1", FIRST)
    with mock.patch.object(rewind.Ledger, "mark_reverted", side_effect=TimeoutError("lost")):
        try:
            activities._check_and_undo(e.effect_id)
        except TimeoutError:
            pass
    retried = activities._check_and_undo(e.effect_id)
    check("undo retry after the undo took effect: reverted, undone once",
          retried["status"] == "reverted" and stripe.retrieve_customer(HERO)["balance_cents"] == 0,
          f"{retried['status']} {retried['reason']} balance={stripe.retrieve_customer(HERO)['balance_cents']}")

    # corrections apply from their step on
    led.add_correction("rw1", "wf-9", "run-a", 3, "A")
    led.add_correction("rw2", "wf-9", "run-a", 2, "B")
    led.add_correction("rw2", "wf-9", "run-a", 2, "B")  # a retried rewind saves it again: no duplicate
    check("corrections apply from their step on, in order, once per rewind",
          led.corrections("wf-9", "run-a", 1) == [] and led.corrections("wf-9", "run-a", 2) == ["B"]
          and led.corrections("wf-9", "run-a", 3) == ["A", "B"])

    # registration refuses what it can't record
    def add_credit(customer_id: str) -> None: ...

    async def lookup_async(customer_id: str) -> None: ...

    refused = []
    for register in (lambda: rewind.read_only(add_credit), lambda: rewind.read_only(lookup_async)):
        try:
            register()
        except (ValueError, TypeError) as e:
            refused.append(type(e).__name__)
    check("registering a duplicate name or an async tool is refused", refused == ["ValueError", "TypeError"],
          str(refused))

    print("\nALL PASS" if not FAILS else f"\nFAILURES: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
