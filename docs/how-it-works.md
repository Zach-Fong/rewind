# How Rewind works

This walks through each step of a rewind, the ledger, Rewind's IDs and failure handling.

## Overview

A rewind goes back to the step where the agent went wrong, called step M below. It has to change three things:

| What | Lives in | Rewind's job |
|---|---|---|
| What the agent thought and did: model replies, tool results, the conversation | Temporal history | Reset it to step M |
| What the agent changed in the world: each tool call's before and after state | The ledger (SQLite) | Undo it from step M on |
| What the agent should be told in the rewound run | The ledger (SQLite) | Save the correction |

The design rests on one idea: the agent's memory never needs saving. Temporal already records every model reply and
tool result, and a reset replays them to rebuild the conversation exactly. So Rewind only manages the world and the
corrections, and asks Temporal to do the rest.

## While the agent runs

Your agent keeps the usual Temporal loop. It calls the model, runs the tools the model asks for, adds the results to
the conversation, and repeats until the model answers. Two helpers make that loop rewindable. `rewind.model_call`
runs the model activity with the ID `{step}.model`, and `rewind.tool_calls` runs each tool in parallel as an
activity named after it, with the ID `{step}.{tool}.{i}`.

A read-only tool just runs. A reversible or irreversible tool changes something in the world, so Rewind records each
call to it as an effect. An effect is a row in the ledger with the call, its before and after state, and its result.
Rewind records it in this order:

1. Read the before-state with the tool's `state(args)`, if it has one
2. Write a `pending` row to the ledger
3. Call the tool, passing it an idempotency key, if it takes one
4. Read the after-state
5. Mark the row `done`, or `flagged` for an irreversible tool

## A rewind

Only a finished agent workflow (anything but running) can be rewound. `rewind.rewind()` starts the
`rewind:{workflow_id}` workflow on the agent's task queue. That workflow does four things:

1. **Plan.** Rewind calls `rewind.preview()`, which finds step M's model call in the agent's history. That sets the
   reset point: the moment just before that call. It then reads every effect from step M on, from the original
   run and any rewound runs, and sorts them into effects to undo and effects to flag. An effect is flagged if it's
   irreversible or has drifted.
2. **Undo.** Rewind undoes each effect, newest first, as its own activity. Just before each undo it checks for drift
   again. If someone has changed the state since, it flags the effect and leaves the world alone.
3. **Save the correction.** Rewind saves your correction in the ledger, with a line for each flagged effect, so the
   agent knows what has already happened and can't be undone. In the example, the agent sees:

   ```
   Max 10% credit. Never cancel without asking.

   Already done and NOT undone; do not repeat:
   - refund(customer_id='cus_42', amount_cents=24000) (irreversible)
   ```

   A tool can give a friendlier line with a `describe` callback ("refund $240 to cus_42").
4. **Reset.** Rewind uses Temporal's reset to take the agent's workflow back to the reset point. A reset starts a new
   run under the same workflow ID, with the history up to that point. The workflow code replays that history, so every
   model and tool call before step M returns its recorded result without running again. Step M's model call isn't in
   that history, so it runs for real and reads the correction.

Rewinding again works the same way. The plan reads effects from every run, so rewinding the rewound run to an
earlier step also undoes the original run's remaining effects from that step, and skips anything already undone.

## The ledger

The ledger holds what Temporal doesn't. It's a SQLite file at `REWIND_DB` (default `rewind.db`) with two tables: one
for the changes the agent's tools made in the world, and one for the corrections that rewinds save.

### Effects table

| Column | What it's for |
|---|---|
| Effect ID | One row per tool call, however often it's retried |
| Workflow ID | The agent workflow it belongs to |
| First run ID | Groups the workflow's runs, so a rewind sees effects from all of them |
| Run ID | The run that made it: the original run or a rewound one |
| Step | Whether it's kept (before step M) or undone (step M on) |
| Tool, arguments | Which call made it, and what the undo is given |
| Before and after state | Whether someone else has changed the state since the tool ran (drift), and what an undo can restore |
| Result | What the tool returned |
| Sequence number | The order effects were made in, for newest-first undo |
| Status | Where the effect is in its life (below) |
| Reason | Why it was flagged or failed |

### Effect statuses

