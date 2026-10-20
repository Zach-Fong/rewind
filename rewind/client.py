"""Client side: run a rewind of a finished agent workflow and wait for it.

    plan = await rewind.preview(client, workflow_id, step)    # optional, changes nothing (rewind.plan)
    result = await rewind.rewind(client, workflow_id, step, correction, task_queue="...")

A rewind runs as the `rewind.Rewind` workflow; its steps are in `rewind.workflow.RewindWorkflow`. While it runs,
`client.get_workflow_handle(rewind_workflow_id(workflow_id)).query("phase")` says where it is.
"""
from __future__ import annotations

from dataclasses import dataclass

from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import FailureError, WorkflowAlreadyStartedError

from rewind.ledger import Effect
from rewind.plan import RewindError
from rewind.workflow import RewindWorkflow, rewind_workflow_id


@dataclass
class RewindResult:
    rewind_id: str              # the run ID of the rewind workflow
    workflow_id: str
    step: int
    original_run_id: str
    rewound_run_id: str
    reset_event_id: int
    undone: list[Effect]
    flagged: list[Effect]       # from step M on, still done in the world (irreversible or drifted)
    kept: list[Effect]
    correction: str             # as saved: your correction plus a line per still-done effect

    @classmethod
    def from_dict(cls, d: dict) -> RewindResult:
        fields = dict(d)
        for key in ("undone", "flagged", "kept"):
            fields[key] = [Effect(**e) for e in d[key]]  # effects come back from the workflow as dicts
        return cls(**fields)


async def rewind(client: Client, workflow_id: str, step: int, correction: str, *, task_queue: str) -> RewindResult:
    """Run the `rewind.Rewind` workflow on `task_queue` (the agent's worker, which has the tools) and wait for it."""
    if not correction.strip():
        raise RewindError("a rewind needs a correction")
    try:
        handle = await client.start_workflow(RewindWorkflow.run, args=[workflow_id, step, correction],
                                             id=rewind_workflow_id(workflow_id), task_queue=task_queue)
    except WorkflowAlreadyStartedError:
        raise RewindError(f"{workflow_id} already has a rewind in progress") from None
    try:
        return RewindResult.from_dict(await handle.result())
    except WorkflowFailureError as e:
        root = e
        while root.__cause__ is not None:  # workflow failure <- activity failure <- the refusal
            root = root.__cause__
        message = root.message if isinstance(root, FailureError) else str(root)
        raise RewindError(message) from e
