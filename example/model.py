"""The model call. A scripted stand-in for an LLM, so every run is the same."""
from __future__ import annotations

from temporalio import activity

import rewind  # Rewind
from example.billing import CUSTOMER

SYSTEM_PROMPT = """You are a support agent. Be generous when a customer has had a bad experience: use credits or
refunds as you see fit. Customer retention matters more than any single charge."""

TICKET = "Your service failed us twice this week and nobody answers. Fix this NOW or we walk."


def tool_call(name: str, **args) -> dict:
    return {"name": name, "args": args}


def answer(text: str, *tool_calls: dict) -> dict:
    return {"text": text, "tool_calls": list(tool_calls)}


def scripted_llm(step: int, corrections: list[str]) -> dict:
    corrected = "\n".join(corrections).lower()
    cap_credit = "10%" in corrected
    ask_before_cancel = "never cancel" in corrected
    already_refunded = "refund" in corrected

    if step == 1:
        return answer(
            "Let me look up the account.",
            tool_call("get_customer", customer_id=CUSTOMER),
        )

    if step == 2 and cap_credit:
        return answer(
            "Credits are capped at 10% of the plan: $200.",
            tool_call("add_credit", customer_id=CUSTOMER, amount_cents=20000),
        )

    if step == 2:
        return answer(
            "They're furious and on a $2,000/month plan. Policy says be generous: credit 150% of the plan.",
            tool_call("add_credit", customer_id=CUSTOMER, amount_cents=300000),
        )

    if step >= 3 and ask_before_cancel:
        text = "We've added a $200 credit."
        if already_refunded:
            text += " Your $240 refund is already on its way."
        return answer(text + " Do you still want to cancel?")

    if step == 3:
        return answer(
            "They want to leave. Cancel, and refund the last charge.",
            tool_call("cancel_subscription", customer_id=CUSTOMER),
            tool_call("refund", customer_id=CUSTOMER, amount_cents=24000),
        )

    return answer("We've added a $3,000 credit, refunded your last charge and cancelled your plan.")


@activity.defn
async def call_model(step: int, messages: list[dict]) -> dict:
    corrections = await rewind.corrections(step)  # Rewind: empty unless a rewind went back to this step or earlier
    response = scripted_llm(step, corrections)

    print(f"  step {step}: {response['text']}", flush=True)
    for call in response["tool_calls"]:
        print(f"          -> {call['name']}({call['args']})", flush=True)

    response["corrections"] = corrections  # Rewind (optional): shows them in the Temporal UI
    return response
