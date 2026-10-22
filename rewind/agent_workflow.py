"""Helpers for your agent workflow: `model_call` and `tool_calls`.

A step is one model call plus the tool calls it asks for. The helpers give each call an activity ID a rewind can find
in history: `3.model` for step 3's model call, and `3.add_credit.0` for its first tool call. Each tool runs as an
activity named after it.

    reply = await rw.model_call(call_model, step, *args)       # your activity, which takes `step` first
    results = await rw.tool_calls(reply["tool_calls"], step)   # runs [{"name", "args"}] in parallel

This file only imports Temporal and `rewind.ids`, and refers to activities by name, so it's safe in workflow code.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from rewind import ids

TIMEOUT = timedelta(seconds=60)
RETRY = RetryPolicy(maximum_attempts=5)


async def model_call(
    activity: Any, step: int, *args: Any, start_to_close_timeout: timedelta = TIMEOUT, retry_policy: RetryPolicy = RETRY
) -> Any:
    """Run your model activity with the ID `{step}.model`, passing `step` as its first argument. A rewind uses that
    ID to find this step in history."""
    return await workflow.execute_activity(
        activity,
        args=[step, *args],
        activity_id=ids.model_activity_id(step),
        summary=f"step {step} · model call",
        start_to_close_timeout=start_to_close_timeout,
        retry_policy=retry_policy,
    )


async def tool_calls(
    calls: list[dict], step: int, *, start_to_close_timeout: timedelta = TIMEOUT, retry_policy: RetryPolicy = RETRY
) -> list[Any]:
    """Run the tool calls in parallel, and return their results in the same order as `calls`."""
    # Activities can't see the workflow's first run ID, so it's passed in. The tool activity files the effect under it.
    first_run_id = workflow.info().first_execution_run_id
    running = []
    for i, call in enumerate(calls):
        name = call["name"]
        running.append(
            workflow.execute_activity(
                name,
                args=[call.get("args", {}), step, first_run_id],
                activity_id=ids.tool_activity_id(step, name, i),
                summary=f"step {step} · {name}",
                start_to_close_timeout=start_to_close_timeout,
                retry_policy=retry_policy,
            )
        )
    return list(await asyncio.gather(*running))
