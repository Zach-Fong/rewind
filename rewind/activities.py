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
from temporalio.exceptions import ApplicationError

from rewind import client as rewind_client
from rewind import tools
from rewind.ledger import Effect, Ledger
from rewind.workflow import undo_activity_name

DRIFTED = "drifted"


def tool_activity(tool: tools.Tool):
    """The activity `tool_calls` runs for this tool: runs it and records it."""
    @activity.defn(name=tool.name)
    async def run_tool(args: dict, step: int, first_run_id: str) -> Any:
        info = activity.info()
        key = f"{info.workflow_id}:{info.workflow_run_id}:{info.activity_id}"
        # Tools are blocking calls; a thread keeps the worker's event loop free.
        return await asyncio.to_thread(tools.run_and_record, tool.name, args, step=step, workflow_id=info.workflow_id,
                                       first_run_id=first_run_id, run_id=info.workflow_run_id, key=key)
    return run_tool


def undo_activity(tool: tools.Tool):
    """The activity a rewind runs to undo one of this tool's effects."""
    @activity.defn(name=undo_activity_name(tool.name))
    async def undo_effect(effect_id: str) -> dict:
        return await asyncio.to_thread(_check_and_undo, effect_id)
    return undo_effect


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


@activity.defn(name="rewind.plan")
async def plan(workflow_id: str, step: int) -> dict:
    try:
        return (await rewind_client.preview(activity.client(), workflow_id, step)).to_dict()
    except rewind_client.RewindError as e:
        raise ApplicationError(str(e), type="RewindError", non_retryable=True) from e


def _check_and_undo(effect_id: str) -> dict:
    """Undo one effect if the world still matches it, else flag it. Returns the effect afterwards. Idempotent: the
    row is `reverting` while the undo runs, so a retry re-runs the undo under its key (`undo:{effect_id}`) instead of
    checking a world the first attempt already changed. Anything else is left as it is."""
    ledger = Ledger()
    effect = ledger.get_effect(effect_id)
    if effect.status == "done" and not tools.world_unchanged_since(effect):
        ledger.mark_flagged(effect_id, DRIFTED)
    elif effect.status in ("done", "reverting"):
        ledger.mark_reverting(effect_id)
        tools.undo(effect)
        ledger.mark_reverted(effect_id)
    return dataclasses.asdict(ledger.get_effect(effect_id))


@activity.defn(name="rewind.reset_agent")
async def reset_agent(rewind_id: str, plan: dict, correction: str) -> dict:
    """Save the correction, then reset the agent workflow to the plan's step. Safe to retry: the correction is saved
    once per rewind, and if an earlier attempt already reset the planned run, its rewound run is returned instead of
    resetting again."""
    workflow_id, first_run_id, step = plan["workflow_id"], plan["first_run_id"], plan["step"]
    client = activity.client()
    planned = await client.get_workflow_handle(workflow_id, run_id=plan["run_id"]).describe()
    rewound_run_id = planned.raw_description.workflow_extended_info.reset_run_id
    if not rewound_run_id:
        latest_run_id = (await client.get_workflow_handle(workflow_id).describe()).run_id
        if latest_run_id != plan["run_id"]:
            raise ApplicationError(f"{workflow_id} has a newer run ({latest_run_id}) than the one this rewind planned "
                                   "on, so it wasn't reset", type="RewindError", non_retryable=True)

    ledger = Ledger()
    flagged = await asyncio.to_thread(ledger.list_effects, workflow_id, first_run_id, from_step=step,
                                      status="flagged")
    text = correction_with_flagged(correction, flagged)
    # Before the reset: the new run starts at once, and its step `step` model call must find the correction.
    await asyncio.to_thread(ledger.add_correction, rewind_id, workflow_id, first_run_id, step, text)

    if not rewound_run_id:
        response = await client.workflow_service.reset_workflow_execution(ResetWorkflowExecutionRequest(
            namespace=client.namespace,
            workflow_execution=WorkflowExecution(workflow_id=workflow_id, run_id=plan["run_id"]),
            workflow_task_finish_event_id=plan["reset_event_id"],
            reason=f"rewind to step {step}: {correction}"[:1000],
            request_id=str(uuid.uuid4()),
            # The old run's later signals and updates belong to the timeline being thrown away.
            reset_reapply_exclude_types=[ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_SIGNAL,
                                         ResetReapplyExcludeType.RESET_REAPPLY_EXCLUDE_TYPE_UPDATE]))
        rewound_run_id = response.run_id
    return {"rewound_run_id": rewound_run_id, "flagged": [dataclasses.asdict(e) for e in flagged],
            "correction": text}


def correction_with_flagged(correction: str, flagged: list[Effect]) -> str:
    """Your correction, plus what the rewound agent must not repeat: it can't see these effects in its history."""
    if not flagged:
        return correction
    lines = [correction.rstrip(), "", "Already done and NOT undone; do not repeat:"]
    for e in flagged:
        lines.append(f"- {tools.get_tool(e.tool).describe_effect(e)} ({e.reason})")
    return "\n".join(lines)


async def corrections(step: int) -> list[str]:
    """Await from your model activity: the corrections rewinds have saved for this workflow that apply to `step`.
    Add them to the prompt, and return them in the activity's result so they show up in history.

    Activities aren't told the workflow's `first_execution_run_id`, so this asks Temporal for it (one describe)."""
    info = activity.info()
    handle = activity.client().get_workflow_handle(info.workflow_id, run_id=info.workflow_run_id)
    first_run_id = (await handle.describe()).raw_description.workflow_execution_info.first_run_id
    return await asyncio.to_thread(Ledger().corrections, info.workflow_id, first_run_id, step)
