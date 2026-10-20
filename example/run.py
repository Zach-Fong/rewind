"""Run the agent, then rewind it to step 2 with a correction. Temporal UI on :8233.

Usage: python -m example.run [--address localhost:7233]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
import urllib.request

from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import rewind
from example import billing
from example import tools
from example.agent import SupportAgent
from example.model import TICKET, call_model

_data = tempfile.mkdtemp(prefix="rewind-example-")
os.environ.setdefault("BILLING_DB", f"{_data}/billing.db")
os.environ.setdefault("REWIND_DB", f"{_data}/rewind.db")  # Rewind: where it records tool calls

TASK_QUEUE = "example"
WORKFLOW_ID = "support-ticket-42"
UI_PORT = 8233
UI = f"http://localhost:{UI_PORT}"
STEP = 2
CORRECTION = "Max 10% credit. Never cancel without asking."


def dollars(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def show_customer() -> None:
    c = billing.get(billing.CUSTOMER)
    credit = dollars(c["balance_cents"])
    refunded = dollars(c["refunded_cents"])
    print(f"  billing: credit {credit} · cancelled {c['cancelled']} · refunded {refunded}\n")


def show_effects(title: str, effects: list) -> None:
    names = [f"step {e.step} {e.tool}" for e in effects]
    print(f"  {title}: {', '.join(names) or 'none'}")


def run_url(run_id: str) -> str:
    return f"{UI}/namespaces/default/workflows/{WORKFLOW_ID}/{run_id}/history"


async def demo(client: Client) -> None:
    billing.reset()
    rewind.Ledger().clear()

    # 1. Run the agent
    print(f"\n1. The agent handles the ticket: {TICKET!r}")
    handle = await client.start_workflow(
        SupportAgent.run,
        TICKET,
        id=WORKFLOW_ID,
        task_queue=TASK_QUEUE,
    )
    reply = await handle.result()  # Rewind needs a finished workflow
    print(f"  reply: {reply}")
    show_customer()

    # 2. Preview the rewind
    print(f"2. Preview a rewind to step {STEP} (changes nothing)")
    plan = await rewind.preview(client, WORKFLOW_ID, STEP)
    show_effects("keep", plan.kept)
    show_effects("undo, newest first", plan.to_undo)
    show_effects("flagged", plan.flagged)

    # 3. Rewind
    print(f"\n3. Rewind to step {STEP} with the correction {CORRECTION!r}")
    result = await rewind.rewind(
        client,
        WORKFLOW_ID,
        STEP,
        CORRECTION,
        task_queue=TASK_QUEUE,
    )
    show_effects("undone", result.undone)
    show_effects("flagged (the agent is told)", result.flagged)

    print("  the agent re-runs from step 2:")
    new_run = client.get_workflow_handle(WORKFLOW_ID, run_id=result.rewound_run_id)
    reply = await new_run.result()
    print(f"  reply: {reply}")
    show_customer()

    print("In Temporal:")
    print(f"  original run: {run_url(result.original_run_id)}")
    print(f"  rewound run:  {run_url(result.rewound_run_id)}")
    print(f"  the rewind:   {UI}/namespaces/default/workflows/{rewind.rewind_workflow_id(WORKFLOW_ID)}")


async def wait_for_ui() -> None:
    """The dev server prints its banner once the UI is up; wait for it so the banner doesn't land mid-demo."""
    for _ in range(100):
        try:
            await asyncio.to_thread(urllib.request.urlopen, UI, timeout=1)
            await asyncio.sleep(0.5)
            return
        except OSError:
            await asyncio.sleep(0.2)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--address", help="use an existing Temporal server instead of starting one")
    args = parser.parse_args()

    env = None
    if args.address:
        client = await Client.connect(args.address)
    else:
        env = await WorkflowEnvironment.start_local(ui=True, ui_port=UI_PORT, dev_server_log_level="error")
        await wait_for_ui()
        client = env.client

    try:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[SupportAgent, *rewind.WORKFLOWS],  # Rewind
            activities=[call_model, *rewind.ACTIVITIES],  # Rewind
        )
        async with worker:
            await demo(client)

        if env:
            print(f"\nTemporal UI is up on :{UI_PORT}. Ctrl-C to stop.", flush=True)
            await asyncio.Event().wait()
    finally:
        if env:
            await env.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
