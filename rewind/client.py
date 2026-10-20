"""Client side: preview and run a rewind of a finished agent workflow.

    plan = await rewind.preview(client, workflow_id, step)
    result = await rewind.rewind(client, workflow_id, step, correction, task_queue="...")

A rewind runs as the `rewind.Rewind` workflow; its steps are in `rewind.workflow.RewindWorkflow`. While it runs,
`client.get_workflow_handle(rewind_workflow_id(workflow_id)).query("phase")` says where it is.
"""
from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from rewind import tools
from rewind.ledger import Effect, Ledger
from rewind.workflow import RewindWorkflow, model_activity_id, rewind_workflow_id


class RewindError(Exception):
    pass


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


@dataclass
class RewindResult:
    rewind_id: str              # the run ID of the rewind workflow
    workflow_id: str
    step: int
    original_run_id: str
    rewound_run_id: str
    reset_event_id: int
    undone: list[Effect]
    flagged: list[Effect]       # from step M on, still done in the world (irreversible or drifted)
    kept: list[Effect]
    correction: str             # as saved: your correction plus a line per still-done effect

    @classmethod
    def from_dict(cls, d: dict) -> RewindResult:
        effects = {k: [Effect(**e) for e in d[k]] for k in ("undone", "flagged", "kept")}
        return cls(**{**d, **effects})


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
    """The workflow task that scheduled step `step`'s model call."""
    async for event in client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled = event.activity_task_scheduled_event_attributes
            if scheduled.activity_id == model_activity_id(step):
                return scheduled.workflow_task_completed_event_id
    raise RewindError(f"{workflow_id} has no model call for step {step} in run {run_id}")


async def preview(client: Client, workflow_id: str, step: int, *, ledger: Ledger | None = None) -> Plan:
    """Changes nothing. Whether an effect has drifted is read now, so it can differ at the rewind."""
    ledger = ledger or Ledger()
    run_id, first_run_id = await latest_closed_run(client, workflow_id)
    plan = Plan(workflow_id, run_id, first_run_id, step, await find_reset_event_id(client, workflow_id, run_id, step))
    # The ledger and the tools' `state()` calls block, so they run in a thread.
    await asyncio.to_thread(_sort_effects, plan, ledger)
    return plan

def _sort_effects(plan: Plan, ledger: Ledger) -> None:
    for e in ledger.list_effects(plan.workflow_id, plan.first_run_id):
        if e.status not in ("done", "reverting", "flagged"):
            continue
        if e.step < plan.step:
            plan.kept.append(e)
        elif e.status == "flagged":
            plan.flagged.append(e)
        elif e.status == "reverting" or tools.world_unchanged_since(e):
            plan.to_undo.append(e)
        else:
            plan.drifted.append(e)
    plan.to_undo.reverse()


async def rewind(client: Client, workflow_id: str, step: int, correction: str, *, task_queue: str) -> RewindResult:
    """Run the `rewind.Rewind` workflow on `task_queue` (the agent's worker, which has the tools) and wait for it."""
    if not correction.strip():
        raise RewindError("a rewind needs a correction")
    try:
        handle = await client.start_workflow(RewindWorkflow.run, args=[workflow_id, step, correction],
                                             id=rewind_workflow_id(workflow_id), task_queue=task_queue)
    except WorkflowAlreadyStartedError:
        raise RewindError(f"{workflow_id} already has a rewind in progress") from None
    try:
        return RewindResult.from_dict(await handle.result())
    except WorkflowFailureError as e:
        cause = e
        while cause.__cause__ is not None:  # workflow failure <- activity failure <- the refusal
            cause = cause.__cause__
        raise RewindError(getattr(cause, "message", str(cause))) from e
