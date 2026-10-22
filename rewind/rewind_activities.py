"""The rewind workflow's activities, in the order it runs them: the plan, an undo per effect, then saving the
correction and resetting the agent.

The plan is `rewind.preview()` from `client.py`, run on the worker. Each reversible tool gets its own undo activity,
named after it (`undo_add_credit`).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from temporalio import activity
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import ResetReapplyExcludeType
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from rewind import ids, tools
from rewind.client import RewindError, preview
from rewind.ledger import DRIFTED_REASON, Effect, Ledger


# -- 1. plan ------------------------------------------------------------------------------------------------
@activity.defn(name=ids.PREVIEW_ACTIVITY)
async def preview_activity(workflow_id: str, step: int) -> dict:
    """Run `rewind.preview()` on the worker as the rewind's first step. A refusal fails the rewind straight away,
    without retrying."""
    try:
        plan = await preview(activity.client(), workflow_id, step)
    except RewindError as e:
        raise ApplicationError(str(e), type="RewindError", non_retryable=True) from e
    return plan.to_dict()


# -- 2. undo ------------------------------------------------------------------------------------------------
def undo_activity(tool: tools.Tool):
    """Build the activity a rewind runs to undo one of this tool's effects."""

    @activity.defn(name=ids.undo_activity_name(tool.name))
    async def undo_effect(effect_id: str) -> dict:
        return await asyncio.to_thread(_check_and_undo, effect_id)

    return undo_effect


def _check_and_undo(effect_id: str) -> dict:
    """Undo one effect, or flag it if it has drifted. Returns the effect as it is afterwards, as a dict.

    It's safe to retry. The row is `reverting` while the undo runs, so a retry runs the undo again with the same undo
    key, without checking drift in a world the first attempt may already have changed. An effect in any other status
    is left alone.
    """
    ledger = Ledger()
    effect = ledger.get_effect(effect_id)
    if effect.status == "done" and tools.has_drifted(effect):
        ledger.mark_flagged(effect_id, DRIFTED_REASON)
    elif effect.status in ("done", "reverting"):
        ledger.mark_reverting(effect_id)
        undo(effect)
        ledger.mark_reverted(effect_id)
    return dataclasses.asdict(ledger.get_effect(effect_id))


def undo(effect: Effect) -> Any:
    """Call the tool's undo for this effect, with its undo key."""
    return tools.get_tool(effect.tool).undo(effect, ids.undo_key(effect.effect_id))


def undo_activities() -> list:
    """Build one undo activity per reversible tool."""
    return [undo_activity(tool) for tool in tools.TOOLS.values() if tool.kind == "reversible"]


# -- 3 and 4. save the correction, reset --------------------------------------------------------------------
@activity.defn(name=ids.RESET_ACTIVITY)
async def reset_agent(rewind_id: str, plan: dict, correction: str) -> dict:
    """Save the correction, then reset the agent workflow to the plan's reset point. `plan` is `Plan.to_dict()`.

    It's safe to retry. The correction is saved once per rewind, and the agent is reset only once.
    """
    client = activity.client()
    workflow_id = plan["workflow_id"]
    run_id = plan["run_id"]
    first_run_id = plan["first_run_id"]
    step = plan["step"]

    # Check whether an earlier attempt already reset the agent. Temporal records the new run's ID on the planned run.
    planned_run = await client.get_workflow_handle(workflow_id, run_id=run_id).describe()
    rewound_run_id = planned_run.raw_description.workflow_extended_info.reset_run_id
    already_reset = bool(rewound_run_id)

    # If it hasn't, refuse when the workflow has a newer run than the one this rewind planned on.
    if not already_reset:
        latest_run_id = (await client.get_workflow_handle(workflow_id).describe()).run_id
        if latest_run_id != run_id:
            raise ApplicationError(
                f"{workflow_id} has a newer run than the one this rewind planned on ({latest_run_id}), "
                "so it wasn't reset",
                type="RewindError",
                non_retryable=True,
            )

    # Save the correction before the reset. The rewound run starts at once, and its model call for `step` must find
    # the correction. Saving it again on a retry changes nothing.
    ledger = Ledger()
    flagged = await asyncio.to_thread(ledger.list_effects, workflow_id, first_run_id, from_step=step, status="flagged")
    text = correction_with_flagged(correction, flagged)
    await asyncio.to_thread(ledger.add_correction, rewind_id, workflow_id, first_run_id, step, text)

    # Reset, unless an earlier attempt already did.
    if not already_reset:
        rewound_run_id = await _reset(client, rewind_id, plan, correction)

    return {
        "rewound_run_id": rewound_run_id,
        "flagged": [dataclasses.asdict(effect) for effect in flagged],
        "correction": text,
    }


def correction_with_flagged(correction: str, flagged: list[Effect]) -> str:
    """Return your correction with a line for each flagged effect. The rewound agent can't see these effects in its
    history, so the lines tell it what not to repeat."""
    if not flagged:
        return correction
    lines = [correction.rstrip(), "", "Already done and NOT undone; do not repeat:"]
    for effect in flagged:
        lines.append(f"- {tools.get_tool(effect.tool).describe_effect(effect)} ({effect.reason})")
    return "\n".join(lines)


async def _reset(client: Client, rewind_id: str, plan: dict, correction: str) -> str:
    """Reset the agent workflow to the plan's reset point, and return the rewound run's ID."""
    response = await client.workflow_service.reset_workflow_execution(
        ResetWorkflowExecutionRequest(
            namespace=client.namespace,
            workflow_execution=WorkflowExecution(workflow_id=plan["workflow_id"], run_id=plan["run_id"]),
            workflow_task_finish_event_id=plan["reset_event_id"],
            # Cut short, since the correction can be any length.
            reason=f"rewind to step {plan['step']}: {correction}"[:1000],
            request_id=rewind_id,  # the same on every retry of this rewind
            # The old run's later signals and updates belong to the timeline being thrown away.
            reset_reapply_exclude_types=[
                ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_SIGNAL,
                ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_UPDATE,
            ],
        )
    )
    return response.run_id
