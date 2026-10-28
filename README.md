# Rewind: an undo button for AI agents on Temporal

Rewind undoes the real-world changes your agent made from the step where it went wrong. Then it uses Temporal's reset
to run the agent again from that step, with your correction. The agent keeps its earlier work and carries on as if the
mistake never happened.

https://github.com/user-attachments/assets/2ab747be-00c2-46d8-8cce-ac8348f8064a

The UI in the video is a mockup on scripted data. The example below runs the real library on a local Temporal
server. For an in-depth look at each step, the design decisions and what I learned about Temporal's reset, check out
[How Rewind works](docs/how-it-works.md).

## The problem

When an agent fails, it usually doesn't crash. It finishes and does the wrong thing: it issues a $3,000 credit
instead of $200, or cancels a customer it should have kept. Someone notices later. By then the mistake is in the real
world, and fixing it by hand means undoing each change and then redoing the work properly.

## What a rewind does

1. **Preview (optional).** See what a rewind would keep, undo and flag. It changes nothing.
2. **Undo.** As the agent runs, Rewind records each tool call that changes something, called an effect. A rewind
   undoes the effects from the chosen step on, newest first.
3. **Flag what it can't safely undo.** Two kinds of effect are flagged instead of undone:
   - irreversible ones, like a refund
   - ones someone else has changed since (drift). Undoing them would overwrite that edit.
4. **Correct.** Rewind saves your correction for the agent. It also tells your agent about each flagged effect, so it
   doesn't repeat it.
5. **Reset.** Temporal resets the agent's workflow to that step. The earlier steps come back from history. Then that
   step runs again with your correction.

## Using it in your agent

Mark each tool with what a rewind should do with it:

```python
@rewind.read_only          # not recorded
def get_customer(customer_id): ...

@rewind.reversible(undo=remove_credit, state=read_balance)   # undone, or flagged if the balance has drifted
def add_credit(customer_id, amount_cents): ...

@rewind.irreversible()     # flagged, since it can't be undone
def refund(customer_id, amount_cents): ...
```

Your workflow runs its model and tool calls through two Rewind helpers:

```python
from rewind import agent_workflow as rw

for step in range(1, MAX_STEPS + 1):
    reply = await rw.model_call(call_model, step, messages)
    ...
    results = await rw.tool_calls(reply["tool_calls"], step)
```

Your model activity reads the correction, and you rewind the workflow once it finishes:

```python
# In your model activity: add these to the prompt
corrections = await rewind.corrections(step)

# From your client: undo from step 3 and rerun the agent with the correction
result = await rewind.rewind(
    client,
    "support-ticket-42",
    step=3,
    correction="Max 10% credit. Never cancel without asking.",
    task_queue="support",
)
```

[`example/README.md`](example/README.md) walks through every change as a diff against the agent you'd write
without Rewind.

## Try it

`example/` is a small support agent you can run in a minute. It makes a costly mistake, and the script rewinds it on
a local Temporal server so you can see each part of a rewind.

```
git clone https://github.com/Zach-Fong/rewind.git && cd rewind
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m example.run
```

What happens:

- The agent gives an angry customer a 10% discount, credits them $3,000, cancels their plan, refunds $240 and
  schedules a reply.
- The script rewinds it to step 3 with the correction "Max 10% credit. Never cancel without asking."
- The discount is kept. The reply, the cancel and the credit are undone, and the refund is flagged since it's
  irreversible. The rewound run credits $200 and asks before cancelling.

The model, the billing system and the outbox are mocks, so every run is the same.

In the Temporal UI you'll find the original and rewound runs (both under `support-ticket-42`) and the rewind itself
(`rewind:support-ticket-42`).

## Tests

```
.venv/bin/python tests/check_tools.py     # no Temporal: recording, idempotency, undo, drift, corrections
.venv/bin/python tests/check_rewind.py    # starts its own Temporal dev server: rewind, preview, drift, repeat rewinds, refusals, failed undos, reused IDs
```

Plain scripts, not pytest: each prints PASS or FAIL per check. Both run against `tests/testbed/`, a support agent on
a fake Stripe, a fake outbox and a scripted model.

## Limitations

- Only finished workflows (anything but running) can be rewound.
- Every reversible tool needs a hand-written `undo`.
- Irreversible effects are flagged and reported, not undone.
- The ledger is a local SQLite file shared by the worker and the client.

The full list, including what can never be done, is in [How Rewind works](docs/how-it-works.md#limits), with the design
decisions and what building Rewind taught me about Temporal's reset.
