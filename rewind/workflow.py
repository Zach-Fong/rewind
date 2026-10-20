"""Workflow side: helpers for your agent workflow, and the rewind workflow. Safe to import in workflow code: only
Temporal imports, and activities are referred to by name.

A step is one model call plus the tool calls it asked for. The helpers give them activity IDs a rewind can find:
`3.model` for step 3's model call, `3.add_credit.0` for its first tool call. Each tool runs as an activity named
after it (`add_credit`), and its undo as `undo_add_credit` (see `rewind.activities`).

    reply = await rw.model_call(call_model, step, *args)      # your activity; its first parameter is `step`
    results = await rw.tool_calls(reply["tool_calls"], step)    # [{"name", "args"}] -> results, in parallel
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

TIMEOUT = timedelta(seconds=60)
RETRY = RetryPolicy(maximum_attempts=5)


def model_activity_id(step: int) -> str:
    return f"{step}.model"


def rewind_workflow_id(workflow_id: str) -> str:
    return f"rewind:{workflow_id}"


def undo_activity_name(tool: str) -> str:
    return f"undo_{tool}"


async def model_call(activity: Any, step: int, *args: Any, start_to_close_timeout: timedelta = TIMEOUT,
                     retry_policy: RetryPolicy = RETRY) -> Any:
    return await workflow.execute_activity(
        activity, args=[step, *args], activity_id=model_activity_id(step), summary=f"step {step} · model call",
        start_to_close_timeout=start_to_close_timeout, retry_policy=retry_policy)


async def tool_calls(calls: list[dict], step: int, *, start_to_close_timeout: timedelta = TIMEOUT,
                     retry_policy: RetryPolicy = RETRY) -> list[Any]:
    """All in parallel; results in the order of `calls`."""
    running = []
    for i, call in enumerate(calls):
        name = call["name"]
        running.append(workflow.execute_activity(
            name,
            args=[call.get("args", {}), step, workflow.info().first_execution_run_id],
            activity_id=f"{step}.{name}.{i}",  # `i` keeps two calls to the same tool in one step distinct
            summary=f"step {step} · {name}",
            start_to_close_timeout=start_to_close_timeout,
            retry_policy=retry_policy,
        ))
    return list(await asyncio.gather(*running))


async def _run(activity: str, *args: Any, summary: str | None = None) -> Any:
    return await workflow.execute_activity(activity, args=list(args), summary=summary,
                                           start_to_close_timeout=TIMEOUT, retry_policy=RETRY)


@workflow.defn(name="rewind.Rewind")
class RewindWorkflow:
    """Rewind one agent workflow to step M. Started by `rewind.rewind()` with ID `rewind:{workflow_id}`, so only one
    runs per agent workflow at a time, and its runs are the record of past rewinds. A crashed caller or worker doesn't
    stop it. An undo that keeps failing fails the rewind before the reset, with no correction saved; running the same
    rewind again carries on from there (undone effects are skipped).

      1. plans: the workflow must be closed, and step M's model call (activity ID `M.model`) must be in its latest
         run. The reset point is that event's `workflow_task_completed_event_id` (a reset must land on a
         workflow-task boundary; the model call then runs again)
      2. undoes the workflow's `done` effects from step M on, newest first; an effect that has drifted is flagged
         instead of undone
      3. saves the correction, plus a line per effect from step M on that is still done in the world (irreversible
         or drifted), for model calls from step M on
      4. resets the workflow to step M with no signals or updates reapplied. Replay rebuilds the agent's state up to
         step M from history; step M's model call runs again and reads the correction.

    Effects before step M are kept."""

    def __init__(self) -> None:
        self._phase = "planning"

    @workflow.query
    def phase(self) -> str:
        """planning, undoing, resetting, done."""
        return self._phase

    @workflow.run
    async def run(self, workflow_id: str, step: int, correction: str) -> dict:
        plan = await _run("rewind.plan", workflow_id, step)

        self._phase = "undoing"
        # Effects that look drifted are tried too: the undo activity checks again and flags them.
        candidates = sorted(plan["to_undo"] + plan["drifted"], key=lambda e: e["seq"], reverse=True)
        undone = []
        for effect in candidates:
            effect_after = await _run(undo_activity_name(effect["tool"]), effect["effect_id"],
                                      summary=f"undo step {effect['step']} · {effect['tool']}")
            if effect_after["status"] == "reverted":
                undone.append(effect_after)

        self._phase = "resetting"
        rewind_id = workflow.info().run_id
        reset = await _run("rewind.reset_agent", rewind_id, plan, correction)

        self._phase = "done"
        return {"rewind_id": rewind_id, "workflow_id": workflow_id, "step": step, "original_run_id": plan["run_id"],
                "rewound_run_id": reset["rewound_run_id"], "reset_event_id": plan["reset_event_id"], "undone": undone,
                "flagged": reset["flagged"], "kept": plan["kept"], "correction": reset["correction"]}
