"""Decorators that register an agent's tools and say how a rewind treats each one.

    @rewind.read_only()                        runs normally and isn't recorded
    @rewind.reversible(undo=..., state=...)    recorded, and a rewind undoes it unless it has drifted
    @rewind.irreversible()                     recorded as flagged, and a rewind tells the agent instead

The callbacks:

    state(args) -> Any         Optional. Reads the part of the world the tool changes, before and after the
                               call. A rewind undoes the effect only if it still equals the after-state.
                               Without it the undo always runs, which suits an undo of a relative change.
    undo(effect, key) -> Any   Reverses the effect. Pass `key` on to your API as the undo's idempotency key.
    describe(effect) -> str    Optional. One line for the correction, such as "refund $240 to cus_4417".

`effect` is the ledger row (`rewind.Effect`), with `.args`, `.before`, `.after` and `.result`.

A tool is recorded when it runs as an activity through `rewind.agent_workflow.tool_calls`, not when you call the
function directly. A tool with an `idempotency_key` parameter gets a key that stays the same across retries and
changes after a rewind.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from rewind.ledger import Effect

log = logging.getLogger(__name__)


def read_only(fn: Callable) -> Callable:
    return _register(Tool(fn.__name__, fn, "read_only"))


def reversible(
    *, undo: Callable, state: Callable | None = None, describe: Callable | None = None
) -> Callable[[Callable], Callable]:
    def register(fn: Callable) -> Callable:
        return _register(Tool(fn.__name__, fn, "reversible", state=state, undo=undo, describe=describe))

    return register


def irreversible(fn: Callable | None = None, *, describe: Callable | None = None) -> Callable:
    """Use it as `@rewind.irreversible` or as `@rewind.irreversible(describe=...)`."""

    def register(fn: Callable) -> Callable:
        return _register(Tool(fn.__name__, fn, "irreversible", describe=describe))

    return register(fn) if fn is not None else register


@dataclass
class Tool:
    """A registered tool: the function, its kind, and the callbacks a rewind uses."""

    name: str
    fn: Callable[..., Any]
    kind: Literal["read_only", "reversible", "irreversible"]
    state: Callable[[dict], Any] | None = None
    undo: Callable[[Effect, str], Any] | None = None
    describe: Callable[[Effect], str] | None = None

    @property
    def takes_idempotency_key(self) -> bool:
        return "idempotency_key" in inspect.signature(self.fn).parameters

    def read_state(self, args: dict) -> Any:
        return self.state(args) if self.state else None

    def describe_effect(self, effect: Effect) -> str:
        """This never raises. It runs late in a rewind, after the undos, and an error here would stop the agent from
        being reset."""
        if self.describe:
            try:
                return self.describe(effect)
            except Exception:
                log.exception("describe failed for %s, so the default line is used", self.name)
        args = ", ".join(f"{k}={v!r}" for k, v in effect.args.items())
        return f"{self.name}({args})"


TOOLS: dict[str, Tool] = {}


def _register(tool: Tool) -> Callable:
    if inspect.iscoroutinefunction(tool.fn):
        raise TypeError(f"{tool.name}: async tools aren't supported yet. Make it a plain function")
    if tool.name in TOOLS:
        raise ValueError(f"a tool named {tool.name!r} is already registered")
    TOOLS[tool.name] = tool
    return tool.fn


def get_tool(name: str) -> Tool:
    try:
        return TOOLS[name]
    except KeyError:
        raise KeyError(f"unknown tool {name!r}. Is the module that registers it imported in this process?") from None


def has_drifted(effect: Effect) -> bool:
    """Return True if the state this effect changed is no longer what the tool left. A tool without `state` is never
    checked."""
    tool = get_tool(effect.tool)
    if tool.state is None:
        return False
    return tool.state(effect.args) != effect.after
