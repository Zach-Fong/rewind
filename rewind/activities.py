"""Worker-side pieces: the activities behind `tool_calls` and the rewind workflow, and `corrections()` for the agent's
model activity.

Each registered tool gets an activity named after it (`add_credit`), and each reversible tool an undo activity
(`undo_add_credit`), so the Temporal UI shows the tool's name. Register `rewind.ACTIVITIES` on the worker that runs
your agent, after your tools are imported.
"""
from __future__ import annotations

import asyncio
import dataclasses
import uuid
from typing import Any

from temporalio import activity
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import ResetReapplyExcludeType
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from rewind import tools
from rewind.ledger import Effect, Ledger
from rewind.plan import RewindError, preview
from rewind.workflow import undo_activity_name

DRIFTED_REASON = "drifted"


def tool_activity(tool: tools.Tool):
    """The activity `tool_calls` runs for this tool: runs it and records it."""
    @activity.defn(name=tool.name)
    async def run_tool(args: dict, step: int, first_run_id: str) -> Any:
        info = activity.info()
        key = f"{info.workflow_id}:{info.workflow_run_id}:{info.activity_id}"
        # Tools are blocking calls; a thread keeps the worker's event loop free.
        return await asyncio.to_thread(
            tools.run_and_record, tool.name, args,
            step=step, workflow_id=info.workflow_id, first_run_id=first_run_id, run_id=info.workflow_run_id, key=key,
        )
    return run_tool


def undo_activity(tool: tools.Tool):
    """The activity a rewind runs to undo one of this tool's effects."""
    @activity.defn(name=undo_activity_name(tool.name))
    async def undo_effect(effect_id: str) -> dict:
        return await asyncio.to_thread(_check_and_undo, effect_id)
    return undo_effect


def _check_and_undo(effect_id: str) -> dict:
    """Undo one effect, or flag it if it has drifted. Returns the effect afterwards, as a dict.

    Safe to retry: the row is `reverting` while the undo runs, so a retry runs the undo again under the same undo key
    instead of checking a world the first attempt already changed. Effects in any other status are left alone."""
    ledger = Ledger()
    effect = ledger.get_effect(effect_id)
    if effect.status == "done" and tools.has_drifted(effect):
        ledger.mark_flagged(effect_id, DRIFTED_REASON)
    elif effect.status in ("done", "reverting"):
        ledger.mark_reverting(effect_id)
        tools.undo(effect)
        ledger.mark_reverted(effect_id)
    return dataclasses.asdict(ledger.get_effect(effect_id))


def tool_activities() -> list:
    """One activity per registered tool, plus an undo activity per reversible tool."""
    if not tools.TOOLS:
        raise RuntimeError("no tools registered: import your tools before reading rewind.ACTIVITIES")
    result = []
    for tool in tools.TOOLS.values():
        result.append(tool_activity(tool))
        if tool.kind == "reversible":
            result.append(undo_activity(tool))
    return result


@activity.defn(name="rewind.preview")
async def preview_activity(workflow_id: str, step: int) -> dict:
    """`rewind.preview()`, run on the worker as the rewind's first step. A refusal fails the rewind without retrying."""
    try:
        plan = await preview(activity.client(), workflow_id, step)
    except RewindError as e:
        raise ApplicationError(str(e), type="RewindError", non_retryable=True) from e
    return plan.to_dict()


@activity.defn(name="rewind.reset_agent")
async def reset_agent(rewind_id: str, plan: dict, correction: str) -> dict:
    """Save the correction, then reset the agent workflow to the plan's reset point. `plan` is `Plan.to_dict()`.
    Safe to retry: the correction is saved once per rewind, and the reset happens once."""
    client = activity.client()
    workflow_id, run_id, first_run_id, step = plan["workflow_id"], plan["run_id"], plan["first_run_id"], plan["step"]

    # An earlier attempt may have reset already: Temporal records the run it made on the planned run.
    planned_run = await client.get_workflow_handle(workflow_id, run_id=run_id).describe()
    rewound_run_id = planned_run.raw_description.workflow_extended_info.reset_run_id
    if not rewound_run_id:
        latest_run_id = (await client.get_workflow_handle(workflow_id).describe()).run_id
        if latest_run_id != run_id:
            raise ApplicationError(f"{workflow_id} has a newer run ({latest_run_id}) than the one this rewind planned "
                                   "on, so it wasn't reset", type="RewindError", non_retryable=True)

    # Save the correction before the reset: the rewound run starts at once, and its step `step` model call must find
    # it. Saving it again on a retry changes nothing.
    ledger = Ledger()
    flagged = await asyncio.to_thread(ledger.list_effects, workflow_id, first_run_id, from_step=step,
                                      status="flagged")
    text = correction_with_flagged(correction, flagged)
    await asyncio.to_thread(ledger.add_correction, rewind_id, workflow_id, first_run_id, step, text)

    if not rewound_run_id:
        rewound_run_id = await _reset(client, plan, correction)

    return {
        "rewound_run_id": rewound_run_id,
        "flagged": [dataclasses.asdict(e) for e in flagged],
        "correction": text,
    }


async def _reset(client: Client, plan: dict, correction: str) -> str:
    """Reset the agent workflow to the plan's reset point. Returns the rewound run's ID."""
    response = await client.workflow_service.reset_workflow_execution(ResetWorkflowExecutionRequest(
        namespace=client.namespace,
        workflow_execution=WorkflowExecution(workflow_id=plan["workflow_id"], run_id=plan["run_id"]),
        workflow_task_finish_event_id=plan["reset_event_id"],
        reason=f"rewind to step {plan['step']}: {correction}"[:1000],  # the correction can be any length
        request_id=str(uuid.uuid4()),
        # The old run's later signals and updates belong to the timeline being thrown away.
        reset_reapply_exclude_types=[ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_SIGNAL,
                                     ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_UPDATE],
    ))
    return response.run_id


def correction_with_flagged(correction: str, flagged: list[Effect]) -> str:
    """Your correction, plus what the rewound agent must not repeat: it can't see these effects in its history."""
    if not flagged:
        return correction
    lines = [correction.rstrip(), "", "Already done and NOT undone; do not repeat:"]
    for effect in flagged:
        lines.append(f"- {tools.get_tool(effect.tool).describe_effect(effect)} ({effect.reason})")
    return "\n".join(lines)


async def corrections(step: int) -> list[str]:
    """Await from your model activity: the corrections rewinds have saved for this workflow that apply to `step`.
    Add them to the prompt, and return them in the activity's result so they show up in history.

    Activities aren't told the workflow's `first_execution_run_id`, so this asks Temporal for it (one describe)."""
    info = activity.info()
    handle = activity.client().get_workflow_handle(info.workflow_id, run_id=info.workflow_run_id)
    first_run_id = (await handle.describe()).raw_description.workflow_execution_info.first_run_id
    return await asyncio.to_thread(Ledger().corrections, info.workflow_id, first_run_id, step)
