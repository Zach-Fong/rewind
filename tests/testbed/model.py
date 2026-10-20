"""A deterministic stand-in for the model, and the agent's model activity.

`decide(...)` returns one step: a label, reasoning and the tool calls to make (in parallel). It scripts the whole
ticket from the category, the policy and the corrections, then returns the script's step `step`. Corrections only
reach steps at or after the rewind (earlier steps are replayed from history), so a corrected script keeps the same
early steps.

  billing_error  review account → credit 10% of plan → reply
  outage         review account → credit 5% of plan → reply
  pricing        review account → discount 10% → reply
  cancellation   with a "generous" policy (the hero, ticket 4): review account → discount 10% → credit 150% of plan
                 → schedule cancel ∥ refund last charge → reply. A correction with "10%" caps the credit at 10% of
                 the plan; "never cancel" asks the customer instead of cancelling or refunding.
  anything else  review account (+ docs search) → reply

A money tool named on an "Already done and NOT undone" line of a correction is not called again (a reply that
already went out doesn't stop a new one). The failure and the fix are scripted by construction: this shows the rewind
mechanism, not model behaviour.
"""
from __future__ import annotations

import asyncio
import os

from temporalio import activity

import rewind
from testbed import data

MODEL = "mock-sonnet"


def _call(name: str, **args) -> dict:
    return {"name": name, "args": args}


def _step(label: str, reasoning: str, *calls: dict, status: str | None = None) -> dict:
    return {"label": label, "reasoning": reasoning, "tool_calls": list(calls), "status": status}


def script(t: dict, policy: str, corrections: list[str]) -> list[dict]:
    cid, plan_cents, n = t["customer_id"], t["plan_cents"], t["ticket_id"]
    text = "\n".join(corrections).lower()

    def reply(body: str, status: str = "reply_scheduled", label: str = "reply to customer",
              why: str = "Summarise what was done.") -> dict:
        return _step(label, why, _call("send_reply", customer_id=cid, ticket_id=n, body=body), status=status)

    review = _step("review account", "Check the account first.", _call("lookup_customer", customer_id=cid))
    cat = t["category"]

    if cat == "cancellation":
        steps = [
            _step("review account", "The customer reports outages and threatens to leave. Check the account and "
                  "recent incidents first.", _call("lookup_customer", customer_id=cid),
                  _call("get_incidents", customer_id=cid)),
            _step("offer goodwill discount", "Confirmed outages on a large plan. A 10% discount on the next invoice "
                  "is a fair goodwill gesture.", _call("apply_discount", customer_id=cid, percent=10)),
        ]
        if "generous" not in policy.lower():
            return steps + [reply("We're sorry about the outages; we've applied a 10% discount.")]
        if "10%" in text:
            steps.append(_step("issue capped credit", "Correction: credits are capped at 10% of the plan.",
                               _call("add_credit", customer_id=cid, amount_cents=plan_cents // 10)))
        else:
            steps.append(_step("issue account credit", "The customer is furious and threatening to leave. Policy "
                               "says be generous, and retention matters more than any single charge. Issue a credit "
                               "of 150% of the monthly plan.",
                               _call("add_credit", customer_id=cid, amount_cents=plan_cents * 3 // 2)))
        if "never cancel" in text:
            return steps + [reply("We've applied a credit. Before we change your plan, do you want to cancel?",
                                  status="awaiting_customer", label="ask before cancelling",
                                  why="Correction says never cancel without asking. Ask them to confirm first.")]
        steps.append(_step("handle cancellation", "They said they want to leave. Honor it: schedule the "
                           "cancellation, and refund the last charge to make things right.",
                           _call("schedule_cancel", customer_id=cid),
                           _call("refund", customer_id=cid, amount_cents=t["charge_cents"])))
        return steps + [reply("We're so sorry. We've applied a credit, refunded your last charge and processed "
                              "your cancellation.")]

    money = {"billing_error": ("add_credit", {"amount_cents": plan_cents // 10}),
             "outage": ("add_credit", {"amount_cents": plan_cents // 20}),
             "pricing": ("apply_discount", {"percent": 10})}
    if cat in money:
        tool, args = money[cat]
        return [review, _step("apply standard compensation", "A standard goodwill gesture fits this ticket.",
                              _call(tool, customer_id=cid, **args)),
                reply("Sorry about that; we've applied a goodwill adjustment.")]
    return [_step("review account", "Look up the account and the help center.",
                  _call("lookup_customer", customer_id=cid), _call("search_docs", query=cat.replace("_", " "))),
            reply("Here's how to do that; see the linked help article.")]


def _already_done(corrections: list[str]) -> set[str]:
    done, inside = set(), False
    for line in "\n".join(corrections).splitlines():
        if line.startswith("Already done and NOT undone"):
            inside = True
        elif inside and line.startswith("- "):
            # "- refund $240 to cus_4417 (irreversible)" or "- refund(customer_id=...)" -> "refund"
            done.add(line[2:].split()[0].split("(")[0])
        elif not line.strip():
            inside = False
    return done


def decide(policy: str, ticket: dict, step: int, corrections: list[str]) -> dict:
    steps = script(ticket, policy, corrections)
    this_step = dict(steps[min(step, len(steps)) - 1])
    skip = _already_done(corrections) - {"send_reply"}
    this_step["tool_calls"] = [c for c in this_step["tool_calls"] if c["name"] not in skip]
    return this_step


@activity.defn
async def call_model(step: int, ticket_id: int, messages: list[dict]) -> dict:
    """One model call. Reads the corrections saved by rewinds and returns them, so they're in history."""
    corrections = await rewind.corrections(step)
    await asyncio.sleep(int(os.environ.get("MODEL_DELAY_MS", "300")) / 1000)
    return {**decide(data.POLICY, data.ticket(ticket_id), step, corrections), "model": MODEL,
            "corrections": corrections}
