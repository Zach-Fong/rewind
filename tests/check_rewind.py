"""Rewind checks on a real Temporal dev server (own server, random port, throwaway DBs), using the support agent.

  A  ticket 4 rewound to step 3: step 2 kept, 3-5 undone, refund flagged, rewound run follows the correction,
     conversation rebuilt from history (steps 1-2 not re-run), other tickets untouched
  B  preview changes nothing and predicts the real rewind
  C  drift: a manual change and an already-sent reply are flagged, not overwritten
  D  repeat rewind (to 2 after 3): only `done` rows undone, corrections accumulate
  E  refusals: running workflow, unreached step, empty correction, rewind already in progress
  F  a rewind whose undo fails stops before the reset. Running it again finishes it, and a retried reset doesn't
     reset twice
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

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.converter import PayloadConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

import rewind
import testbed
from rewind import rewind_activities, tools
from rewind.ids import model_activity_id, rewind_workflow_id
from rewind.ledger import DRIFTED_REASON
from rewind.rewind_workflow import RewindWorkflow
from testbed import data, outbox
from testbed import tools as support_tools
from testbed.agent import SupportAgent, workflow_id
from testbed.stripe import FakeStripe

TQ = testbed.TASK_QUEUE
CORRECTION = "Max 10% credit. Never cancel without asking."
HERO, WF = "cus_4417", workflow_id(4)
FAILS: list[str] = []
ledger = rewind.Ledger()
logging.getLogger("temporalio.activity").setLevel(logging.ERROR)  # the refusals in E and F are expected


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


def steps(effects) -> list[tuple[int, str]]:
    return [(e.step, e.tool) for e in effects]


def predicted(plan: rewind.Plan, result: rewind.RewindResult) -> bool:
    """The plan's undos are the rewind's, in order, and what it flagged or saw drifted is what stays done."""
    same_undos = [e.effect_id for e in plan.to_undo] == [e.effect_id for e in result.undone]
    planned_flagged = sorted(e.effect_id for e in plan.flagged + plan.drifted)
    same_flagged = planned_flagged == sorted(e.effect_id for e in result.flagged)
    return same_undos and same_flagged


async def run_tickets(client: Client, tickets) -> dict[int, str]:
    """Start a fresh scenario and run the tickets to completion. Returns their run IDs."""
    for n in range(1, len(data.TICKETS) + 1):
        try:
            await client.get_workflow_handle(workflow_id(n)).terminate()
        except Exception:
            pass
    testbed.seed()
    handles = [await client.start_workflow(SupportAgent.run, n, id=workflow_id(n), task_queue=TQ) for n in tickets]
    await asyncio.gather(*(h.result() for h in handles))
    return {n: h.result_run_id for n, h in zip(tickets, handles, strict=True)}


async def model_result(client: Client, run_id: str, step: int) -> dict | None:
    """The result of `step`'s model call in this run, from history."""
    scheduled_id = None
    async for event in client.get_workflow_handle(WF, run_id=run_id).fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            if event.activity_task_scheduled_event_attributes.activity_id == model_activity_id(step):
                scheduled_id = event.event_id
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            completed = event.activity_task_completed_event_attributes
            if scheduled_id is not None and completed.scheduled_event_id == scheduled_id:
                return PayloadConverter.default.from_payloads(list(completed.result.payloads))[0]
    return None


async def activity_types(client: Client, workflow_id: str, run_id: str) -> set[str]:
    names = set()
    async for event in client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            names.add(event.activity_task_scheduled_event_attributes.activity_type.name)
    return names


