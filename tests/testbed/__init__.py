"""The support agent: a test bed for the Rewind library (fake Stripe, fake outbox, scripted model).

    from testbed import TASK_QUEUE, make_worker, seed
    make_worker(client)

Files: STRIPE_DB holds the fake Stripe and the outbox, and REWIND_DB holds the ledger. MODEL_DELAY_MS slows the
model.
"""

from __future__ import annotations

from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

TASK_QUEUE = "support"


def make_worker(client: Client, **options: Any) -> Worker:
    """The support agent's worker, with Rewind added by its plugin. `options` go to `Worker`."""
    # Imported here so that `import testbed` stays light. The sandbox loads this package again for every agent
    # workflow run, because the agent's workflow is in `testbed.agent`.
    import rewind
    from testbed import tools  # noqa: F401  (registers the tools in this process)
    from testbed.agent import SupportAgent
    from testbed.model import call_model

    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SupportAgent],
        activities=[call_model],
        plugins=[rewind.RewindPlugin()],
        **options,
    )


def seed() -> None:
    """Fresh scenario: seed the fake Stripe, empty the outbox and the ledger."""
    import rewind
    from testbed import data, outbox
    from testbed.stripe import FakeStripe

    FakeStripe().reset_to_seed(data.customers())
    outbox.clear()
    rewind.Ledger().clear()
