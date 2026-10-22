"""The support agent: a test bed for the Rewind library (fake Stripe, fake outbox, scripted model).

    from testbed import TASK_QUEUE, WORKFLOWS, ACTIVITIES, seed
    Worker(client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=ACTIVITIES)

Files: STRIPE_DB holds the fake Stripe and the outbox, and REWIND_DB holds the ledger. MODEL_DELAY_MS slows the
model.
"""

from __future__ import annotations

TASK_QUEUE = "support"


def __getattr__(name: str):
    # Built on access: ACTIVITIES lists the tools, so they must be registered first.
    if name not in ("WORKFLOWS", "ACTIVITIES"):
        raise AttributeError(name)
    import rewind
    from testbed import tools  # noqa: F401  (registers the tools in this process)
    from testbed.agent import SupportAgent
    from testbed.model import call_model

    return [SupportAgent, *rewind.WORKFLOWS] if name == "WORKFLOWS" else [call_model, *rewind.ACTIVITIES]


def seed() -> None:
    """Fresh scenario: seed the fake Stripe, empty the outbox and the ledger."""
    import rewind
    from testbed import data, outbox
    from testbed.stripe import FakeStripe

    FakeStripe().reset_to_seed(data.customers())
    outbox.clear()
    rewind.Ledger().clear()
