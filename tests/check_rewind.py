"""Rewind checks on a real Temporal dev server (own server, random port, throwaway DBs), using the support agent.

  A  ticket 4 rewound to step 3: step 2 kept, 3-5 undone, refund flagged, rewound run follows the correction,
     conversation rebuilt from history (steps 1-2 not re-run), other tickets untouched
  B  preview changes nothing and predicts the real rewind
  C  drift: a manual change and an already-sent reply are flagged, not overwritten
  D  repeat rewind (to 2 after 3): only `done` rows undone, corrections accumulate
  E  refusals: running workflow, unreached step, empty correction, rewind already in progress
  F  a rewind whose undo fails stops before the reset; running it again finishes it; a retried reset doesn't reset twice
  G  a new workflow that reuses the workflow ID: its rewind sees none of the old one's effects or corrections

Usage: .venv/bin/python tests/check_rewind.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="rewind-check-")
os.environ.update(STRIPE_DB=f"{_tmp}/stripe.db", REWIND_DB=f"{_tmp}/rewind.db", MODEL_DELAY_MS="20")

from temporalio.api.enums.v1 import EventType  # noqa: E402
from temporalio.client import Client  # noqa: E402
from temporalio.converter import PayloadConverter  # noqa: E402
from temporalio.exceptions import ApplicationError  # noqa: E402
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

import rewind  # noqa: E402
import testbed  # noqa: E402
from testbed import data, outbox, tools as t  # noqa: E402
from testbed.stripe import FakeStripe  # noqa: E402
from testbed.agent import SupportAgent, workflow_id  # noqa: E402
from rewind import activities, tools  # noqa: E402
from rewind.workflow import RewindWorkflow, model_activity_id, rewind_workflow_id  # noqa: E402

TQ = testbed.TASK_QUEUE
CORRECTION = "Max 10% credit. Never cancel without asking."
HERO, WF = "cus_4417", workflow_id(4)
FAILS: list[str] = []
led = rewind.Ledger()
logging.getLogger("temporalio.activity").setLevel(logging.ERROR)  # the refusals in E and F are expected


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


def steps(effects) -> list[tuple[int, str]]:
    return [(e.step, e.tool) for e in effects]


def predicted(plan: rewind.Plan, r: rewind.RewindResult) -> bool:
    """The plan's undos are the rewind's, in order, and what it flagged or saw drifted is what stays done."""
    return ([e.effect_id for e in plan.to_undo] == [e.effect_id for e in r.undone]
            and sorted(e.effect_id for e in plan.flagged + plan.drifted)
            == sorted(e.effect_id for e in r.flagged))


async def run_tickets(client: Client, tickets) -> dict[int, str]:
    """Fresh scenario; run the tickets to completion. Returns their run IDs."""
    for n in range(1, 17):
        try:
            await client.get_workflow_handle(workflow_id(n)).terminate()
        except Exception:  # noqa: BLE001
            pass
    testbed.seed()
    handles = [await client.start_workflow(SupportAgent.run, n, id=workflow_id(n), task_queue=TQ)
               for n in tickets]
    await asyncio.gather(*(h.result() for h in handles))
    return {n: h.result_run_id for n, h in zip(tickets, handles, strict=True)}


async def model_result(client: Client, run_id: str, step: int) -> dict | None:
    """The result of `step`'s model call in this run, from history."""
    sched = None
    async for e in client.get_workflow_handle(WF, run_id=run_id).fetch_history_events():
        if (e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
                and e.activity_task_scheduled_event_attributes.activity_id == model_activity_id(step)):
            sched = e.event_id
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED and sched is not None \
                and e.activity_task_completed_event_attributes.scheduled_event_id == sched:
            payloads = e.activity_task_completed_event_attributes.result.payloads
            return PayloadConverter.default.from_payloads(list(payloads))[0]
    return None


async def activity_types(client: Client, workflow_id: str, run_id: str) -> set[str]:
    return {e.activity_task_scheduled_event_attributes.activity_type.name
            async for e in client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history_events()
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED}


