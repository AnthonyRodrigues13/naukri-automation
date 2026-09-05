# Working in this repo

Conventions for any AI agent (or human) making changes here, beyond what's in
`README.md`.

## Log decisions, not just diffs

Git shows *what* changed. It doesn't show *why*. Whenever a change involves a
real tradeoff — choosing between libraries or patterns, picking a threshold
or constant based on evidence, or deciding *not* to do something — add an
entry to `DECISIONS.md` before considering the change done. Skip it only for
changes with one obviously-correct option (a typo fix, a rename). The point
is to stop future work (yours or someone else's) from re-litigating a
question that was already settled, for a reason that made sense at the time.

## Keep the execution path traceable

`FLOW.md` documents what actually calls what, in what order, across
`orchestrator.py` / `naukri_client.py` / `scoring.py` / `storage.py`. If a
change alters a call path — a new function in the chain, a reordered step, a
cycle that starts calling something it didn't before — update `FLOW.md` in
the same change. While a change is in progress, note it under "Currently in
flight" in that file so anyone picking up the work mid-stream knows which
path is unstable right now.

## Own the mental model

`DECISIONS.md` and `FLOW.md` support understanding — they don't substitute
for it. Before treating a change as finished, you should be able to explain,
in your own words and without re-reading the docs, what the code does and
why it's shaped that way. If you can't, the work isn't ready, no matter how
thorough the documentation looks. The docs exist to help you (and whoever
comes after) build that understanding faster — not to give you an excuse to
skip building it.

## Safety-relevant code

`config.py`'s caps, thresholds, and `DRY_RUN`/`PAUSED` switches gate real
actions (submitting applications, sending messages on the user's behalf).
Changes to any of them are safety-relevant, not routine config — call them
out explicitly rather than folding them into an unrelated change.
