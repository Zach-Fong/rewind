"""SupportAgent: a standard tool-using agent loop as a Temporal workflow, one workflow per ticket (`ticket-N`).

The workflow keeps the conversation. Each step: call the model with it, run the tool calls it asked for in
parallel, add the results, repeat until the model sets a final status. Everything the conversation holds comes from
activity results, so a reset to step M rebuilds it from history exactly as it was before step M's model call.
"""
from __future__ import annotations

from temporalio import workflow

from rewind import workflow as rw

with workflow.unsafe.imports_passed_through():
    from testbed.model import call_model

MAX_STEPS = 12


def workflow_id(ticket: int) -> str:
    return f"ticket-{ticket}"


@workflow.defn(name="SupportAgent")
class SupportAgent:
    @workflow.run
    async def run(self, ticket: int) -> dict:
        messages: list[dict] = [{"role": "user", "ticket": ticket}]
        for step in range(1, MAX_STEPS + 1):
            reply = await rw.model_call(call_model, step, ticket, messages)
            messages.append({"role": "assistant", "step": step, "reasoning": reply["reasoning"],
                             "tool_calls": reply["tool_calls"]})
            if reply["tool_calls"]:
                results = await rw.tool_calls(reply["tool_calls"], step)
                messages.append({"role": "tool", "step": step, "results": [
                    {"name": c["name"], "result": r} for c, r in zip(reply["tool_calls"], results, strict=True)]})
            if reply["status"]:
                return {"ticket": ticket, "status": reply["status"], "steps": step}
        return {"ticket": ticket, "status": "max_steps", "steps": MAX_STEPS}