async def scenario_a_b(client: Client) -> None:
    runs = await run_tickets(client, range(1, 17))
    first = runs[4]  # ticket 4's first_execution_run_id
    before = FakeStripe().snapshot()
    rows_before = [(e.effect_id, e.status) for e in led.list_effects(WF, first)]
    plan = await rewind.preview(client, WF, 3)
    check("B preview changes nothing", FakeStripe().snapshot() == before
          and [(e.effect_id, e.status) for e in led.list_effects(WF, first)] == rows_before)
    check("B preview plan: keep step 2, undo 5→3, refund flagged",
          steps(plan.kept) == [(2, "apply_discount")]
          and steps(plan.to_undo) == [(5, "send_reply"), (4, "schedule_cancel"), (3, "add_credit")]
          and steps(plan.flagged) == [(4, "refund")] and not plan.drifted)

    r = await rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
    result = await client.get_workflow_handle(WF, run_id=r.rewound_run_id).result()
    phase = await client.get_workflow_handle(rewind_workflow_id(WF)).query(RewindWorkflow.phase)
    check("A rewind workflow reports phase done", phase == "done", phase)
    check("B preview predicted the real rewind", predicted(plan, r))
    check("A new run follows the correction", result["status"] == "awaiting_customer", str(result))

    effs = led.list_effects(WF, first)
    old = {(e.step, e.tool): e for e in effs if e.run_id == r.original_run_id}
    new = [(e.step, e.tool, e.status) for e in effs if e.run_id == r.rewound_run_id]
    check("A step 2 kept", old[(2, "apply_discount")].status == "done"
          and t.lookup_customer(HERO)["discount_pct"] == 10)
    check("A steps 3-5 reverted, refund flagged",
          [old[k].status for k in [(3, "add_credit"), (4, "schedule_cancel"), (5, "send_reply"), (4, "refund")]]
          == ["reverted", "reverted", "reverted", "flagged"])
    check("A new run: capped $200 credit and a reply asking first; no cancel or refund",
          new == [(3, "add_credit", "done"), (4, "send_reply", "done")], str(new))
    c = t.lookup_customer(HERO)
    check("A Stripe: balance = new credit only, plan not cancelled",
          c["balance_cents"] == 20000 and not c["cancel_at_period_end"], str(c))
    msgs = [m["status"] for m in outbox.messages(4)]
    check("A outbox: original reply cancelled, new one scheduled", msgs == ["cancelled", "scheduled"], str(msgs))
    check("A steps 1-2 not re-run (no new-run effects before step 3)",
          all(s >= 3 for s, *_ in new) and sum(1 for e in effs if e.tool == "apply_discount") == 1)
    m3 = await model_result(client, r.rewound_run_id, 3)
    check("A rewound step 3 model call got the correction (in history)",
          m3 is not None and m3["corrections"] and CORRECTION in m3["corrections"][0]
          and "refund $240" in m3["corrections"][0], str(m3)[:200])
    m1 = await model_result(client, r.rewound_run_id, 1)
    check("A replayed step 1 kept its original result (no correction)", m1 is not None and not m1["corrections"])
    others = {n: (await client.get_workflow_handle(workflow_id(n)).describe()).run_id for n in runs if n != 4}
    check("A other tickets untouched", all(others[n] == runs[n] for n in others))
    record = await client.get_workflow_handle(rewind_workflow_id(WF)).describe()
    check("A rewind recorded in Temporal", record.run_id == r.rewind_id and record.workflow_type == "rewind.Rewind")
    agent_types = await activity_types(client, WF, r.rewound_run_id)
    rewind_types = await activity_types(client, rewind_workflow_id(WF), r.rewind_id)
    check("A activities named after the tools and their undos",
          {"call_model", "lookup_customer", "get_incidents", "apply_discount", "add_credit", "send_reply"}
          == agent_types
          and {"rewind.plan", "undo_send_reply", "undo_schedule_cancel", "undo_add_credit",
               "rewind.reset_agent"} == rewind_types, f"{agent_types} {rewind_types}")

    # D: rewind the rewound run again, to step 2
    r2 = await rewind.rewind(client, WF, 2, "No discounts either.", task_queue=TQ)
    result2 = await client.get_workflow_handle(WF, run_id=r2.rewound_run_id).result()
    check("D second rewind undoes the step 2 discount and the first rewound run's effects",
          sorted(steps(r2.undone)) == [(2, "apply_discount"), (3, "add_credit"), (4, "send_reply")],
          str(steps(r2.undone)))
    old_rows = {(e.step, e.tool): e.status for e in led.list_effects(WF, first, run_id=r.original_run_id)}
    check("D first run's rows: discount now reverted, the rest unchanged; refund still flagged and reported",
          old_rows == {(2, "apply_discount"): "reverted", (3, "add_credit"): "reverted",
                       (4, "schedule_cancel"): "reverted", (4, "refund"): "flagged", (5, "send_reply"): "reverted"}
          and steps(r2.flagged) == [(4, "refund")], str(old_rows))
    m2 = await model_result(client, r2.rewound_run_id, 2)
    check("D corrections accumulate (step 2 sees only the new one; step 3 sees both)",
          m2 is not None and len(m2["corrections"]) == 1 and "No discounts" in m2["corrections"][0])
    m3b = await model_result(client, r2.rewound_run_id, 3)
    check("D step 3 sees both corrections", m3b is not None and len(m3b["corrections"]) == 2, str(m3b)[:200])
    check("D run completes", result2["status"] == "awaiting_customer", str(result2))


