"""The plan for a rewind: find step M's model call in the agent's history, and sort the workflow's effects into what a
rewind keeps, undoes and flags. `rewind.preview()` returns it; the rewind workflow makes it as its first activity.
"""
from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from rewind import tools
from rewind.ledger import Effect, Ledger
from rewind.workflow import model_activity_id


class RewindError(Exception):
    """A rewind was refused, or failed before it reset the agent."""


@dataclass
class Plan:
    """What a rewind to `step` would do."""
    workflow_id: str
    run_id: str
    first_run_id: str
    step: int
    reset_event_id: int
    kept: list[Effect] = field(default_factory=list)     # before step M: left alone
    to_undo: list[Effect] = field(default_factory=list)  # will be undone, newest first; also `reverting`
    drifted: list[Effect] = field(default_factory=list)  # drifted since the tool ran: will be flagged instead
    flagged: list[Effect] = field(default_factory=list)  # irreversible (or flagged earlier): the agent is told

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


async def preview(client: Client, workflow_id: str, step: int, *, ledger: Ledger | None = None) -> Plan:
    """What a rewind to `step` would do. Changes nothing. Drift is checked now, so it can differ at the rewind."""
    ledger = ledger or Ledger()
    run_id, first_run_id = await latest_closed_run(client, workflow_id)
    reset_event_id = await find_reset_event_id(client, workflow_id, run_id, step)
    plan = Plan(workflow_id, run_id, first_run_id, step, reset_event_id)
    # The ledger and the tools' `state()` calls block, so they run in a thread.
    await asyncio.to_thread(_sort_effects, plan, ledger)
    return plan


async def latest_closed_run(client: Client, workflow_id: str) -> tuple[str, str]:
    """The workflow's latest run: its run ID and its `first_execution_run_id`."""
    try:
        desc = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as e:
        if e.status != RPCStatusCode.NOT_FOUND:
            raise
        raise RewindError(f"cannot find workflow {workflow_id}") from e
    if desc.close_time is None:
        raise RewindError(f"{workflow_id} is still running; only finished workflows can be rewound")
    return desc.run_id, desc.raw_description.workflow_execution_info.first_run_id


async def find_reset_event_id(client: Client, workflow_id: str, run_id: str, step: int) -> int:
    """The reset point: the workflow task that scheduled step `step`'s model call (activity ID `{step}.model`). A reset
    must land on a workflow-task boundary; resetting to this one makes the model call run again."""
    async for event in client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled = event.activity_task_scheduled_event_attributes
            if scheduled.activity_id == model_activity_id(step):
                return scheduled.workflow_task_completed_event_id
    raise RewindError(f"{workflow_id} has no model call for step {step} in run {run_id}")


def _sort_effects(plan: Plan, ledger: Ledger) -> None:
    """Sort the workflow's effects into kept / to_undo / drifted / flagged. Pending, failed and reverted effects play
    no part. A `reverting` effect (an undo a crash interrupted) is undone again without a drift check."""
    for effect in ledger.list_effects(plan.workflow_id, plan.first_run_id):
        if effect.status not in ("done", "reverting", "flagged"):
            continue
        if effect.step < plan.step:
            plan.kept.append(effect)
        elif effect.status == "flagged":
            plan.flagged.append(effect)
        elif effect.status == "done" and tools.has_drifted(effect):
            plan.drifted.append(effect)
        else:
            plan.to_undo.append(effect)
    plan.to_undo.reverse()  # newest first