| Status | Set when | What a rewind does with it |
|---|---|---|
| `pending` | Before the tool runs | Leaves it out |
| `done` | The tool finished | Undoes it, or flags it if it has drifted |
| `failed` | The tool raised, and no earlier attempt had finished (a retry reopens the row) | Ignores it |
| `reverting` | Just before its undo runs | Finishes the undo a crash interrupted |
| `reverted` | Its undo finished | Skips it |
| `flagged` | An irreversible tool finished, or a rewind found the effect drifted | Leaves it alone and lists it in the correction |

### Corrections table

| Column | What it's for |
|---|---|
| Rewind ID | One correction per rewind, however often the save is retried |
| Workflow ID, first run ID | The agent workflow it applies to, across all its runs |
| Step | The step it applies from |
| Text | Your correction, plus a line per flagged effect |

A model call at step 3 gets every correction saved for step 3 or earlier. So after several rewinds, the agent sees all
your corrections, not only the latest.

## Identifiers

Rewind uses IDs to find a step in history, to make retries safe and to tell runs apart. Most are built from Temporal's
own IDs.

| ID | Format | Example | What it guarantees |
|---|---|---|---|
| Agent workflow ID | yours | `support-ticket-42` | Stays the same across rewinds |
| Run ID | Temporal's | `01a1…`, then `01b3…` | Changes on every reset, which separates the original run from rewound runs |
| First run ID | Temporal's `first_execution_run_id` (`first_run_id` in the code) | `01a1…` | Same across resets, so a repeat rewind sees every run's effects. A new workflow started with the same ID starts with no effects |
| Model call activity ID | `{step}.model` | `3.model` | How a rewind finds step M in history. It's the same in the rewound run |
| Tool call activity ID | `{step}.{tool}.{i}` | `4.refund.1` | Unique per call in a step, and the same in the rewound run |
| Idempotency key | `{workflow_id}:{run_id}:{activity_id}` | `support-ticket-42:01a1…:3.add_credit.0` | The same across retries of one call, and different in a rewound run |
| Effect ID | `eff_` + hash of the key | `eff_9c1e…` | One ledger row per tool call, however often it's retried |
| Undo key | `undo:{effect_id}` | `undo:eff_9c1e…` | Each undo applied at most once |
| Rewind workflow ID | `rewind:{workflow_id}` | `rewind:support-ticket-42` | At most one rewind of an agent at a time |
| Rewind ID | the rewind workflow's run ID | `01a2…` | One saved correction per rewind, however often the save is retried. It's also the reset's request ID |

The Temporal UI shows these names:

| Name | What it is |
|---|---|
| `{tool}` | A tool's activity, named after the tool's function |
| `undo_{tool}` | A reversible tool's undo activity |
| `rewind.Rewind` | The rewind workflow |
| `rewind.preview` | The rewind's first activity, which makes the plan |
| `rewind.reset_agent` | The rewind's last activity, which saves the correction and resets the agent |

## Failure handling

Rewind relies on Temporal to retry activities and resume workflows. Each attempt gets 60 seconds. The agent's model
and tool calls get 5 attempts, which you can change with `start_to_close_timeout` and `retry_policy` on
`rewind.model_call` and `rewind.tool_calls`. Rewind's own activities keep retrying for up to an hour, and each is safe
to repeat. Your tools and undos are safe to repeat too, as long as they pass their idempotency key on to your API.

| Failure | What happens |
|---|---|
| The worker dies during a tool call | Temporal retries the call with the same idempotency key, so the API applies it once. Without a key, the call runs again. The retry keeps the before-state the first attempt saved, because reading the state again could pick up a change the first attempt already made |
| A tool call succeeds but Temporal never gets the result, and the retry raises | The row stays `done` (or `flagged`), because the first attempt really changed the world. A rewind still undoes or reports it |
| The worker dies during an undo | The workflow resumes. The row is `reverting`, so the retry runs the undo again with the same undo key, and the API applies it once |
| An undo keeps failing | Rewind retries it for up to an hour, or stops at once on a non-retryable error. The rewind then fails before saving the correction, so the agent isn't reset. Running the same rewind again undoes what's left and finishes it |
| The worker dies while saving the correction or resetting | The retry saves the correction again under the same rewind ID, which changes nothing. If the reset already happened, Temporal has recorded the rewound run on the original run, so the retry returns it instead of resetting again |
| The workflow gets a newer run during the rewind (a new workflow with the same ID) | The reset is refused, so the rewind fails after its undos, with no correction saved |

---

For the design decisions, what I verified about Temporal's reset and Rewind's limits, read
[Design notes](design-notes.md).
