"""The activities Rewind runs during the agent's run. There's one per tool, which runs the tool and records it in the
ledger, and `corrections()`, which your model activity calls.

Each tool's activity is named after the tool, so the Temporal UI shows the tool's name.
"""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity

from rewind import ids, tools
from rewind.ledger import IRREVERSIBLE_REASON, Ledger


def tool_activity(tool: tools.Tool):
    """Build the activity that `tool_calls` runs for this tool. It runs the tool and records the call."""

    @activity.defn(name=tool.name)
    async def run_tool(args: dict, step: int, first_run_id: str) -> Any:
        info = activity.info()
        key = ids.idempotency_key(info.workflow_id, info.workflow_run_id, info.activity_id)
        # Tools are plain blocking functions. Running them in a thread keeps the worker's event loop free.
        return await asyncio.to_thread(
            run_and_record,
            tool.name,
            args,
            step=step,
            workflow_id=info.workflow_id,
            first_run_id=first_run_id,
            run_id=info.workflow_run_id,
            key=key,
        )

    return run_tool


def run_and_record(
    name: str,
    args: dict,
    *,
    step: int,
    workflow_id: str,
    first_run_id: str,
    run_id: str,
    key: str,
    ledger: Ledger | None = None,
) -> Any:
    """Run a tool and record its effect in the ledger. The tool's activity calls this.

    A crash at any point leaves a `pending` row. A retry has the same key, and so the same effect ID, which lets it
    find that row and carry on.
    """
    tool = tools.get_tool(name)

    # Pass the idempotency key only to tools that take one.
    kwargs = dict(args)
    if tool.takes_idempotency_key:
        kwargs["idempotency_key"] = key

    # A read-only tool changes nothing, so it just runs.
    if tool.kind == "read_only":
        return tool.fn(**kwargs)

    # Read the before-state and write a `pending` row before the tool runs. A retry keeps the first attempt's
    # before-state, because that attempt may already have changed the world.
    ledger = ledger or Ledger()
    effect_id = ids.effect_id(key)
    earlier_attempt = ledger.get_effect(effect_id)
    before = earlier_attempt.before if earlier_attempt else tool.read_state(args)
    ledger.insert_pending(effect_id, workflow_id, first_run_id, run_id, step, name, args, before)

    # Run the tool. If it raises, the row becomes `failed`, unless an earlier attempt already finished.
    try:
        result = tool.fn(**kwargs)
    except Exception as e:
        ledger.mark_failed(effect_id, f"{type(e).__name__}: {e}")
        raise

    # Read the after-state and finish the row. An irreversible tool's row is flagged straight away.
    after = tool.read_state(args)
    if tool.kind == "irreversible":
        ledger.record_result(effect_id, after, result, status="flagged", reason=IRREVERSIBLE_REASON)
    else:
        ledger.record_result(effect_id, after, result, status="done")
    return result


def tool_activities() -> list:
    """Build one activity per registered tool."""
    if not tools.TOOLS:
        raise RuntimeError("no tools registered: import your tools before building the worker")
    return [tool_activity(tool) for tool in tools.TOOLS.values()]


async def corrections(step: int) -> list[str]:
    """Return the corrections that apply to `step` for this workflow. Call it from your model activity.

    Add them to the prompt, and return them in the activity's result so they show in history. Activities can't see
    the workflow's `first_execution_run_id`, so this asks Temporal for it with one describe call.
    """
    info = activity.info()
    handle = activity.client().get_workflow_handle(info.workflow_id, run_id=info.workflow_run_id)
    first_run_id = (await handle.describe()).raw_description.workflow_execution_info.first_run_id
    return await asyncio.to_thread(Ledger().corrections, info.workflow_id, first_run_id, step)
