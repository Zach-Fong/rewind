"""Register an agent's tools, and how to undo them. Recording happens when a tool runs through
`rewind.workflow.tool_calls` (an activity named after the tool), not when the function is called directly.

Three kinds:
  @rewind.read_only                               runs, nothing recorded
  @rewind.reversible(undo=..., state=...)         recorded; a rewind undoes it, unless it has drifted
  @rewind.irreversible()                          recorded as flagged; a rewind leaves it and tells the agent

Callbacks:
  state(args) -> Any            optional: the part of the world the tool changes, read before and after the call. A
                                rewind undoes the effect only if state(args) still equals the recorded after-state.
                                Without it, the undo always runs (for undos that reverse a relative change).
  undo(effect, key) -> Any      reverse the effect; pass `key` on as the undo's idempotency key
  describe(effect) -> str       one line for the correction ("refund $240 to cus_4417"); has a default

`effect` is the ledger row (`rewind.ledger.Effect`): `.args`, `.before`, `.after`, `.result`, ...

Idempotency: a tool with an `idempotency_key` parameter gets `{workflow_id}:{run_id}:{activity_id}`. The run ID is
there because activity IDs repeat across a reset. A retry maps to the same ledger row (the effect ID is derived from
the key) and keeps the first attempt's before-state. Undos get `undo:{effect_id}`.

Write order: before-state, pending row, tool call, after-state, done (`flagged` for an irreversible tool). A crash in
between leaves a `pending` row.
"""
from __future__ import annotations

import hashlib
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

from rewind.ledger import Effect, Ledger

log = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    fn: Callable[..., Any]
    kind: Literal["read_only", "reversible", "irreversible"]
    state: Callable[[dict], Any] | None = None
    undo: Callable[[Effect, str], Any] | None = None
    describe: Callable[[Effect], str] | None = None

    @property
    def takes_key(self) -> bool:
        return "idempotency_key" in inspect.signature(self.fn).parameters

    def read_state(self, args: dict) -> Any:
        return self.state(args) if self.state else None

    def describe_effect(self, effect: Effect) -> str:
        """Never raises: it runs mid-rewind, after the undos, where an error would leave the agent un-reset."""
        if self.describe:
            try:
                return self.describe(effect)
            except Exception:
                log.exception("describe failed for %s; using the default line", self.name)
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in effect.args.items())})"


TOOLS: dict[str, Tool] = {}


def _register(tool: Tool) -> Callable:
    if inspect.iscoroutinefunction(tool.fn):
        raise TypeError(f"{tool.name}: async tools aren't supported yet; make it a plain function")
    if tool.name in TOOLS:
        raise ValueError(f"a tool named {tool.name!r} is already registered")
    TOOLS[tool.name] = tool
    return tool.fn


def read_only(fn: Callable) -> Callable:
    return _register(Tool(fn.__name__, fn, "read_only"))


def reversible(*, undo: Callable, state: Callable | None = None,
               describe: Callable | None = None) -> Callable[[Callable], Callable]:
    return lambda fn: _register(Tool(fn.__name__, fn, "reversible", state, undo, describe))


def irreversible(*, describe: Callable | None = None) -> Callable[[Callable], Callable]:
    return lambda fn: _register(Tool(fn.__name__, fn, "irreversible", describe=describe))


def get_tool(name: str) -> Tool:
    try:
        return TOOLS[name]
    except KeyError:
        raise KeyError(f"unknown tool {name!r}; is the module that registers it imported in this process?") from None


def effect_id_from_key(key: str) -> str:
    return "eff_" + hashlib.sha1(key.encode()).hexdigest()[:16]


def run_and_record(name: str, args: dict, *, step: int, workflow_id: str, first_run_id: str, run_id: str, key: str,
                   ledger: Ledger | None = None) -> Any:
    """Run a tool and record it. The tool's activity calls this with its identity; tests call it directly."""
    tool = get_tool(name)
    kwargs = {**args, "idempotency_key": key} if tool.takes_key else args
    if tool.kind == "read_only":
        return tool.fn(**kwargs)

    ledger = ledger or Ledger()
    effect_id = effect_id_from_key(key)
    # A retry keeps the first attempt's before-state: that attempt may already have changed the world.
    earlier_attempt = ledger.get_effect(effect_id)
    before = earlier_attempt.before if earlier_attempt else tool.read_state(args)
    ledger.insert(effect_id, workflow_id, first_run_id, run_id, step, name, args, before)

    try:
        result = tool.fn(**kwargs)
    except Exception as e:
        ledger.mark_failed(effect_id, f"{type(e).__name__}: {e}")
        raise

    if tool.kind == "irreversible":
        ledger.mark_done(effect_id, tool.read_state(args), result, status="flagged", reason="irreversible")
    else:
        ledger.mark_done(effect_id, tool.read_state(args), result)
    return result


def world_unchanged_since(effect: Effect) -> bool:
    """The drift check: does the world still look the way this effect left it? A tool without `state` isn't checked."""
    tool = get_tool(effect.tool)
    if tool.state is None:
        return True
    return tool.state(effect.args) == effect.after


def undo(effect: Effect) -> Any:
    return get_tool(effect.tool).undo(effect, f"undo:{effect.effect_id}")