async def scenario_a_b_d(client: Client) -> None:
    runs = await run_tickets(client, range(1, len(data.TICKETS) + 1))
    first = runs[4]  # ticket 4's first_execution_run_id
    before = FakeStripe().snapshot()
    rows_before = [(e.effect_id, e.status) for e in ledger.list_effects(WF, first)]
    plan = await rewind.preview(client, WF, 3)
    check(
        "B preview changes nothing",
        FakeStripe().snapshot() == before
        and [(e.effect_id, e.status) for e in ledger.list_effects(WF, first)] == rows_before,
    )
    check(
        "B preview plan: keep step 2, undo 5→3, refund flagged",
        steps(plan.kept) == [(2, "apply_discount")]
        and steps(plan.to_undo) == [(5, "send_reply"), (4, "schedule_cancel"), (3, "add_credit")]
        and steps(plan.flagged) == [(4, "refund")]
        and not plan.drifted,
    )

    rewind_1 = await rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
    outcome = await client.get_workflow_handle(WF, run_id=rewind_1.rewound_run_id).result()
    phase = await client.get_workflow_handle(rewind_workflow_id(WF)).query(RewindWorkflow.phase)
    check("A rewind workflow reports phase done", phase == "done", phase)
    check("B preview predicted the real rewind", predicted(plan, rewind_1))
    check("A new run follows the correction", outcome["status"] == "awaiting_customer", str(outcome))

    effects = ledger.list_effects(WF, first)
    old = {(e.step, e.tool): e for e in effects if e.run_id == rewind_1.original_run_id}
    new = [(e.step, e.tool, e.status) for e in effects if e.run_id == rewind_1.rewound_run_id]
    check(
        "A step 2 kept",
        old[(2, "apply_discount")].status == "done" and support_tools.lookup_customer(HERO)["discount_pct"] == 10,
    )
    check(
        "A steps 3-5 reverted, refund flagged",
        [old[k].status for k in [(3, "add_credit"), (4, "schedule_cancel"), (5, "send_reply"), (4, "refund")]]
        == ["reverted", "reverted", "reverted", "flagged"],
    )
    check(
        "A new run: capped $200 credit and a reply asking first; no cancel or refund",
        new == [(3, "add_credit", "done"), (4, "send_reply", "done")],
        str(new),
    )
    customer = support_tools.lookup_customer(HERO)
    check(
        "A Stripe: balance = new credit only, plan not cancelled",
        customer["balance_cents"] == 20000 and not customer["cancel_at_period_end"],
        str(customer),
    )
    msgs = [m["status"] for m in outbox.messages(4)]
    check("A outbox: original reply cancelled, new one scheduled", msgs == ["cancelled", "scheduled"], str(msgs))
    check(
        "A steps 1-2 not re-run (no new-run effects before step 3)",
        all(s >= 3 for s, *_ in new) and sum(1 for e in effects if e.tool == "apply_discount") == 1,
    )
    step3_model = await model_result(client, rewind_1.rewound_run_id, 3)
    check(
        "A rewound step 3 model call got the correction (in history)",
        step3_model is not None
        and step3_model["corrections"]
        and CORRECTION in step3_model["corrections"][0]
        and "refund $240" in step3_model["corrections"][0],
        str(step3_model)[:200],
    )
    step1_model = await model_result(client, rewind_1.rewound_run_id, 1)
    check(
        "A replayed step 1 kept its original result (no correction)",
        step1_model is not None and not step1_model["corrections"],
    )
    others = {n: (await client.get_workflow_handle(workflow_id(n)).describe()).run_id for n in runs if n != 4}
    check("A other tickets untouched", all(others[n] == runs[n] for n in others))
    record = await client.get_workflow_handle(rewind_workflow_id(WF)).describe()
    check(
        "A rewind recorded in Temporal", record.run_id == rewind_1.rewind_id and record.workflow_type == "rewind.Rewind"
    )
    agent_types = await activity_types(client, WF, rewind_1.rewound_run_id)
    rewind_types = await activity_types(client, rewind_workflow_id(WF), rewind_1.rewind_id)
    check(
        "A activities named after the tools and their undos",
        {"call_model", "lookup_customer", "get_incidents", "apply_discount", "add_credit", "send_reply"} == agent_types
        and {"rewind.preview", "undo_send_reply", "undo_schedule_cancel", "undo_add_credit", "rewind.reset_agent"}
        == rewind_types,
        f"{agent_types} {rewind_types}",
    )

    # D: rewind the rewound run again, to step 2
    rewind_2 = await rewind.rewind(client, WF, 2, "No discounts either.", task_queue=TQ)
    outcome_2 = await client.get_workflow_handle(WF, run_id=rewind_2.rewound_run_id).result()
    check(
        "D second rewind undoes the step 2 discount and the first rewound run's effects",
        sorted(steps(rewind_2.undone)) == [(2, "apply_discount"), (3, "add_credit"), (4, "send_reply")],
        str(steps(rewind_2.undone)),
    )
    old_rows = {(e.step, e.tool): e.status for e in ledger.list_effects(WF, first, run_id=rewind_1.original_run_id)}
    check(
        "D first run's rows: discount now reverted, the rest unchanged; refund still flagged and reported",
        old_rows
        == {
            (2, "apply_discount"): "reverted",
            (3, "add_credit"): "reverted",
            (4, "schedule_cancel"): "reverted",
            (4, "refund"): "flagged",
            (5, "send_reply"): "reverted",
        }
        and steps(rewind_2.flagged) == [(4, "refund")],
        str(old_rows),
    )
    step2_model = await model_result(client, rewind_2.rewound_run_id, 2)
    check(
        "D corrections accumulate (step 2 sees only the new one; step 3 sees both)",
        step2_model is not None
        and len(step2_model["corrections"]) == 1
        and "No discounts" in step2_model["corrections"][0],
    )
    step3_model_2 = await model_result(client, rewind_2.rewound_run_id, 3)
    check(
        "D step 3 sees both corrections",
        step3_model_2 is not None and len(step3_model_2["corrections"]) == 2,
        str(step3_model_2)[:200],
    )
    check("D run completes", outcome_2["status"] == "awaiting_customer", str(outcome_2))


