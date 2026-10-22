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

import rewind
from example import billing
from example.agent import SupportAgent
from example.worker import TASK_QUEUE, make_worker

_data = tempfile.mkdtemp(prefix="rewind-example-")
os.environ.setdefault("BILLING_DB", f"{_data}/billing.db")
os.environ.setdefault("REWIND_DB", f"{_data}/rewind.db")  # Rewind: where it records tool calls

WORKFLOW_ID = "support-ticket-42"
UI_PORT = 8233
UI = f"http://localhost:{UI_PORT}"
STEP = 2
TICKET = "Your service failed us twice this week and nobody answers. Fix this NOW or we walk."
CORRECTION = "Max 10% credit. Never cancel without asking."


def dollars(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def show_customer() -> None:
    customer = billing.get(billing.CUSTOMER)
    credit = dollars(customer["balance_cents"])
    refunded = dollars(customer["refunded_cents"])
    print(f"  billing: credit {credit} · cancelled {customer['cancelled']} · refunded {refunded}\n")


def show_effects(title: str, effects: list) -> None:
    names = [f"step {effect.step} {effect.tool}" for effect in effects]
    print(f"  {title}: {', '.join(names) or 'none'}")


def run_url(run_id: str) -> str:
    return f"{UI}/namespaces/default/workflows/{WORKFLOW_ID}/{run_id}/history"


async def demo(client: Client) -> None:
    billing.seed()
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
    plan = await rewind.preview(client, WORKFLOW_ID, step=STEP)
    show_effects("keep", plan.kept)
    show_effects("undo, newest first", plan.to_undo)
    show_effects("flagged", plan.flagged)

    # 3. Rewind
    print(f"\n3. Rewind to step {STEP} with the correction {CORRECTION!r}")
    result = await rewind.rewind(
        client,
        WORKFLOW_ID,
        step=STEP,
        correction=CORRECTION,
        task_queue=TASK_QUEUE,
    )
    show_effects("undone", result.undone)
    show_effects("flagged (the agent is told)", result.flagged)

    print(f"  the agent re-runs from step {STEP}:")
    rewound_run = client.get_workflow_handle(WORKFLOW_ID, run_id=result.rewound_run_id)
    reply = await rewound_run.result()
    print(f"  reply: {reply}")
    show_customer()

    print("In Temporal:")
    print(f"  original run: {run_url(result.original_run_id)}")
    print(f"  rewound run:  {run_url(result.rewound_run_id)}")
    print(f"  the rewind:   {UI}/namespaces/default/workflows/{rewind.rewind_workflow_id(WORKFLOW_ID)}")


async def wait_for_ui() -> None:
    """Wait for the dev server's UI to come up. The server prints a banner when it does, and waiting keeps the banner
    out of the middle of the demo."""
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
        async with make_worker(client):
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
