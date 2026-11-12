---
name: map-system
description: "Use when asked to map a system, build or bootstrap its storyboard, or 'make the map' for a codebase: produces the stage (lanes and tables), picks the flows worth knowing, traces and second-reads them, and renders the storyboard. Run once per project, then keep it current."
---

The system map is the storyboard's data: **lanes** (the system's big moving parts), **tables** (what it keeps),
**invariants** and **decisions**, and **flows** (scenarios traced across them). It lives in one directory of JSON
files, loaded into the store with `python -m coordlite map-load <dir>`, idempotent by key. A complete small example
is `examples/coord-lite-map/`; read it before starting. Keep the map directory where the team can find it (for a
monorepo, `docs/system-map/` is a good home), never in a temp directory.

**This is a long job.** Every flow is written to its own file and loaded the moment it is traced, so a stopped run
keeps everything finished; a rerun skips flows whose file already exists. Say up front how many flows you will
trace and roughly how long it will take.

1. **Tables, without a model.** Find where the schema is declared (migrations, an ORM's models, DDL, a schema
   package). Write a small deterministic extractor script that turns it into `tables.json`
   (`[{name, columns, layer?, body?}]`, names as the database names them, schema-qualified when there are several
   schemas). Keep the script in the map directory so the table list can be regenerated. Set `layer` (for example
   raw, canonical, auth, reporting) when the database's own schemas do not group tables by meaning. Note any
   table the code uses that the schema source does not declare: that is a finding. Read code only; never connect
   to a database.

2. **Lanes, approved by the human.** Propose at most nine lanes in `lanes.json` (`[{key, title, body, order}]`):
   the human or caller first, outside systems near the edge, storage last. A lane is a role a reader can hold in
   their head (scraping, the API, the batch jobs), not a file; when one process plays two roles, say so in both
   bodies. Ask the human to approve, rename or merge before tracing; lanes are the one choice that is theirs.

3. **Choose the flows from evidence.** List candidate scenarios from the entry points (routes, scheduled jobs, CLIs,
   queue consumers, webhooks) and rank them by what the team actually talks about: the store's journal and decision
   nodes (`search`), plans and design docs, the busiest tables. Group them into five or six domains. Propose 8-12
   with a one-line reason each and let the human cut or add.

4. **Trace them.** One background subagent per flow, each started with a fresh context, on the `trace-flow` skill,
   writing `flows/<key>.json` and loading it. Invariants they find go into `invariants.json` (load it before the
   flows that pin them); decisions only with real provenance (`decided_by`, `decided_at`, `decided_in`) into
   `decisions.json`.

5. **Second-read them.** A different session runs `verify-flow` on every new flow. Fix every `wrong` by
   re-tracing.

6. **Render and hand over.** `python -m coordlite storyboard --repo <checkout> --out <map dir>/board.html`. The header
   must say every pointer checked, 0 broken, nothing changed since traced, and show the second-read count. Report:
   lanes, flows by domain, invariants by `enforced_by` (how many are guarded by nothing), what the second reader
   found wrong, and tables the schema source does not declare.

**Keeping it current.** The storyboard flags steps whose code moved or changed since they were traced; `ingest-pr`
calls `flows_touching` with a PR's files and re-traces those steps; `verify-flow` second-reads one step per flow on
each run. New flows come from need: a quiz miss or a meeting question nobody could trace is the next flow.
