"""Rewind: undo what an AI agent did in the real world, then run it again from the step where it went wrong.

    @rewind.read_only / reversible / irreversible   on your tools
    rewind.agent_workflow                           in your agent workflow: model_call and tool_calls
    await rewind.corrections(step)                  in your model activity
    rewind.WORKFLOWS / ACTIVITIES                   on your worker, after your tools are imported
    rewind.preview / rewind.rewind                  from your own code

Where to start reading: a rewind begins in `client.rewind`, which starts `RewindWorkflow` in `rewind_workflow.py`.
Its activities are in `rewind_activities.py`. A tool call is recorded by `run_and_record` in `agent_activities.py`.
Every ID and activity name is built in `ids.py`.
"""

from rewind import agent_activities, rewind_activities
from rewind.agent_activities import corrections
from rewind.client import Plan, RewindError, RewindResult, preview, rewind
from rewind.ids import rewind_workflow_id
from rewind.ledger import Effect, Ledger
from rewind.rewind_workflow import RewindWorkflow
from rewind.tools import irreversible, read_only, reversible

__all__ = [
    "ACTIVITIES",
    "WORKFLOWS",
    "Effect",
    "Ledger",
    "Plan",
    "RewindError",
    "RewindResult",
    "corrections",
    "irreversible",
    "preview",
    "read_only",
    "reversible",
    "rewind",
    "rewind_workflow_id",
]

WORKFLOWS = [RewindWorkflow]


# ACTIVITIES is built when it's read, not when this file is imported. It holds one activity per registered tool,
# and your tools register when your own module is imported, after this one.
def __getattr__(name: str):
    if name == "ACTIVITIES":
        return [
            rewind_activities.preview_activity,
            rewind_activities.reset_agent,
            *agent_activities.tool_activities(),
            *rewind_activities.undo_activities(),
        ]
    raise AttributeError(f"module 'rewind' has no attribute {name!r}")
