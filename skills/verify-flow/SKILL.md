---
name: verify-flow
description: "Use when asked to verify, second-read or audit flows on the system storyboard, or after trace-flow finishes a flow: a reader who did not trace it checks each step's CLAIM against the code and records confirmed / wrong / unclear on the step."
---

A pointer that resolves only proves the line exists. This skill checks that the sentence on the step is TRUE of
that line and the code around it. You must not be the session that traced the flow (`flow_check` refuses it).

1. **Pick the flows.** A new flow gets every step read. For the standing map, read one step per flow per run,
   choosing the steps nobody has checked yet (no `check` in its step), then the oldest checks, then steps whose
   storyboard ring says the code moved or changed.

2. **Read each step cold.** Open `at` at its sha and enough of the code around it to know what actually happens
   (the caller, the query, the condition). Read the step's `text`, `reads`, `writes` and its pinned invariant or
   decision only after you have your own account. Compare:
   - **confirmed**: the sentence is true, the tables and columns are the ones touched, and the pin bites here.
   - **wrong**: something in it is false: a different table, a condition that is not there, a law that is not
     enforced the way the invariant says. Say in the note what is actually true, with a `path:line@sha`.
   - **unclear**: the code does not settle it (dynamic SQL, a value from config you cannot see, a database
     object not in the repo). Say what would settle it. Never connect to a database to find out.

3. **Record it**: `flow_check(actor, flow_id, step, verdict, note)`, one call per step.

4. **Report**: per flow, steps read and verdicts, every `wrong` with its note, and which invariants turned out to
   be enforced differently than recorded. A `wrong` step goes back to `trace-flow` for that flow; the storyboard
   shows ✗ on it until it is re-traced.
