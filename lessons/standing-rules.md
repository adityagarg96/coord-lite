# Standing rules for working with coding agents

Generalized from incidents in real agent sessions. Each rule is the mechanism behind a failure that actually
happened; the specifics are removed. `agent-rules.md` beside this file is the longer, corpus-derived set.

## Delete before you build

1. **Question the requirement first, whoever set it.** Ask which goal a piece of work serves and whether that
   goal should die. "Someone senior asked for it" is the most dangerous justification a requirement has.
2. **Default to deletion.** If you never have to add back something you cut, you are not cutting enough.
   Money already spent and tooling already built are never reasons to keep going: that is sunk cost
   wearing the costume of a requirement.
3. **Order matters: question, delete, simplify, speed up, automate,** in that order. The classic error is
   polishing or automating something that should not exist.
4. **An intermediate implementation built only to be ported is a part to delete.** If the target
   implementation is needed anyway, build that. What carries over is behaviour (tests, fixtures, constants),
   not code. File- or API-shaped interfaces make the implementation language a free choice; check whether the
   pressure to stay in one language is real.
5. **Run this hardest on your own plans.** A plan you wrote and defended twice with plausible engineering
   reasons is exactly where you are worst at seeing the deletion.

## Scale the process to the blast radius

6. **Heavy process (separate implementer, two-stage review) is for mistakes that would be silent, permanent,
   or written into someone's data.** One read-through is enough when the worst case is visible and
   reversible; a genuinely small change you just write yourself.
7. **Delegation has a fixed cost floor.** A subagent re-reads context, rebuilds and re-runs suites whatever
   the diff size, so small tasks are where dispatching is the wrong trade.
8. **A process that keeps finding bugs trains you to over-apply it.** Re-ask "what does a mistake here cost?"
   per task, not per phase. A workflow's default review cadence is scoped to its imagined typical task;
   re-derive it for the tasks actually in front of you, and batch reviews of low-risk work.

## Long or expensive jobs

9. **Stream results to disk as they complete:** one line per item, flushed. A killed run keeps everything it
   finished. Never hold a batch in memory and write at the end.
10. **Make it resumable:** on start, skip ids already in the output. Re-running continues, never restarts.
11. **Gate before the expensive pass.** If a cheap filter drops most items, run it first. Estimate wall-clock
    from the binding constraint (usually request rate limits, not tokens) and state the estimate up front.
12. **Progress comes from the file, not the process:** a line count on the output is a truthful progress bar.
13. **Put all of the above in every brief for a long job.** An agent that isn't told writes the in-memory version.
14. **When told to kill, kill first, then explain what was lost.** Irreversibility is not a reason to stall on an
    explicit instruction; the person already weighed it.
15. **Track what the vendor actually bills on,** not the number it prints per call (response bytes, holds,
    seconds, tiered rates). Read the pricing page for every billed dimension, log each from the responses, use
    the vendor's spend cap if it has one, run one consumer per shared wallet, and reconcile the dashboard
    against your own log after the first few dollars.

## Claims and evidence

16. **Tag provenance on anything a decision rests on:** *verified* (read the primary source this session),
    *secondhand* (an agent's summary, a search snippet, one fetch's paraphrase), *inference* (your reasoning
    about something you can't observe). Never state the second or third at the first's confidence.
17. **Re-verify against the primary source before anyone acts externally on a claim:** sends a message, accuses,
    spends, states it to a counterparty. Even if it was stated confidently earlier.
18. **An agent's citation is unverified until spot-checked.** Summaries routinely compress a snippet and link
    the wrong page.
19. **Narrating another party's internal process is garnish, never load-bearing.** Either find evidence or
    design the action so it works whichever way their process runs.
20. **When caught, downgrade and redesign; don't defend.** Concede the claim, restate what the evidence does
    pin, and check which decisions relied on the dead claim.

## Measurement

21. **A number without its baseline is worse than no number.** For any comparison, measure the control: the same
    configuration run against itself sets the ceiling. If that ceiling is low, the task is underspecified,
    and no choice of model or setting will fix it.
22. **Use symmetric metrics.** Precision alone punishes producing more; recall alone rewards it.
23. **A suspiciously good number is a bug signal,** e.g. a zero error rate because the field was never written.
24. **A check only finds what it tests for.** Correctness and completeness are different sweeps: an item with a
    correct citation and no content passes a citation audit. Before trusting a clean result, name the
    property it tested and test the other one.
25. **Sweep a heterogeneous structure by each part's own schema,** not one field across all parts. When a
    grouping key picks one of several values (first author, primary owner), the unpicked values went unchecked.
26. **Never derive a label vocabulary from your own model's output.** It narrows with every pass and the blind
    spot seals itself. Build from sources; extend only where the material demands it, and mark those
    additions as yours.

## Safety

27. **Never run destructive SQL** (TRUNCATE, DROP, unbounded DELETE) against a database you didn't create
    yourself, whatever an env file says it points at. Guarantee safety by not having destructive code in the path.
28. **Tests that touch a shared database insert fixtures with clearly fake ids, record every id, and delete only
    those ids afterwards.** No global setup that wipes and reseeds, no separate "test database" env vars (a
    misconfigured one is how a test wipes production), no bulk seed files. The test must stay correct with
    millions of real rows present.
29. **Confirm scope before anything hard to reverse:** deleting files, force-pushing, resetting, dropping branches,
    writing to shared data, even if a similar action was approved before.
30. **Vet a new tool before running it, not after.** Check install hooks, publish age and release history, and
    provenance; read the installed source for process spawning, eval and telemetry; check every dependency,
    including optional ones (they may fetch native binaries from outside the registry, and package managers
    don't always honour "omit optional"). If it already ran, vet it anyway and say so.
31. **Tear down what you start:** headless browsers, dev servers, watchers, background services. If you leave
    something running, say what and why. When something is slow or hot, check your own leftover processes
    before blaming the user's code.
32. **Batch uniform work:** one set-based statement or bulk call, not a per-item loop, unless each item needs
    different logic.

## Orchestration

33. **Some multi-agent harnesses relay the triggering message verbatim to every agent as authoritative.** Before
    launching an evaluation or anything prompt-sensitive, make sure that message is neutral, then read one
    agent's transcript to confirm what it received. Constrain label-like output fields to an enum.
34. **Trust in a source never replaces verifying the artifact.** A reputable vendor or uploader can still ship
    one bad item; keep the downstream check unconditional.
35. **Generic signature matches produce false positives at scale.** When the decision matters, confirm with a
    structural check rather than acting on the match.
