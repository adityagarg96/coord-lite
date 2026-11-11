---
name: trace-flow
description: "Use when asked to trace a flow, add a scenario to the system storyboard, or map how a request, job or agent action moves through the system: writes one flow (ordered steps across components, each with a verified path:line@sha and the tables it reads and writes) into the store."
---

A **flow** is one scenario traced end to end through the system: what starts it, each step in order, which
component does it, the exact line, and which tables and columns it reads and writes. The storyboard
(`python -m coordlite storyboard --repo <checkout> --out board.html`) plays flows across a fixed canvas of
components and tables, and the quiz asks about them. A flow is only worth having if every step is true, so
trace from code, never from memory or the store's own bodies.

1. **Name the scenario** with the human or the requester: one trigger ("a reservation arrives from an OTA", "a
   nightly reconciliation run", "a user opens the dashboard"). One flow per trigger; a fork in the path is a
   second flow.

2. **Make sure the stage exists.** Components are concept nodes with `attrs.lane` (an integer: top to bottom,
   the human or the caller first, storage last); tables are concept nodes with `attrs.entity` (the table name)
   and `attrs.columns`. `search` before creating; put new ones under a "Runtime" and a "Data" concept beneath
   the concept root. Keep lanes to about eight: they are the system's big parts, not its files.

3. **Trace it in the code at the current commit.** Start at the entry point and follow the call path. For each
   step that changes state, crosses a component boundary, or decides something:
   - `text`: one sentence, in the system's own terms, of what happens and why it matters.
   - `lane`: the component node id.
   - `at`: `path:line@<short sha>` (a range `path:12-18@sha` is fine), a line you actually read.
   - `reads` / `writes`: `table` or `table.column`.
   - `pin`: the id of an invariant or decision that bites at this step, if any. If you find a law that no node
     records yet, create the `invariant` (enforced_by, breaks_if, evidence) and pin it.
   Keep a flow to about 6-12 steps. Skip pure plumbing (argument parsing, logging) unless it decides something.

4. **Store it** as a `concept` under the concept the flow is mostly about, titled as the scenario, with body
   "A flow: <trigger>.", `attrs.trigger` and `attrs.steps` (the list above).

5. **Check it.** Run the storyboard with `--repo`: the header must say "every pointer checked". A broken pointer
   is a step you did not read; fix it before reporting. Report the flow's id, its step count, and any
   invariants you created.
