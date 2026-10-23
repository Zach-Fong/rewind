# Example: rewind a support agent

A standard Temporal agent loop handles one angry customer. The policy says "be generous", so at step 2 it credits
$3,000 (150% of a $2,000/month plan), and at step 3 it cancels their subscription and refunds $240. Then you rewind it
to step 2 with your correction, "Max 10% credit. Never cancel without asking." The rewind does three things:

- the cancel and the $3,000 credit are undone (newest first)
- the $240 refund is irreversible, so it's flagged, and the agent is told it already happened
- the workflow is reset to step 2. Step 1 comes back from Temporal's history, and step 2 runs again with the
  correction. The agent credits $200, mentions the refund already sent, and asks before cancelling

```
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m example.run
```

The model is a scripted stand-in for an LLM (`model.py`), so every run is the same. The billing system is a fake
one-table SQLite file (`billing.py`).

```
agent.py     the agent workflow: the standard loop
tools.py     four tools: get_customer (read-only), add_credit, cancel_subscription (reversible), refund (irreversible)
model.py     the model call (scripted)
worker.py    the worker: your workflow and activities, plus Rewind's
billing.py   the fake billing system the tools change
run.py       runs the agent, rewinds it, prints both runs
```

## What Rewind changes in your agent

Without Rewind, this is the agent you'd write, with the model call and each tool as activities. With Rewind, the
tools become plain functions, and Rewind turns each one into an activity named after it (`add_credit`), plus an undo
activity for each reversible tool (`undo_add_credit`). In the Temporal UI they look like any other activity.

**The workflow** runs the model and tool calls through two helpers, so they get activity IDs a rewind can find
(`2.model`, `2.add_credit.0`) and each tool call is recorded:

```diff
 for step in range(1, MAX_STEPS + 1):
-    reply = await workflow.execute_activity(call_model, messages, start_to_close_timeout=TIMEOUT)
+    reply = await rw.model_call(call_model, step, messages)
     messages.append({"role": "assistant", "content": reply["text"], "tool_calls": reply["tool_calls"]})

     if not reply["tool_calls"]:
         return reply["text"]
-    results = await asyncio.gather(*(
-        workflow.execute_activity(c["name"], args=list(c["args"].values()), start_to_close_timeout=TIMEOUT)
-        for c in reply["tool_calls"]))
+    results = await rw.tool_calls(reply["tool_calls"], step)
     messages.append({"role": "tool", "content": results})
```

**The model call** reads the corrections a rewind saved for this step:

```diff
 @activity.defn
-async def call_model(messages: list[dict]) -> dict:
-    return llm(SYSTEM_PROMPT, messages, TOOLS)
+async def call_model(step: int, messages: list[dict]) -> dict:
+    corrections = await rewind.corrections(step)  # add these to the prompt
+    return {**llm(SYSTEM_PROMPT, messages, TOOLS, corrections), "corrections": corrections}
```

**Each tool** swaps `@activity.defn` for a Rewind decorator that says what kind it is. A reversible tool also gets an
`undo`, and optionally a `state` that reads what the tool changes. A rewind only undoes the effect if that state still
matches what the tool left. If someone else has changed it (drift), the effect is flagged instead. These tools skip
idempotency keys to stay short. A real tool should take one, so a retry doesn't apply twice.

```diff
+def _balance(args):
+    return billing.get(args["customer_id"])["balance_cents"]
+
+def _undo_credit(effect, key):
+    billing.add_balance(effect.args["customer_id"], -effect.args["amount_cents"])
+
-@activity.defn
+@rewind.reversible(state=_balance, undo=_undo_credit)
 def add_credit(customer_id: str, amount_cents: int) -> dict:
     billing.add_balance(customer_id, amount_cents)
     return {"balance_cents": billing.get(customer_id)["balance_cents"]}
```

**The worker** registers Rewind's workflow and activities next to yours. `rewind.ACTIVITIES` includes the tool
activities, so the tools aren't listed one by one. Import your tools before reading
`rewind.ACTIVITIES`, as `worker.py` does.

```diff
-Worker(client, task_queue=TASK_QUEUE, workflows=[SupportAgent],
-       activities=[call_model, get_customer, add_credit, cancel_subscription, refund])
+Worker(client, task_queue=TASK_QUEUE, workflows=[SupportAgent, *rewind.WORKFLOWS],
+       activities=[call_model, *rewind.ACTIVITIES])
```

**The rewind** itself, once the agent's workflow has finished:

```python
plan = await rewind.preview(client, "support-ticket-42", 2)       # changes nothing
result = await rewind.rewind(client, "support-ticket-42", 2, "Max 10% credit. Never cancel without asking.",
                             task_queue=TASK_QUEUE)
```

The rewind runs as a Temporal workflow with the ID `rewind:support-ticket-42`. It undoes the effects, saves the
correction, and resets the agent's workflow to step 2's model call. The reset keeps the workflow ID, so in the Temporal
UI you'll see two runs of `support-ticket-42`: the original and the rewound one. The rewind's own history shows the
undos it ran: `undo_cancel_subscription`, then `undo_add_credit`.
