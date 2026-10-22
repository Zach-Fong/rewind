# Rewind design

How Rewind works, why it's built this way, and what it can't do.

## Overview

A rewind goes back to the step where the agent went wrong, called step M below. It has to change three things, kept
in two places:

| What | Lives in | Rewind's job |
|---|---|---|
| What the agent thought and did: model replies, tool results, the conversation | Temporal history | Reset it to step M |
| What the agent changed in the world: each tool call's before and after state | The ledger (SQLite) | Undo it from step M on |
| What the agent should be told in the rewound run | The ledger (SQLite) | Save the correction |

The design rests on one idea: the agent's memory never needs saving. Temporal already records every model reply and
tool result, and a reset replays them to rebuild the conversation exactly. So Rewind only manages the world and the
corrections, and asks Temporal to do the rest.

## A normal run

The agent's loop is ordinary: call the model, run the tools it asks for in parallel, add the results to the
conversation, repeat until it answers. Two helpers make it rewindable. `rw.model_call` runs the model activity with
the ID `{step}.model`, and `rw.tool_calls` runs each tool as an activity named after it, with the ID
`{step}.{tool}.{i}`.

A read-only tool just runs. A reversible or irreversible tool is recorded, in this order:

1. Read the before-state with the tool's `state(args)`, if it has one
2. Write a `pending` row to the ledger
3. Call the tool, passing the idempotency key if it accepts one
4. Read the after-state
5. Mark the row `done`, or `flagged` for an irreversible tool

## The ledger

The ledger holds what Temporal doesn't: the agent's effects on the world, and the corrections for rewound runs. It's a
SQLite file with a table for each.

### Effects

Each call to a reversible or irreversible tool is saved as one effect:

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

An effect's status follows it from the tool call to its undo:

| Status | Set when | What a rewind does with it |
|---|---|---|
| `pending` | Before the tool runs | Leaves it out |
| `done` | The tool finished | Undoes it, or flags it if it has drifted |
| `failed` | The tool raised, and no earlier attempt had finished (a retry reopens the row) | Ignores it |
| `reverting` | Just before its undo runs | Finishes the undo a crash interrupted |
| `reverted` | Its undo finished | Skips it |
| `flagged` | An irreversible tool finished, or a rewind found the effect drifted | Leaves it alone and lists it in the correction |

### Corrections

Each rewind saves one correction:

| Column | What it's for |
|---|---|
| Rewind ID | One correction per rewind, however often the save is retried |
| Workflow ID, first run ID | The agent workflow it applies to, across all its runs |
| Step | The step it applies from |
| Text | Your correction, plus a line per flagged effect |

A model call gets every correction saved for its workflow at its step or earlier, oldest first, so repeat rewinds add up.

## A rewind

Only a finished agent workflow (anything but running) can be rewound. `rewind()` starts the
`rewind:{workflow_id}` workflow on the agent's task queue. That workflow does four things:

1. **Plan.** Rewind calls `rewind.preview()`, which finds step M's model call in the agent's history. That sets the
   reset point: the moment just before that call. It then reads every effect from step M on, from all of the
   workflow's runs, and sorts them into effects to undo and effects to flag.
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
4. **Reset.** Rewind resets the agent's workflow to the reset point.

Temporal then starts the rewound run under the same workflow ID. The workflow code runs from the top, and every model
and tool call before step M is answered from history, so the conversation comes back exactly and nothing runs twice.
Step M's model call is no longer in history, so it runs for real and reads the correction. From there the run
carries on as normal.

**Rewinding again** works the same way. The plan reads effects from every run, so rewinding the rewound run to an
earlier step also undoes the original run's remaining effects from that step, and skips anything already undone.

## Identifiers

Most of Rewind's guarantees come down to choosing the right ID, and most IDs come from Temporal's own.

