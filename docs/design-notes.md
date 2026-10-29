# Design notes

This walks through Rewind's design decisions, what I verified about Temporal's reset, Rewind's limits and the
features that would make it native to Temporal. It uses the terms defined in [How Rewind works](how-it-works.md).

## Design decisions

| Decision | Why |
|---|---|
| Reset to a model call, not a tool | Temporal can only reset to a point where the workflow decided what to do next. Resetting to just before step M's model call makes the model decide again. Resetting to a tool call would only repeat the old decision |
| Rebuild the agent's state from history, don't snapshot it | Temporal's history already holds everything the agent thought and did. The cost is Temporal's usual rules: the workflow must build its state only from activity results, and its code can't change between the original run and the rewind |
| Store the correction instead of signalling it | A signal can't reach a completed run, and a signal sent to the rewound run races its first step. Saving the correction to the ledger, where the model activity reads it, avoids both problems. The cost is another table, and history only shows the correction if the model activity returns it |
| Make the rewind a workflow | If the worker or caller dies halfway, the rewind carries on. Its workflow ID works as a lock, and its runs are the record of past rewinds. The cost is that rewinding needs a worker running, and a refusal shows up as a failed rewind run |
| Flag drift, don't overwrite it | If the state isn't what the tool left, someone else changed it, and an undo would wipe out their change. Rewind flags the effect instead and tells the agent. A tool whose undo reverses a relative change (subtracting the credit it added) can skip the check by leaving out `state` |
| Put the run ID in the idempotency key | A rewound run reuses the original run's activity IDs. With only those in the key, a tool call in the rewound run would send the same key as the original call, and the API would return the old response without acting. The run ID changes on a reset, so a retry keeps its key and a rewound run gets a new one |
| Tell the agent about flagged effects instead of stopping | A rewind doesn't wait for a person to approve irreversible effects. Each flagged effect becomes an "already done" line in the correction, and stays in the ledger for a person to review |

## What I verified about Temporal's reset

