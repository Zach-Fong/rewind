"""The rewind workflow: plan, undo, save the correction, reset. Its activities are in `rewind_activities.py`.

This file only imports Temporal and `rewind.ids`, and refers to activities by name, so it's safe in workflow code.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from rewind import ids

# Every rewind activity is safe to retry, and refusals aren't retried. So each activity keeps retrying for up to an
# hour. That rides out an outage of the tools' APIs, instead of stopping halfway with some effects undone.
ATTEMPT_TIMEOUT = timedelta(seconds=60)
GIVE_UP_AFTER = timedelta(hours=1)
RETRY = RetryPolicy(maximum_interval=timedelta(minutes=1))


@workflow.defn(name=ids.REWIND_WORKFLOW)
class RewindWorkflow:
    """Rewinds one agent workflow to step M.

    `rewind.rewind()` starts it with the ID `rewind:{workflow_id}`, so only one rewind of an agent runs at a time.

      1. Plan: find step M's model call in history, and sort the effects from step M on into ones to undo and flag.
      2. Undo the effects, newest first. An effect that has drifted is flagged instead.
      3. Save the correction, with a line for each flagged effect.
      4. Reset the agent to just before step M's model call, with no signals or updates reapplied.

    If an undo fails with a non-retryable error, or keeps failing for an hour, the rewind stops before step 3.
    Running the same rewind again carries on from there.
    """

    def __init__(self) -> None:
        self._phase = "planning"

    @workflow.query
    def phase(self) -> str:
        """Where the rewind is: planning, undoing, resetting or done."""
        return self._phase

    @workflow.run
    async def run(self, workflow_id: str, step: int, correction: str) -> dict:
        # 1. Plan. The activity returns `Plan.to_dict()`, so `plan` has the fields of `rewind.client.Plan`.
        plan = await _run_activity(ids.PREVIEW_ACTIVITY, workflow_id, step)

        # 2. Undo, newest first. Effects the plan saw as drifted are tried too, because the undo activity checks
        # drift again and flags them if they're still drifted.
        self._phase = "undoing"
        to_try = sorted(plan["to_undo"] + plan["drifted"], key=lambda effect: effect["seq"], reverse=True)
        undone = []
        for effect in to_try:
            undo_activity = ids.undo_activity_name(effect["tool"])
            summary = f"undo step {effect['step']} · {effect['tool']}"
            after_undo = await _run_activity(undo_activity, effect["effect_id"], summary=summary)
            if after_undo["status"] == "reverted":
                undone.append(after_undo)

        # 3 and 4. Save the correction and reset the agent. The rewind's run ID identifies it, so a retried save
        # or reset isn't applied twice.
        self._phase = "resetting"
        rewind_id = workflow.info().run_id
        reset_result = await _run_activity(ids.RESET_ACTIVITY, rewind_id, plan, correction)

        self._phase = "done"
        # The shape RewindResult.from_dict reads.
        return {
            "rewind_id": rewind_id,
            "workflow_id": workflow_id,
            "step": step,
            "original_run_id": plan["run_id"],
            "rewound_run_id": reset_result["rewound_run_id"],
            "reset_event_id": plan["reset_event_id"],
            "undone": undone,
            "flagged": reset_result["flagged"],
            "kept": plan["kept"],
            "correction": reset_result["correction"],
        }


async def _run_activity(activity: str, *args: Any, summary: str | None = None) -> Any:
    """Run one of Rewind's activities by name, with the rewind's timeouts and retries."""
    return await workflow.execute_activity(
        activity,
        args=list(args),
        summary=summary,
        start_to_close_timeout=ATTEMPT_TIMEOUT,
        schedule_to_close_timeout=GIVE_UP_AFTER,
        retry_policy=RETRY,
    )