| ID | Format | Example | What it guarantees |
|---|---|---|---|
| Agent workflow ID | yours | `support-ticket-42` | Stays the same across rewinds |
| Run ID | Temporal's | `01a1…` | Changes on every reset, which separates the original run from rewound runs |
| First run ID | Temporal's `first_execution_run_id` (`first_run_id` in the code) | `01a1…` | Same across resets, so a repeat rewind sees every run's effects. A new workflow started with the same ID starts empty |
| Model call activity ID | `{step}.model` | `2.model` | How a rewind finds step M in history. It's the same in the rewound run |
| Tool call activity ID | `{step}.{tool}.{i}` | `3.refund.1` | Unique per call in a step, and the same in the rewound run |
| Idempotency key | `{workflow_id}:{run_id}:{activity_id}` | `support-ticket-42:01a1…:2.add_credit.0` | The same across retries of one call, and different in a rewound run |
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

## Design decisions

| Decision | Why |
|---|---|
| Reset to a model call, not a tool | Temporal can only reset to a point where the workflow decided what to do next. Resetting to just before step M's model call makes the model decide again. Resetting to a tool call would only repeat the old decision |
| Rebuild the agent's state from history, don't snapshot it | Temporal's history already holds everything the agent thought and did. The cost is Temporal's usual rules: the workflow must build its state only from activity results, and its code can't change between the original run and the rewind |
| Store the correction instead of signalling it | A signal can't reach a completed run, and a signal sent to the rewound run races its first step. Saving the correction to the ledger, where the model activity reads it, avoids both problems. The cost is another table, and history only shows the correction if the model activity returns it |
| Make the rewind a workflow | If the worker or caller dies halfway, the rewind carries on. Its workflow ID works as a lock, and its runs are the record of past rewinds. The cost is that rewinding needs a worker running, and a refusal shows up as a failed rewind run |
| Undo newest first | Later changes can build on earlier ones, so they're undone in reverse order |
| Flag drift, don't overwrite it | If the state isn't what the tool left, someone else changed it, and an undo would wipe out their change. Rewind flags the effect instead and tells the agent. A tool whose undo reverses a relative change (subtracting the credit it added) can skip the check by leaving out `state` |
| Put the run ID in the idempotency key | The rewound run reuses the original run's activity IDs. Without the run ID, the rewound run's `add_credit` would send the original key just after that credit was undone, and the API would return its cached response without crediting. With it, a retry gets the same key and a rewound run gets a new one |
| Tell the agent about flagged effects instead of stopping | A rewind doesn't wait for a person to approve irreversible effects. Each flagged effect becomes an "already done" line in the correction, and stays in the ledger for a person to review |

## Failure handling

Rewind relies on Temporal to retry activities and resume workflows. Each attempt gets 60 seconds. The agent's model
and tool calls get 5 attempts, which you can change with `start_to_close_timeout` and `retry_policy` on
`rw.model_call` and `rw.tool_calls`. Rewind's own activities keep retrying for up to an hour. Running any of them twice
has the same result as running it once, as long as your tools and undos pass their idempotency key on to your API.

| Failure | What happens |
|---|---|
| The worker dies during a tool call | The `pending` row, written before the call, stays. The retry finds it through the key and keeps the first attempt's before-state, not the half-changed world |
| The worker dies after the tool's API call, before `done` | The retry sends the same key, and the API returns its stored response. Without a key, the call runs again |
| A retry raises after an earlier attempt finished | The row stays `done` (or `flagged`), so a rewind still undoes or reports the effect |
| The caller dies during a rewind | The rewind carries on, because it runs as a workflow on the worker |
| The worker dies during an undo | The workflow resumes. The row is `reverting`, so the retry runs the undo again with the same undo key, and the API applies it once |
| An undo keeps failing | Rewind retries it for up to an hour, or stops at once on a non-retryable error. The rewind then fails before saving the correction, so the agent isn't reset. Running the same rewind again undoes what's left and finishes it |
| The worker dies after saving the correction | The retry saves it again under the same rewind ID, which changes nothing |
| The worker dies after the reset, before reporting it | Temporal records which run the planned run was reset to, so the retry returns that run instead of resetting again |
| The workflow gets a newer run during the rewind (a new workflow with the same ID) | The reset is refused, so the rewind fails after its undos, with no correction saved |
| A second rewind while one runs | Temporal refuses to start it |
| The workflow is missing or still running, or step M was never reached | The plan fails at once, before anything has changed |

