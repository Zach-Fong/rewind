# Rewind: an undo button for AI agents on Temporal

Rewind undoes the real-world changes your agent made from the step where it went wrong. Then it uses Temporal's reset
to run the agent again from that step, with your correction. The agent keeps its earlier work and carries on as if the
mistake never happened.

https://github.com/user-attachments/assets/2ab747be-00c2-46d8-8cce-ac8348f8064a

The UI in the video is a mockup on scripted data. The example below runs the real library on a local Temporal
server. For an in-depth look at each step, check out [How Rewind works](docs/how-it-works.md). For the design
decisions and what I learned about Temporal's reset, read the [Design notes](docs/design-notes.md).

## The problem

When an agent fails, it usually doesn't crash. It finishes and does the wrong thing: it issues a $3,000 credit
instead of $200, or cancels a customer it should have kept. Someone notices later. By then the mistake is in the real
world, and fixing it by hand means undoing each change and then redoing the work properly.

## What a rewind does

As your agent runs, Rewind records each tool call that changes something in the world, called an effect. It keeps
them in a local SQLite file called the ledger. When you rewind to a step:

1. **Plan.** Rewind creates a plan that you can preview before rewinding. The plan sorts the effects
   into those to keep (before the step), undo (from the step on) and flag. An effect is flagged when Rewind can't
   safely undo it:
   - it's irreversible, like a refund
   - the data it changed has been changed again since (drift), like a teammate editing the same discount. Undoing
     it would overwrite those changes.
2. **Undo.** Rewind undoes the effects from that step on, newest first, and leaves the flagged ones alone.
3. **Correct.** Rewind saves your correction for the agent, with a line for each flagged effect so the agent doesn't
   repeat it.
4. **Reset.** Temporal resets the agent's workflow to that step. The earlier steps come back from Temporal's history
   without running again. Then that step runs again with your correction.

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
- The script rewinds it to the step where it credited $3,000, with the correction "Max 10% credit. Never cancel
  without asking."
- The discount is kept. The reply, the cancel and the credit are undone, and the refund is flagged since it's
  irreversible. The rewound run credits $200 and asks before cancelling.

The model, the billing system and the outbox are mocks, so every run is the same. In the Temporal UI you'll find
the original and rewound runs (both under `support-ticket-42`) and the rewind itself (`rewind:support-ticket-42`).

To add Rewind to your own agent, read [Adding Rewind](example/README.md#adding-rewind).

## Limitations

- Only finished workflows (anything but running) can be rewound.
- Every reversible tool needs a hand-written `undo`.
- Irreversible effects are flagged and reported, not undone.
- The ledger is shared by the worker and the client, so they must run on the same machine.

---

For how a rewind works step by step, read [How Rewind works](docs/how-it-works.md). For the design decisions, what
building Rewind taught me about Temporal's reset and the full list of limits, read the
[Design notes](docs/design-notes.md).
