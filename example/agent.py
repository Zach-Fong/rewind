"""A standard agent loop: call the model, run the tools it asks for, repeat until it answers."""

from __future__ import annotations

from temporalio import workflow

from rewind import agent_workflow as rw  # Rewind

with workflow.unsafe.imports_passed_through():
    from example.model import call_model

MAX_STEPS = 10


@workflow.defn
class SupportAgent:
    @workflow.run
    async def run(self, ticket: str) -> str:
        messages: list[dict] = [{"role": "user", "content": ticket}]

        for step in range(1, MAX_STEPS + 1):
            # Rewind: instead of workflow.execute_activity
            reply = await rw.model_call(call_model, step, messages)
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
            results = await rw.tool_calls(reply["tool_calls"], step)
            messages.append({"role": "tool", "content": results})

        return "Stopped: too many steps."
