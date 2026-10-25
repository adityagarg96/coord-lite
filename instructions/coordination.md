# How every session here works

Several OpenCode sessions work one set of requirements together: one **master** coordinates, a handful of
**workers** build. They share two things: the **store** (a SQLite graph behind the `store_*` tools) and the
**peers** tool (messages between live sessions). The store holds the truth; messages only point at it.

## Laws

1. **The store is the only home of a fact.** A requirement's status, owner, decisions, blockers and what was
   tried live in nodes and edges, never only in a chat or a message. If you said it and it matters, write it.
2. **Messages point, they don't carry.** A peer message says what changed and where: "R7 (#42) is BLOCKED on
   the schema decision #51, see its body". The receiver reads the store before acting.
3. **Every write names you.** Pass `actor` = your session's peer name (`master`, `w1` … `w5`; your session
   title). That is how others see who changed what (`changes_since`).
4. **Reversals are recorded, not overwritten.** When a DEFINED requirement or decision changes, use
   `node_supersede`. Dropping needs `why_dropped`. Nothing is deleted.
5. **Progress needs a receipt.** A PR, a passing test, a sent doc: `receipt_add`. Editing a node is not progress.
6. **Lint after a batch of edits.** `plan_lint` on the root you touched; fix or explain what it finds.

## The loop that wraps every task

- **Consult** before you start: `search` for the topic, `plan_render` the branch, and `changes_since` (with the
  `now` from your last call) to see what other sessions did. Trust the store over your memory.
- **Rely** on what it says: work from the node's exit test and its edges, not from a re-derivation.
- **Update** as you go, not at the end: state and status changes, a `lesson` node for an approach that failed
  (what you tried, why it failed), receipts for finished work. Fix any node you find stale on the spot,
  overview nodes first, because they are everyone's starting point.

## Two trees

- **Requirements**: one `plan` root; each requirement is a `requirement` node with `attrs.exit_test` (how we
  know it is done) and `attrs.owner` (the worker). Tasks, decisions and checkpoints hang beneath it; `blocks`
  edges carry the order between requirements.
- **Codebase map**: one `concept` root. Its direct children are the overview (architecture, data flow, major
  subsystems); deeper nodes are details. A concept body references others inline as `#id`, and every concept
  links to at least two others (the lint checks). Requirements point at the concepts they touch
  (`relates_to`). `map_export` renders it as a plain mind-map file for people without the store.

## Peers

- `peers list` shows the live sessions. Name your session (its title) before your first message.
- `send` to notify, `ask` when you are blocked on an answer, `reply` to answer a question waiting on you.
- After any peer message arrives: `changes_since`, then act.
- A session that spawns subagents and wants to stay reachable spawns them with `background: true`.
