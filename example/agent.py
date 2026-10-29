"""A standard agent loop: call the model, run the tools it asks for, repeat until it answers."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

import rewind  # Rewind

with workflow.unsafe.imports_passed_through():
    from example.model import call_model

MAX_STEPS = 10
TIMEOUT = timedelta(seconds=60)


@workflow.defn
class SupportAgent:
    @workflow.run
    async def run(self, ticket: str) -> str:
        messages: list[dict] = [{"role": "user", "content": ticket}]

        for step in range(1, MAX_STEPS + 1):
            # Rewind: instead of workflow.execute_activity
            reply = await rewind.model_call(call_model, step, messages, start_to_close_timeout=TIMEOUT)
            messages.append(
                {
                    "role": "assistant",
                    "content": reply["text"],
                    "tool_calls": reply["tool_calls"],
                }
            )

            if not reply["tool_calls"]:
                return reply["text"]

            # Rewind: runs and records the tools
            results = await rewind.tool_calls(reply["tool_calls"], step, start_to_close_timeout=TIMEOUT)
            messages.append({"role": "tool", "content": results})

        return "Stopped: too many steps."