async def scenario_c(client: Client) -> None:
    first = (await run_tickets(client, [4]))[4]
    FakeStripe().tamper("customers", HERO, balance_cents=1234)  # someone edited the balance by hand
    [reply] = [e for e in ledger.list_effects(WF, first) if e.tool == "send_reply"]
    outbox.deliver(reply.result["message_id"])  # and the reply already went out
    plan = await rewind.preview(client, WF, 3)
    check(
        "C preview predicts both drifted",
        sorted(steps(plan.drifted)) == [(3, "add_credit"), (5, "send_reply")],
        str(steps(plan.drifted)),
    )
    result = await rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
    await client.get_workflow_handle(WF, run_id=result.rewound_run_id).result()
    check("C preview predicted the real rewind", predicted(plan, result))
    flagged = {(e.step, e.tool): e.reason for e in result.flagged}
    check(
        "C drifted effects flagged, not undone",
        flagged.get((3, "add_credit")) == DRIFTED_REASON and flagged.get((5, "send_reply")) == DRIFTED_REASON,
        str(flagged),
    )
    new = [e.tool for e in ledger.list_effects(WF, first, run_id=result.rewound_run_id)]
    check(
        "C manual change not overwritten, and the agent (told the credit still stands) adds no second one",
        support_tools.lookup_customer(HERO)["balance_cents"] == 1234 and new == ["send_reply"],
        f"{new}",
    )
    check(
        "C drifted effects listed in the correction",
        "add_credit $3,000" in result.correction and "send_reply" in result.correction,
        result.correction,
    )
    check(
        "C reversible cancel still undone", steps(result.undone) == [(4, "schedule_cancel")], str(steps(result.undone))
    )


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
    busy = await client.start_workflow(
        RewindWorkflow.run, args=[WF, 3, "x"], id=rewind_workflow_id(WF), task_queue="nobody"
    )
    await refused(
        "E second rewind while one is in progress refused", rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
    )
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
        check(name, "billing is down" in str(e) and latest == first and not ledger.corrections(WF, first, 3), str(e))
    finally:
        add_credit.undo = undo

    result = await rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
    await client.get_workflow_handle(WF, run_id=result.rewound_run_id).result()
    check(
        "F running it again undoes only what was left, and the new run follows the correction",
        steps(result.undone) == [(3, "add_credit")] and support_tools.lookup_customer(HERO)["balance_cents"] == 20000,
        str(steps(result.undone)),
    )

    plan = {
        "workflow_id": WF,
        "run_id": result.original_run_id,
        "first_run_id": first,
        "step": 3,
        "reset_event_id": result.reset_event_id,
    }
    again = await ActivityEnvironment(client).run(rewind_activities.reset_agent, result.rewind_id, plan, CORRECTION)
    latest = (await client.get_workflow_handle(WF).describe()).run_id
    check(
        "F a retried reset returns the run it made, without resetting again or saving the correction twice",
        again["rewound_run_id"] == result.rewound_run_id == latest and len(ledger.corrections(WF, first, 3)) == 1,
    )


async def scenario_g(client: Client) -> None:
    first_a = (await run_tickets(client, [4]))[4]
    result_a = await rewind.rewind(client, WF, 3, CORRECTION, task_queue=TQ)
    await client.get_workflow_handle(WF, run_id=result_a.rewound_run_id).result()
    rows_a = [(e.effect_id, e.status) for e in ledger.list_effects(WF, first_a)]

    # the same ticket again as a new workflow: fresh Stripe, same ledger
    FakeStripe().reset_to_seed(data.customers())
    outbox.clear()
    h = await client.start_workflow(SupportAgent.run, 4, id=WF, task_queue=TQ)
    await h.result()
    first_b = h.result_run_id
    new_step3 = await model_result(client, first_b, 3)
    check(
        "G new workflow with the same ID starts with no corrections",
        new_step3 is not None and new_step3["corrections"] == [],
    )
    result_b = await rewind.rewind(
        client, WF, 3, "Max 10% credit. Never cancel without asking. Be brief.", task_queue=TQ
    )
    await client.get_workflow_handle(WF, run_id=result_b.rewound_run_id).result()
    touched = result_b.undone + result_b.flagged + result_b.kept
    check(
        "G its rewind touches only its own effects; the old workflow's rows are unchanged",
        result_b.undone
        and all(e.first_run_id == first_b for e in touched)
        and [(e.effect_id, e.status) for e in ledger.list_effects(WF, first_a)] == rows_a,
        str(steps(touched)),
    )
    rewound_step3 = await model_result(client, result_b.rewound_run_id, 3)
    check(
        "G its rewound run sees only its own correction",
        rewound_step3 is not None
        and len(rewound_step3["corrections"]) == 1
        and "Be brief" in rewound_step3["corrections"][0],
        str(rewound_step3 and rewound_step3["corrections"]),
    )


async def main() -> int:
    async with await WorkflowEnvironment.start_local() as env:
        # With no workflow cache, every workflow task replays the run from the start, so each scenario also checks
        # that the agent and the rewind replay cleanly.
        async with testbed.make_worker(env.client, max_cached_workflows=0):
            for scenario in (scenario_a_b_d, scenario_c, scenario_e, scenario_f, scenario_g):
                await scenario(env.client)
    print("\nALL PASS" if not FAILS else f"\nFAILURES: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
