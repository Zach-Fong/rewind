"""Rewind: undo what an AI agent did in the real world and re-run it from the step where it went wrong.

    rewind.read_only / reversible / irreversible   register a tool and its undo (rewind.tools)
    rewind.workflow                                model_call / tool_calls for your agent workflow (sandbox-safe)
    await rewind.corrections(step)                 inside your model activity: corrections saved by rewinds
    rewind.preview / rewind.rewind                 client side (rewind.plan, rewind.client)
    rewind.WORKFLOWS / ACTIVITIES                  register these on your agent's worker (ACTIVITIES: after your tools)
    rewind.Ledger                                  the SQLite ledger (REWIND_DB)

Reading order. A rewind: `workflow.RewindWorkflow.run` makes the plan (`plan.preview`), undoes effects
(`activities._check_and_undo`), then saves the correction and resets the agent (`activities.reset_agent`). Recording
a tool call: `workflow.tool_calls` -> `activities.tool_activity` -> `tools.run_and_record`.
"""
from __future__ import annotations

from rewind.activities import corrections
from rewind.client import RewindResult, rewind
from rewind.ledger import Ledger
from rewind.plan import Plan, RewindError, preview
from rewind.tools import irreversible, read_only, reversible
from rewind.workflow import RewindWorkflow, rewind_workflow_id

WORKFLOWS = [RewindWorkflow]

__all__ = [
    "read_only", "reversible", "irreversible",
    "Ledger",
    "corrections",
    "preview", "Plan", "RewindError",
    "rewind", "RewindResult",
    "rewind_workflow_id",
    "WORKFLOWS", "ACTIVITIES",
]


def __getattr__(name: str):
    # Built on access: it lists one activity per registered tool, so import your tools first.
    if name == "ACTIVITIES":
        from rewind import activities
        return [activities.preview_activity, activities.reset_agent, *activities.tool_activities()]
    raise AttributeError(name)