async def scenario_c(client: Client) -> None:
    first = (await run_tickets(client, [4]))[4]
    FakeStripe().tamper("customers", HERO, balance_cents=1234)  # someone edited the balance by hand
    [reply] = [e for e in led.list_effects(WF, first) if e.tool == "send_reply"]
    outbox.deliver(reply.result["message_id"])                       # and the reply already went out
    plan = await rewind.preview(client, WF, 3)
    check("C preview predicts both drifted", sorted(steps(plan.drifted)) == [(3, "add_credit"), (5, "send_reply")],
          str(steps(plan.drifted)))
    r = await rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
    await client.get_workflow_handle(WF, run_id=r.rewound_run_id).result()
    check("C preview predicted the real rewind", predicted(plan, r))
    flagged = {(e.step, e.tool): e.reason for e in r.flagged}
    check("C drifted effects flagged, not undone", flagged.get((3, "add_credit")) == activities.DRIFTED
          and flagged.get((5, "send_reply")) == activities.DRIFTED, str(flagged))
    new = [e.tool for e in led.list_effects(WF, first, run_id=r.rewound_run_id)]
    check("C manual change not overwritten, and the agent (told the credit still stands) adds no second one",
          t.lookup_customer(HERO)["balance_cents"] == 1234 and new == ["send_reply"], f"{new}")
    check("C drifted effects listed in the correction",
          "add_credit $3,000" in r.correction and "send_reply" in r.correction, r.correction)
    check("C reversible cancel still undone", steps(r.undone) == [(4, "schedule_cancel")], str(steps(r.undone)))


async def scenario_e(client: Client) -> None:
    await run_tickets(client, [4])
    snap = FakeStripe().snapshot()

    async def refused(name: str, coro) -> None:
        try:
            await coro
            check(name, False, "no error")
        except rewind.RewindError as e:
            check(name, FakeStripe().snapshot() == snap, str(e))

    await refused("E unreached step refused", rewind.rewind(client, WF, 9, CORRECTION, task_queue=TQ))
    await refused("E empty correction refused", rewind.rewind(client, WF, 3, "  ", task_queue=TQ))
    # a rewind that can't progress (no worker on its queue) holds the workflow ID
    busy = await client.start_workflow(RewindWorkflow.run, args=[WF, 3, "x"], id=rewind_workflow_id(WF),
                                       task_queue="nobody")
    await refused("E second rewind while one is in progress refused",
                  rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ))
    await busy.terminate()
    os.environ["MODEL_DELAY_MS"] = "1000"
    h = await client.start_workflow(SupportAgent.run, 5, id=workflow_id(5), task_queue=TQ)
    await refused("E running workflow refused", rewind.rewind(client, workflow_id(5), 1, CORRECTION, task_queue=TQ))
    await h.result()  # let it finish: terminating it mid-call makes the worker log a warning
    os.environ["MODEL_DELAY_MS"] = "20"


