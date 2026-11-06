"""`RewindPlugin`: adds Rewind to your worker as a Temporal plugin.

    Worker(client, task_queue=..., workflows=[MyAgent], activities=[call_model], plugins=[rewind.RewindPlugin()])

Pass it to the worker or to `Client.connect`, not both. A worker gets its client's plugins too, so with both it
registers Rewind's activities twice and fails to start.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence

from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from rewind import agent_activities, rewind_activities
from rewind.rewind_workflow import RewindWorkflow


class RewindPlugin(SimplePlugin):
    """Adds the rewind workflow and its activities to a worker, with an activity per tool and an undo activity per
    reversible tool. Import your tools before you build the worker, because their activities are made then."""

    def __init__(self) -> None:
        super().__init__(
            "rewind.RewindPlugin",
            workflows=[RewindWorkflow],
            activities=_add_rewind_activities,
            workflow_runner=_pass_rewind_through,
        )


def _add_rewind_activities(activities: Sequence[Callable] | None) -> list[Callable]:
    """Return the worker's own activities with Rewind's added. It runs when the worker is built, after your tools
    have registered."""
    return [
        *(activities or []),
        rewind_activities.preview_activity,
        rewind_activities.reset_agent,
        *agent_activities.tool_activities(),
        *rewind_activities.undo_activities(),
    ]


def _pass_rewind_through(runner: WorkflowRunner | None) -> WorkflowRunner | None:
    """Let workflows use the `rewind` module the worker already imported. Without this, the sandbox imports it
    again for every workflow run. Rewind's workflow code is deterministic and keeps no state between runs, so sharing
    it is safe."""
    if not isinstance(runner, SandboxedWorkflowRunner):
        return runner
    restrictions = runner.restrictions.with_passthrough_modules("rewind")
    return dataclasses.replace(runner, restrictions=restrictions)
