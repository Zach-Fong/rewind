# Support agent example

This example adds Rewind to a support agent implemented as a standard Temporal agent loop. It's handling an angry
customer whose service failed twice this week and who's threatening to leave their $2,000/month plan. Your agent's
policy says to be generous, but it credits too much and cancels their plan:

| Step | What the agent does | Kind of tool |
|---|---|---|
| 1 | Looks up the customer's account | Read-only |
| 2 | Takes 10% off their plan ($200/month) | Reversible |
| 3 | Credits 1.5 months of their subscription ($3,000) | Reversible |
| 4 | Cancels their subscription<br>Refunds their last charge ($240) | Reversible<br>Irreversible |
| 5 | Schedules a reply to the customer (9 AM tomorrow) | Reversible |

So you rewind the agent to step 3 with your correction, "Max 10% credit. Never cancel without asking." The rewind has
two parts:

1. **Undo.** It plans what to keep, undo and flag with `rewind.preview()`, then follows the plan:
   - keep: `apply_discount`
   - undo, newest first: `schedule_reply`, `cancel_subscription`, `add_credit`
   - flag: `refund`, since it can't be undone
2. **Reset.** It resets the workflow to step 3:
   - steps 1 and 2 come back from Temporal's history
   - step 3 runs again with your correction, and the agent is told the refund already happened
   - this time the agent credits $200 and schedules a reply that mentions the refund and asks before cancelling

## Run it

Create a virtual environment, install Rewind, then run the example:

```
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m example.run
```

## Files

The example has six files. Rewind changes four of them: `agent.py`, `model.py`, `tools.py` and `worker.py`. `run.py`
runs the agent's workflow, then calls the rewind.

| File | What it is |
|---|---|
| `agent.py` | The agent's workflow, a standard agent loop |
| `model.py` | The model call, scripted so every run is the same |
| `tools.py` | The agent's tools, each with a Rewind decorator |
| `billing.py` | A fake billing system and outbox |
| `worker.py` | Runs the agent's workflow and activities, plus Rewind's |
| `run.py` | Runs the agent, then rewinds it |

## Adding Rewind

The diffs below show how each file changes when you add Rewind.

### The workflow (`agent.py`)

The workflow runs the model and tool calls through two Rewind helpers instead of `workflow.execute_activity`. The
helpers give each call an activity ID that includes its step, so a rewind can find the step in history.

```diff
+from rewind import agent_workflow as rw
+
 for step in range(1, MAX_STEPS + 1):
-    reply = await workflow.execute_activity(call_model, messages, start_to_close_timeout=TIMEOUT)
+    reply = await rw.model_call(call_model, step, messages, start_to_close_timeout=TIMEOUT)
     messages.append({"role": "assistant", "content": reply["text"], "tool_calls": reply["tool_calls"]})

     if not reply["tool_calls"]:
         return reply["text"]

-    tool_runs = []
-    for call in reply["tool_calls"]:
-        args = list(call["args"].values())
-        tool_runs.append(workflow.execute_activity(call["name"], args=args, start_to_close_timeout=TIMEOUT))
-    results = await asyncio.gather(*tool_runs)
+    results = await rw.tool_calls(reply["tool_calls"], step, start_to_close_timeout=TIMEOUT)
     messages.append({"role": "tool", "content": results})
```

### The model call (`model.py`)

The model call takes the step number as its first argument. It reads the corrections from any rewind to this step or
earlier, and adds them to the prompt. Returning them is optional.

```diff
 @activity.defn
-async def call_model(messages: list[dict]) -> dict:
-    return llm(SYSTEM_PROMPT, messages, TOOLS)
+async def call_model(step: int, messages: list[dict]) -> dict:
+    corrections = await rewind.corrections(step)
+    prompt = SYSTEM_PROMPT + "\n".join(corrections)
+    reply = llm(prompt, messages, TOOLS)
+    return {**reply, "corrections": corrections}  # Returning them shows them in the Temporal UI.
```

### The tools (`tools.py`)

Each tool swaps `@activity.defn` for one of Rewind's decorators:

- `@rewind.read_only` for a tool that changes nothing. It isn't recorded.
- `@rewind.reversible(undo=..., state=...)` for a tool a rewind can undo. `undo` reverses one call. `state` is
  optional. It reads what the tool changes, and if someone has changed that since (drift), a rewind flags the effect
  instead of undoing it.
- `@rewind.irreversible()` for a tool a rewind can't undo. A rewind flags it.

Rewind registers each tool as an activity named after it, plus an undo activity for a reversible one (`add_credit` and
`undo_add_credit`).

```diff
+def _credit_state(args: dict) -> int:
+    return billing.get(args["customer_id"])["balance_cents"]
+
+
+def _undo_credit(effect: rewind.Effect, key: str) -> None:
+    billing.add_balance(effect.args["customer_id"], -effect.args["amount_cents"])
+
+
-@activity.defn
+@rewind.reversible(state=_credit_state, undo=_undo_credit)
 def add_credit(customer_id: str, amount_cents: int) -> dict:
     billing.add_balance(customer_id, amount_cents)
     return {"balance_cents": billing.get(customer_id)["balance_cents"]}
```

These tools skip idempotency keys to stay short. A real tool should take an `idempotency_key` parameter and pass it to
its API. Rewind fills it with a key that stays the same across retries and changes after a rewind, so a retry doesn't
apply twice. An undo gets its own `key` for the same reason.

### The worker (`worker.py`)

The worker registers Rewind's workflow and activities next to yours. `rewind.ACTIVITIES` includes an activity for each
tool, so import your tools first.

```diff
 Worker(
     client,
     task_queue=TASK_QUEUE,
-    workflows=[SupportAgent],
-    activities=[call_model, get_customer, apply_discount, add_credit, cancel_subscription, schedule_reply, refund],
+    workflows=[SupportAgent, *rewind.WORKFLOWS],
+    activities=[call_model, *rewind.ACTIVITIES],
 )
```

### The rewind (`run.py`)

The rewind runs as its own Temporal workflow, `rewind:support-ticket-42`, once the agent's workflow has finished. The
preview is optional and changes nothing. It reads each effect's state to check for drift, so the process that calls
it must import your tools.

```python
# Returns the effects a rewind would keep, undo and flag.
# plan.kept, plan.to_undo, plan.flagged, plan.drifted
plan = await rewind.preview(client, "support-ticket-42", step=3)

# Returns the effects it undid and flagged, and the rewound run's ID.
# result.undone, result.flagged, result.rewound_run_id
result = await rewind.rewind(
    client,
    "support-ticket-42",
    step=3,
    correction="Max 10% credit. Never cancel without asking.",
    task_queue=TASK_QUEUE,
)
```

---

For how a rewind finds the step, undoes effects and resets the workflow, and the design decisions behind it, read
[How Rewind works](../docs/how-it-works.md).