| Finding | How I found it | What it changed in Rewind |
|---|---|---|
| A signal sent after a reset races the rewound run's first step, and Temporal can't reset and signal in one step | Reset a completed workflow, then signalled the rewound run. The signal reached step M's model call in 28 of 30 trials when sent at once, and in none when sent 50 ms later. The [server source](https://github.com/temporalio/temporal/tree/v1.32.0) shows `performPostResetOperations` only supports `UpdateWorkflowOptions` | Store the correction instead of signalling it |
| Reset replays activity IDs, including custom ones, but run IDs change | Compared activity IDs in the original and rewound runs in `tests/check_rewind.py`. Read the activity counter in the [SDK source](https://github.com/temporalio/sdk-python/tree/1.34.0) (`temporalio/worker/_workflow_instance.py`) | Reset to a model call, found by its `{step}.model` ID. Put the run ID in the idempotency key |
| Reset doesn't stop work in flight | Reset while an activity was running. It still made its change, but the server rejected its result, so the change never appeared in the new history | Only finished workflows can be rewound |
| A reset can reapply signals, but only every signal or none. A correction signalled to the original run comes through, but so does every other signal sent after the reset point. Only an open run can be signalled at all | Signalled the original run, then reset with signal reapply. The signal was handled first in 20 of 20 trials. A `pause` sent earlier came back too, and the rewound run started paused | Rewind resets with no signals or updates reapplied, so any sent after step M are lost |
| Resetting twice with the same `request_id` made two runs | Sent the same reset request twice | Rewind checks whether the original run was already reset, because Temporal records the new run's ID on it. So a retry doesn't reset twice |

Tested on Temporal Server 1.32.0 (CLI 1.9.1 dev server), Python SDK `temporalio` 1.34.0 and Python 3.10.
`tests/check_rewind.py` checks these behaviours, and each rewind scenario, on a local Temporal dev server.

## Limits

### When adopting Rewind

| Limit | Why | Fix |
|---|---|---|
| Async tools | Rewind runs each tool in a thread, so a tool must be a plain function. It refuses an async one when it's registered | Await async tools and their callbacks instead of running them in a thread |
| Tool results and state that aren't plain JSON | The ledger stores them as JSON. A tool that returns a dataclass changes the world, then fails to save its result. Each retry runs it again, and the effect stays `pending`. A state with tuples always looks drifted | Store them with Temporal's payload converter |
| Tool calls made outside `rewind.tool_calls` | Rewind takes over how tools are dispatched, so it doesn't see other activities | A worker interceptor that records ordinary activities |
| Rewinding a running workflow | A reset doesn't stop tool calls in flight, so they could change the world after the undo list is made | Pause the agent and wait for in-flight calls first. Today, terminate it, wait for its running tool calls to finish, then rewind |
| Workflow code that changed since the run | Replay needs the code that made the original run | Temporal's workflow versioning |
| Signals and updates sent after the chosen step | The reset reapplies none of them, so a signal the agent received after that step (an approval, say) is gone from the rewound run | Let the caller choose which signals carry over (an approval, but not a pause), and send those to the rewound run |
| Continue-as-new | A continue-as-new starts a fresh history to keep it small. It keeps `first_execution_run_id` but restarts the step numbers, so steps before it can't be reached, and a rewind undoes effects from before it | Carry the step count into the new run |
| Child workflows and subagents | A rewind only sees the effects recorded under the agent's own workflow ID, not a child's | File a child's effects under its parent, so a rewind of the parent undoes them too |
| Sharing a ledger across machines | The ledger is one SQLite file that the worker and the caller both open | A pluggable store, with the preview run on the worker |

### Edge cases

| Limit | Why | Fix |
|---|---|---|
| Rewinding to a later step after a rewind failed | A failed rewind may already have undone some effects that are still in the agent's history. Rewinding to a step after one of them replays it as if it still happened, so the agent believes a change that's no longer in the world | Refuse it until the failed rewind is finished |
| Resetting the agent outside Rewind during a rewind | Rewind can mistake that reset for its own, and report success without the agent getting the correction | Check that the reset was made by this rewind |
| `pending` and `failed` effects | An effect stays `pending` if the workflow closed while its tool ran, and becomes `failed` if its tool raised on its last attempt. Either may still have changed the world (say, the API's reply was lost), but a rewind neither undoes it nor lists it in the correction | List them in the correction as possibly done |
| Two effects on the same state | The preview checks each effect against the world as it is now. If the agent credits $100 and then $50, the preview compares the $100 credit's after-state with today's balance, which includes the $50, so it wrongly lists the older credit as drifted. With two parallel calls in one step, the rewind itself can flag one, because each after-state can include the other's change | Check each effect against the state the newer undos leave, and undo parallel calls in the order they finished |
| Large plans in the rewind's history | The rewind's history carries the whole plan, with every effect's arguments, state and result, three times. Large tool results can bloat it or hit Temporal's payload size limit | Pass the reset only the IDs it needs, and leave the effects in the ledger |

### Can't be fixed

| Limit | Why |
|---|---|
| Irreversible effects | Money that left or an email that was read can't be taken back. Rewind can only flag them and tell the agent. The practical answer is a delay window. The example's outbox holds replies until their send time, which is why a rewind can still cancel the scheduled reply |
| Undos you didn't write | Only you know how to reverse a call to your API, so each reversible tool needs an undo you write |
| Drift that ends where it started | The check compares the current state with the state the tool left. A change that was made and then reversed looks like no change |
| Knock-on effects | If a customer read the email or another system reacted to the credit, Rewind can't see or undo that |
| Following the correction | Whether the rewound run follows your correction is up to the model |

## If this were native to Temporal

| Feature | What it would do |
|---|---|
| A signal as a `PostResetOperation` | Reset and send the correction in one step, with no race, even on completed runs |
| Reset to an activity ID | Reset to "the activity with ID `3.model`" instead of computing a workflow-task boundary from event IDs |
| Wait for running activities, then reset | A reset mode that waits for or cancels in-flight activities, so late changes can't escape the new history |
| A run-aware idempotency token | A stable `(run_id, activity_id)` token in activity info, documented as safe across resets |
| Reset lineage in the UI | Show the original run, the reset and the rewound run as a fork, with a "rewind with correction" action |

None of these is specific to agents. They make reset safer for any workflow with side effects. Agents just make the
need obvious, because "the code ran correctly and the decision was wrong" is how they usually fail.
