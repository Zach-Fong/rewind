"""Rewind: undo what an AI agent did in the real world and re-run it from the step where it went wrong.

    rewind.read_only / reversible / irreversible    register a tool and its undo (rewind.tools)
    rewind.workflow                  model_call / tool_calls helpers for your agent workflow (sandbox-safe)
    await rewind.corrections(step)   inside your model activity: corrections saved by rewinds
    rewind.preview / rewind.rewind   client side (rewind.client)
    rewind.WORKFLOWS / ACTIVITIES    register these on your agent's worker (ACTIVITIES: after importing your tools)
    rewind.Ledger                    the SQLite ledger (REWIND_DB)

Exports are lazy: workflow code imports `rewind.workflow`, which runs this `__init__` inside the workflow sandbox,
so it must not import the client or activity modules eagerly.
"""
from __future__ import annotations

_EXPORTS = {
    "read_only": "tools", "reversible": "tools", "irreversible": "tools", "Ledger": "ledger",
    "corrections": "activities", "preview": "client", "rewind": "client", "Plan": "client",
    "RewindResult": "client", "RewindError": "client", "rewind_workflow_id": "workflow",
}
__all__ = [*_EXPORTS, "WORKFLOWS", "ACTIVITIES"]


def __getattr__(name: str):
    import importlib
    if name == "WORKFLOWS":
        return [importlib.import_module("rewind.workflow").RewindWorkflow]
    if name == "ACTIVITIES":
        a = importlib.import_module("rewind.activities")
        return [a.plan, a.reset_agent, *a.tool_activities()]
    if name in _EXPORTS:
        return getattr(importlib.import_module(f"rewind.{_EXPORTS[name]}"), name)
    raise AttributeError(name)
