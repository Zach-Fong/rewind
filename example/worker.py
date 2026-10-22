"""The worker: runs the agent workflow and its activities, with Rewind's next to them."""

from __future__ import annotations

from temporalio.client import Client
from temporalio.worker import Worker

import rewind
from example import tools  # noqa: F401  Rewind: registers the tools, so import them before rewind.ACTIVITIES
from example.agent import SupportAgent
from example.model import call_model

TASK_QUEUE = "example"


def make_worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SupportAgent, *rewind.WORKFLOWS],  # Rewind
        activities=[call_model, *rewind.ACTIVITIES],  # Rewind: includes an activity per tool
    )
