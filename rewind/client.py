"""The functions you call from your own code: preview a rewind, or run one and wait for it.

    plan = await rewind.preview(client, workflow_id, step)    # optional, changes nothing
    result = await rewind.rewind(client, workflow_id, step, correction, task_queue="...")

A rewind runs as the `rewind.Rewind` workflow, in `rewind_workflow.py`. Its first activity calls `preview()`.
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import FailureError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from rewind import ids, tools
from rewind.ledger import Effect, Ledger
from rewind.rewind_workflow import RewindWorkflow


class RewindError(Exception):
    """A rewind was refused, or failed before it reset the agent."""


async def preview(client: Client, workflow_id: str, step: int, *, ledger: Ledger | None = None) -> Plan:
    """Show what a rewind to `step` would do, without changing anything.

    It finds step M's model call in the agent's history, then sorts the workflow's effects into the ones a rewind
    keeps, undoes and flags. Drift is checked now, so the real rewind can differ if the world changes in between.
    """
    ledger = ledger or Ledger()
    run_id, first_run_id = await latest_closed_run(client, workflow_id)
    reset_event_id = await find_reset_event_id(client, workflow_id, run_id, step)
    plan = Plan(workflow_id, run_id, first_run_id, step, reset_event_id)
    # Reading the ledger and calling each tool's state() both block, so they run in a thread.
    await asyncio.to_thread(_sort_effects, plan, ledger)
    return plan


async def rewind(client: Client, workflow_id: str, step: int, correction: str, *, task_queue: str) -> RewindResult:
    """Run a rewind and wait for it. It runs as the `rewind.Rewind` workflow on `task_queue`, which must be the
    agent's worker, because that worker has the tools."""
    if not correction.strip():
        raise RewindError("a rewind needs a correction")

    # The workflow ID allows one rewind per agent at a time, so a second start is refused.
    try:
        handle = await client.start_workflow(
            RewindWorkflow.run,
            args=[workflow_id, step, correction],
            id=ids.rewind_workflow_id(workflow_id),
            task_queue=task_queue,
        )
    except WorkflowAlreadyStartedError:
        raise RewindError(f"{workflow_id} already has a rewind in progress") from None

    try:
        return RewindResult.from_dict(await handle.result())
    except WorkflowFailureError as e:
        # A refusal comes back wrapped: workflow failure, then activity failure, then the refusal itself.
        # Report the innermost message.
        cause = e
        while cause.__cause__ is not None:
            cause = cause.__cause__
        message = cause.message if isinstance(cause, FailureError) else str(cause)
        raise RewindError(message) from e


@dataclass
class Plan:
    """What a rewind to `step` would do."""

    workflow_id: str
    run_id: str
    first_run_id: str
    step: int
    reset_event_id: int
    kept: list[Effect] = field(default_factory=list)  # before step M, so left alone
    to_undo: list[Effect] = field(default_factory=list)  # undone newest first, including ones a crash left reverting
    drifted: list[Effect] = field(default_factory=list)  # changed by someone else, so flagged instead
    flagged: list[Effect] = field(default_factory=list)  # irreversible or flagged earlier, so the agent is told

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class RewindResult:
    """What a rewind did."""

    rewind_id: str  # the rewind workflow's run ID
    workflow_id: str
    step: int
    original_run_id: str
    rewound_run_id: str
    reset_event_id: int
    undone: list[Effect]
    flagged: list[Effect]  # from step M on and still in effect, because irreversible or drifted
    kept: list[Effect]
    correction: str  # as saved, with a line per flagged effect

    @classmethod
    def from_dict(cls, data: dict) -> RewindResult:
        kwargs = dict(data)
        # The workflow returns effects as dicts, so turn them back into Effects.
        for key in ("undone", "flagged", "kept"):
            kwargs[key] = [Effect(**effect) for effect in data[key]]
        return cls(**kwargs)


async def latest_closed_run(client: Client, workflow_id: str) -> tuple[str, str]:
    """Return the workflow's latest run ID and its `first_execution_run_id`. Raises if the workflow is missing or
    still running."""
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as e:
        if e.status != RPCStatusCode.NOT_FOUND:
            raise
        raise RewindError(f"cannot find workflow {workflow_id}") from e
    if description.close_time is None:
        raise RewindError(f"{workflow_id} is still running; only finished workflows can be rewound")
    return description.run_id, description.raw_description.workflow_execution_info.first_run_id


async def find_reset_event_id(client: Client, workflow_id: str, run_id: str, step: int) -> int:
    """Find the reset point for `step`: the workflow task that scheduled its model call (activity ID `{step}.model`).
    A reset has to land on a workflow task, and resetting to this one makes the model call run again."""
    async for event in client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled = event.activity_task_scheduled_event_attributes
            if scheduled.activity_id == ids.model_activity_id(step):
                return scheduled.workflow_task_completed_event_id
    raise RewindError(f"{workflow_id} has no model call for step {step} in run {run_id}")


def _sort_effects(plan: Plan, ledger: Ledger) -> None:
    """Sort the workflow's effects into the plan's kept, to_undo, drifted and flagged lists."""
    for effect in ledger.list_effects(plan.workflow_id, plan.first_run_id):
        # Pending, failed and reverted effects play no part in a rewind.
        if effect.status not in ("done", "reverting", "flagged"):
            continue

        # Effects before step M stay as they are.
        if effect.step < plan.step:
            plan.kept.append(effect)
        # A flagged effect can't be undone. The agent is told about it instead.
        elif effect.status == "flagged":
            plan.flagged.append(effect)
        # A done effect that has drifted is flagged, not undone.
        elif effect.status == "done" and tools.has_drifted(effect):
            plan.drifted.append(effect)
        # The rest are undone. A `reverting` effect is an undo a crash interrupted, so it runs again without a
        # drift check.
        else:
            plan.to_undo.append(effect)

    # The ledger lists effects oldest first, and they're undone newest first.
    plan.to_undo.reverse()