async def scenario_f(client: Client) -> None:
    first = (await run_tickets(client, [4]))[4]
    add_credit = tools.get_tool("add_credit")
    undo = add_credit.undo

    def failing_undo(effect, key):
        raise ApplicationError("billing is down", non_retryable=True)

    name = "F a rewind whose undo fails stops before the reset, with no correction saved"
    add_credit.undo = failing_undo
    try:
        await rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
        check(name, False, "no error")
    except rewind.RewindError as e:
        latest = (await client.get_workflow_handle(WF).describe()).run_id
        check(name, "billing is down" in str(e) and latest == first and not led.corrections(WF, first, 3), str(e))
    finally:
        add_credit.undo = undo

    r = await rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
    await client.get_workflow_handle(WF, run_id=r.rewound_run_id).result()
    check("F running it again undoes only what was left, and the new run follows the correction",
          steps(r.undone) == [(3, "add_credit")] and t.lookup_customer(HERO)["balance_cents"] == 20000,
          str(steps(r.undone)))

    plan = {"workflow_id": WF, "run_id": r.original_run_id, "first_run_id": first, "step": 3,
            "reset_event_id": r.reset_event_id}
    again = await ActivityEnvironment(client).run(activities.reset_agent, r.rewind_id, plan, CORRECTION)
    latest = (await client.get_workflow_handle(WF).describe()).run_id
    check("F a retried reset returns the run it made, without resetting again or saving the correction twice",
          again["rewound_run_id"] == r.rewound_run_id == latest and len(led.corrections(WF, first, 3)) == 1)


async def scenario_g(client: Client) -> None:
    first_a = (await run_tickets(client, [4]))[4]
    r = await rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
    await client.get_workflow_handle(WF, run_id=r.rewound_run_id).result()
    rows_a = [(e.effect_id, e.status) for e in led.list_effects(WF, first_a)]

    # the same ticket again as a new workflow: fresh Stripe, same ledger
    FakeStripe().reset_to_seed(data.customers())
    outbox.clear()
    h = await client.start_workflow(SupportAgent.run, 4, id=WF, task_queue=TQ)
    await h.result()
    first_b = h.result_run_id
    m3 = await model_result(client, first_b, 3)
    check("G new workflow with the same ID starts with no corrections", m3 is not None and m3["corrections"] == [])
    r2 = await rewind.rewind(client, WF, 3, "Max 10% credit. Never cancel without asking. Be brief.", task_queue=TQ)
    await client.get_workflow_handle(WF, run_id=r2.rewound_run_id).result()
    touched = r2.undone + r2.flagged + r2.kept
    check("G its rewind touches only its own effects; the old workflow's rows are unchanged",
          r2.undone and all(e.first_run_id == first_b for e in touched)
          and [(e.effect_id, e.status) for e in led.list_effects(WF, first_a)] == rows_a, str(steps(touched)))
    m3b = await model_result(client, r2.rewound_run_id, 3)
    check("G its rewound run sees only its own correction", m3b is not None and len(m3b["corrections"]) == 1
          and "Be brief" in m3b["corrections"][0], str(m3b and m3b["corrections"]))


async def main() -> int:
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=TQ, workflows=testbed.WORKFLOWS, activities=testbed.ACTIVITIES):
            for s in (scenario_a_b, scenario_c, scenario_e, scenario_f, scenario_g):
                await s(env.client)
    print("\nALL PASS" if not FAILS else f"\nFAILURES: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
