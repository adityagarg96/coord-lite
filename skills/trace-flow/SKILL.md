---
name: trace-flow
description: "Use when asked to trace a flow, add a scenario to the system storyboard, re-trace a flow a change touched, or map how a request, job or agent action moves through the system: writes one flow (ordered steps across components, each with a verified path:line@sha and the tables it reads and writes) as a JSON file and loads it into the store."
---

A **flow** is one scenario traced end to end through the system: what starts it, each step in order, which
component does it, the exact line, and which tables and columns it reads and writes. The storyboard
(`python -m coordlite storyboard --repo <checkout> --out board.html`) plays flows across a fixed canvas of
components and tables, and the quiz asks about them. A flow is only worth having if every step is true, so
trace from code, never from memory or the store's own bodies. `examples/coord-lite-map/` in this repo is a
complete worked example.

1. **Name the scenario**: one trigger ("a reservation arrives from an OTA", "a nightly reconciliation run", "a user
   opens the dashboard"). One flow per trigger; a fork in the path is a second flow.

2. **Know the stage.** Lanes and tables are already loaded by the `map-system` skill (`query` for nodes whose
   `source_key` starts with `map:lane:` / `map:table:`; the key after the prefix is what you reference). If the
   flow needs a lane or table that is missing, add it to the map's `lanes.json` / `tables.json` and load it, or
   ask; never more than nine lanes.

3. **Trace it in the code at the current commit.** Start at the entry point and follow the call path. For each
   step that changes state, crosses a component boundary, or decides something:
   - `text`: one sentence, in the system's own terms, of what happens and why it matters.
   - `lane`: the lane's key.
   - `at`: `path:line@<short sha>` (a range `path:12-18@sha` is fine), a line you actually read. When the system
     spans several checkouts under one parent directory, name the checkout first: `<checkout>/path:line@sha`, with
     that checkout's sha, and pass the parent as `--repo`.
   - `reads` / `writes`: `table` or `table.column`, named as the database names them.
   - `pin`: the key of an invariant or decision that bites at this step. A law no one has recorded yet goes into
     `invariants.json` first (key, title, body, enforced_by db|code|convention|none, breaks_if, evidence, guards),
     with an honest `enforced_by`: `none` is common and it is the most useful thing a flow can surface.
   Keep a flow to about 6-12 steps. Skip pure plumbing (argument parsing, logging) unless it decides something.

4. **Write it** to `flows/<key>.json` in the map directory:
   `{key, title, domain, trigger, traced_by: <your actor name>, steps: [...]}`. Reuse a handful of domains (the
   page groups flows by them) rather than one per flow. Then `python -m coordlite map-load <map dir>/flows/<key>.json`
   (a file loads in one transaction; a bad lane, pin or pointer loads nothing and says which step). Loading is by
   key, so re-tracing a flow updates it in place.

5. **Check it.** Render the storyboard with `--repo`: the header must say every pointer checked, 0 broken, and no
   step of yours "changed since traced". Report the flow's key, its step count, and any invariants you added.
   Then a different session runs `verify-flow` on it: a check by the author is not a check.
