"""Every ID and name Rewind builds, in the same order as the Identifiers table in docs/DESIGN.md.

This file only imports `hashlib`, so workflow code can import it safely.
"""

from __future__ import annotations

import hashlib


def model_activity_id(step: int) -> str:
    """A rewind finds step M in history by this ID. The rewound run reuses it."""
    return f"{step}.model"


def tool_activity_id(step: int, tool: str, i: int) -> str:
    """`i` is the call's position in the step, so two calls to the same tool in one step get different IDs."""
    return f"{step}.{tool}.{i}"


def idempotency_key(workflow_id: str, run_id: str, activity_id: str) -> str:
    """It stays the same when one call is retried. It changes in a rewound run, because the run ID does."""
    return f"{workflow_id}:{run_id}:{activity_id}"


def effect_id(key: str) -> str:
    """There is one ledger row per tool call, however often the call is retried."""
    return "eff_" + hashlib.sha1(key.encode()).hexdigest()[:16]


def undo_key(effect_id: str) -> str:
    """The undo passes this to the API, so each undo is applied at most once."""
    return f"undo:{effect_id}"


def rewind_workflow_id(workflow_id: str) -> str:
    """Only one workflow can have this ID at a time, so an agent has at most one rewind running."""
    return f"rewind:{workflow_id}"


def undo_activity_name(tool: str) -> str:
    """Each undo activity is named after its tool, so the Temporal UI shows which tool was undone."""
    return f"undo_{tool}"


# The names of Rewind's own workflow and activities, as the Temporal UI shows them.
REWIND_WORKFLOW = "rewind.Rewind"
PREVIEW_ACTIVITY = "rewind.preview"
RESET_ACTIVITY = "rewind.reset_agent"
