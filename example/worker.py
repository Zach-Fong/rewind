"""The worker: runs the agent workflow and its activities. Rewind's plugin adds its own next to them."""

from __future__ import annotations

from temporalio.client import Client
from temporalio.worker import Worker

import rewind
from example import tools  # noqa: F401  Rewind: registers the tools, so import them before building the worker
from example.agent import SupportAgent
from example.model import call_model

TASK_QUEUE = "example"


def make_worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SupportAgent],
        activities=[call_model],
        plugins=[rewind.RewindPlugin()],  # Rewind: adds its workflow and an activity per tool
    )