## Limits

### Not supported yet: has a known fix

| Limit | Why | Fix |
|---|---|---|
| Rewind a running workflow | A reset doesn't stop tool calls in flight, so they could change the world after the undo list is made | Pause the agent and wait for in-flight calls first. Today, terminate it, wait for its running tool calls to finish, then rewind |
| Record tool calls made outside `rw.tool_calls` | Rewind takes over how tools are dispatched | A worker interceptor that records ordinary activities |
| Share a ledger across machines | The ledger is one SQLite file that the worker and the caller both open | A pluggable store, and run the preview on the worker |
| Rewind after the workflow code changed | Replay needs the code that made the original run | Temporal's workflow versioning |
| Keep signals and updates sent after step M | The reset reapplies none of them, so a signal the agent received after step M (an approval, say) is gone from the rewound run | Let the caller choose which signals carry over (an approval, but not a pause), and send those to the rewound run |
| Preview two effects on the same state correctly | The preview checks each effect against the world as it is now. With two credits on one balance, it lists the older one as drifted, but the rewind undoes the newer one first and then the older one too. Two calls in one step run in parallel, so both can also read their before-state before either has run | Check each effect against the state the newer undos leave |
| Rewind to a later step after a rewind failed | A rewind whose undo fails stops with some effects undone that the agent still remembers. Running it again, or rewinding to an earlier step, is safe. Rewinding to a later step replays a history that no longer matches the world | Refuse it until the failed rewind is finished |
| Store tool results and state that aren't plain JSON | The ledger stores them as JSON. A tool that returns a dataclass fails after its call, and a state with tuples always looks drifted | Store them with Temporal's payload converter |
| Account for `pending` effects | An effect left `pending` (the workflow closed while its tool ran, or its last retry crashed) is left out of the plan and the correction | List it in the correction as possibly done |
| Rewind past a continue-as-new | A continue-as-new keeps `first_execution_run_id` but restarts the step numbers, so steps before it can't be reached | Carry the step count into the new run |
| Keep large plans out of the rewind's history | The rewind passes the whole plan, with every effect's arguments, state and result, through its history three times. With large tool results, that can bloat the history or hit Temporal's payload size limit | Pass the reset only the IDs it needs, and leave the effects in the ledger |
| Async tools | A tool must be a plain function. Rewind refuses an async tool, or a second tool with the same name, when it's registered | |
| Child workflows or subagents | A rewind only sees the effects recorded under the agent's own workflow ID, not a child's | |

### Inherent: can't be fixed

| Limit | Why |
|---|---|
| Undo irreversible effects | Money that left, an email that was read. Rewind can only flag them and tell the agent. The practical answer is a delay window. The testbed's outbox holds replies until their send time, so a rewind can still cancel them |
| Undo a tool nobody wrote an undo for | A rewind can only run the undo you wrote |
| Notice drift that ends where it started | The check compares the current state with the state the tool left. A change that was made and then reversed looks like no change |
| Undo knock-on effects | If a customer read the email or another system reacted to the credit, that is outside the tool's state |
| Guarantee the rewound run follows the correction | That's up to the model |

## What I verified about Temporal's reset

Building Rewind meant testing how Temporal's reset behaves at its edges. These are the findings that shaped the design.

