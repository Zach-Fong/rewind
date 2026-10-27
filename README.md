# Rewind

**A rewind button for AI agents running on Temporal.**

Rewind undoes the real-world changes your agent made from the step where it went wrong. Then it uses Temporal's reset
to run the agent again from that step, with your correction. The agent keeps its earlier work and carries on as if the
mistake never happened.

## Demo
https://github.com/user-attachments/assets/56bce03f-557e-4af3-9d2c-957acaaacd09

The UI in the video is a mockup on scripted data. The example below runs the real library on a local Temporal
server.

For an in-depth look at each step, the design decisions and what I learned about Temporal's reset, check out
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

## Try it

`example/` is a small support agent you can run in a minute. It makes a costly mistake, and the script rewinds it on
a local Temporal server so you can see each part of a rewind.

```
git clone https://github.com/Zach-Fong/rewind.git && cd rewind
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m example.run
```

What happens:

- The agent credits an angry customer $3,000, cancels their plan and refunds $240.
- The script rewinds it to step 2 with the correction "Max 10% credit. Never cancel without asking."
- The cancel and the credit are undone, and the refund is flagged since it's irreversible. The rewound run credits
  $200 and asks before cancelling.

The model and the billing system are mocks, so every run is the same.

In the Temporal UI you'll find the original and rewound runs (both under `support-ticket-42`) and the rewind itself
(`rewind:support-ticket-42`).

## Using it in your agent

Rewind wraps your agent's model and tool calls. The model call gets an ID a rewind can find, and each tool call is
recorded as an effect in Rewind's ledger. The ledger is a SQLite file at `REWIND_DB` (default `./rewind.db`), and the
worker and the client must share it. The full before-and-after diff is in [`example/README.md`](example/README.md).

**Tools.** Each tool becomes a Temporal activity. Its decorator tells Rewind how to treat it in a rewind.

```python
import rewind

@rewind.read_only          # changes nothing in the world, so it isn't recorded
def get_customer(customer_id): ...

@rewind.reversible(
    undo=remove_credit,    # remove_credit(effect, key): reverses one add_credit call
    state=read_balance,    # optional. read_balance(args): if the balance has drifted, a rewind flags it instead of undoing it
)
def add_credit(customer_id, amount_cents): ...

@rewind.irreversible()     # a rewind flags it and tells the agent it has happened and cannot be undone
def refund(customer_id, amount_cents): ...
```

A tool with an `idempotency_key` parameter gets a key that stays the same across retries and changes after a rewind.
Pass it to your API so a retry doesn't apply twice.

**The agent workflow.** Each step of the agent's loop goes through two helpers instead of
`workflow.execute_activity`:

```python
from rewind import agent_workflow as rw

for step in range(1, MAX_STEPS + 1):
    reply = await rw.model_call(call_model, step, messages)    # runs call_model(step, messages)
    ...
    results = await rw.tool_calls(reply["tool_calls"], step)   # runs and records the tools the model asked for
```

`step` is the loop's step number. Rewind uses it to find each step in history and to look up its corrections, so your
model activity takes it as its first argument. The other arguments (here, the conversation so far) are yours and are
passed through unchanged.

**The model activity** reads the corrections that apply to its step:

```python
corrections = await rewind.corrections(step)   # empty unless a rewind went back to this step or earlier
# add them to the prompt, and return them in the result so they show in history
```

**The worker** runs Rewind's workflow and activities next to yours. Import your tools first:

```python
Worker(client, task_queue="support", workflows=[SupportAgent, *rewind.WORKFLOWS],
       activities=[call_model, *rewind.ACTIVITIES])
```

**Rewinding** an agent workflow that has finished:

```python
# Optional: preview what a rewind to step 2 would do. Changes nothing.
plan = await rewind.preview(client, "support-ticket-42", step=2)
print(plan.to_undo)      # effects it will undo, newest first
print(plan.drifted)      # drifted: will be flagged, not undone
print(plan.flagged)      # irreversible, or flagged by an earlier rewind. The agent will be told

# Rewind: undo those effects, then rerun the agent from step 2 with the correction.
result = await rewind.rewind(client, "support-ticket-42", step=2,
                             correction="Max 10% credit. Never cancel without asking.",
                             task_queue="support")
print(result.undone)           # effects undone
print(result.flagged)          # flagged effects (irreversible, or drifted)
print(result.rewound_run_id)   # the rewound run's ID (same workflow ID)
```

The preview reads each effect's state to check for drift, so the process that calls it must import your tools too.

## Tests

```
.venv/bin/python tests/check_tools.py     # no Temporal: recording, idempotency, undo, drift, corrections
.venv/bin/python tests/check_rewind.py    # starts its own Temporal dev server: rewind, preview, drift, repeat rewinds, refusals, failed undos, reused IDs
```

Plain scripts, not pytest: each prints PASS or FAIL per check. Both run against `tests/testbed/`, a support agent on
a fake Stripe, a fake outbox and a scripted model.

## Isn't this just a saga?

The undo half is. Recording an undo for each change and running them in reverse is the saga pattern. What's different:

- **A person triggers it after the run.** Nothing failed. A reviewer decided a step was wrong.
- **It restores the agent, not just the world.** A saga leaves the world undone and the workflow over. Here the agent
  carries on from the chosen step with its earlier work intact.
- **The rewound run isn't a retry.** It has a correction, so it makes a different decision.

The other half, going back to an earlier point in the agent's state, is what checkpoint-and-fork features like
LangGraph's time travel do. Those restore the agent's memory but not the credit it already issued. Rewind does both,
and Temporal's history is what makes going back cheap for the agent.

## Limitations

- Only finished workflows (anything but running) can be rewound.
- Every reversible tool needs a hand-written `undo`.
- Irreversible effects are flagged and reported, not undone.
- The ledger is a local SQLite file shared by the worker and the client.

The full list, including what can never be done, is in [How Rewind works](docs/how-it-works.md#limits), with the design
decisions and what building Rewind taught me about Temporal's reset.
