"""Rewind: undo what an AI agent did in the real world, then run it again from the step where it went wrong.

    @rewind.read_only / reversible / irreversible   on your tools
    rewind.model_call / tool_calls                  in your agent workflow
    await rewind.corrections(step)                  in your model activity
    rewind.RewindPlugin()                           on your worker, after your tools are imported
    rewind.preview / rewind.rewind                  from your own code

Where to start reading: a rewind begins in `client.rewind`, which starts `RewindWorkflow` in `rewind_workflow.py`.
Its activities are in `rewind_activities.py`. A tool call is recorded by `run_and_record` in `agent_activities.py`.
Every ID and activity name is built in `ids.py`. `plugin.py` adds all of this to your worker.
"""

from rewind.agent_activities import corrections
from rewind.agent_workflow import model_call, tool_calls
from rewind.client import Plan, RewindError, RewindResult, preview, rewind
from rewind.ids import rewind_workflow_id
from rewind.ledger import Effect, Ledger
from rewind.plugin import RewindPlugin
from rewind.tools import irreversible, read_only, reversible

__all__ = [
    "Effect",
    "Ledger",
    "Plan",
    "RewindError",
    "RewindPlugin",
    "RewindResult",
    "corrections",
    "irreversible",
    "model_call",
    "preview",
    "read_only",
    "reversible",
    "rewind",
    "rewind_workflow_id",
    "tool_calls",
]