| Finding | How I found it | What it changed in Rewind |
|---|---|---|
| A signal sent after a reset races the rewound run's first step, and Temporal can't reset and signal in one step | Reset a completed workflow, then signalled the rewound run. The signal reached step M's model call in 28 of 30 trials when sent at once, and in none when sent 50 ms later. The [server source](https://github.com/temporalio/temporal/tree/92aff3961b3842a3573cac1a96c9d627b6e2cd0d) shows `performPostResetOperations` only supports `UpdateWorkflowOptions` | The correction is saved in the ledger, and the model activity reads it |
| Reset replays activity IDs, including custom ones, but run IDs change | Compared activity IDs in the original and rewound runs in the checks. Read the activity counter in the [SDK source](https://github.com/temporalio/sdk-python/tree/e47c9629b75a48e21abd4f72fed7f72a27562f53) (`temporalio/worker/_workflow_instance.py`) | A rewind finds step M by `{step}.model`, and idempotency keys include the run ID |
| Reset doesn't stop work in flight | Reset while an activity was running. It still made its change, but the server rejected its result, so the change never appeared in the new history | Only finished workflows can be rewound |
| Signalling the original run before the reset works, but only while it's open, and reapply brings back every later signal | Signalled the original run, then reset with signal reapply. The signal was handled first in 20 of 20 trials. A `pause` sent earlier came back too, and the rewound run started paused | `reset_agent` resets with no signals or updates reapplied, so signals after step M are lost (a known limit) |
| Resetting twice with the same `request_id` made two runs | Sent the same reset request twice | `reset_agent` checks whether the planned run was already reset (its `reset_run_id`), so a retry doesn't reset twice |

Tested on Temporal Server 1.32.0 (CLI 1.9.1 dev server), Python SDK `temporalio` 1.34.0 and Python 3.10.

## If this were native to Temporal

| Feature | Why |
|---|---|
| A signal as a `PostResetOperation` | Reset and send the correction in one step, with no race, even on completed runs |
| Reset to a marker | Reset to "the activity with ID `3.model`" instead of computing a workflow-task boundary from event IDs |
| Wait for running activities, then reset | A reset mode that waits for or cancels in-flight activities, so late changes can't escape the new history |
| A run-aware idempotency token | A stable `(run_id, activity_id)` token in activity info, documented as safe across resets |
| Reset lineage in the UI | Show the original run, the reset and the rewound run as a fork, with a "rewind with correction" action |

None of these is specific to agents. They make reset safer for any workflow with side effects. Agents just make the
need obvious, because "the code ran correctly and the decision was wrong" is how they usually fail.

## Testing

`tests/check_tools.py` runs without Temporal and calls the recording code directly. It checks that:

- read-only tools aren't recorded, and reversible ones are, with their before and after state
- a retry reuses its row and doesn't apply twice, and a new run ID runs the tool again
- each reversible tool's undo restores its before-state
- drift is caught (a manual edit, an email already sent)
- irreversible tools are flagged, and a reversible tool without `state` is undone without a drift check
- a tool that raises leaves a `failed` row, and a crash after the call leaves a `pending` one, even for an
  irreversible tool
- a retry that raises after an earlier attempt finished keeps the row `done`
- an undo retried after it took effect is applied once
- corrections apply from their step on
- an async tool, or a second tool with the same name, is refused

`tests/check_rewind.py` starts its own dev server and runs the testbed's agent on several tickets:

- **A.** Rewind ticket 4 to step 3: step 2 is kept, steps 3–5 are undone, the refund is flagged, the rewound run
  follows the correction, and the other tickets are untouched
- **B.** The preview changes nothing and predicts the real rewind
- **C.** A manual edit and an email already sent (drift) are flagged, not overwritten
- **D.** Rewinding again to step 2 undoes only what's still `done`, and corrections add up
- **E.** Refusals: a running workflow, an unreached step, an empty correction, a rewind already in progress
- **F.** An undo that fails stops the rewind before the reset, with no correction saved. Running it again finishes
  it, and a retried reset doesn't reset twice
- **G.** A new workflow that reuses the workflow ID: its rewind sees none of the old one's effects or corrections

Not verified: worker crashes during a real Temporal run (retries are covered without Temporal), and a real model.
