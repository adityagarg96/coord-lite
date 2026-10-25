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

## Upkeep: keep the graph small and true

A graph rots one harmless-looking node at a time. These rules are what stop it.

1. **Search before you create.** Run `search` on the title's key words first. If a node already covers it,
   edit that node (`node_edit`) or add an edge; a near-duplicate costs every later reader.
2. **Edit beats add.** New facts about a thing go into its body. Add a node only for a genuinely new thing:
   a requirement, a decision, a subsystem, a failed approach worth remembering.
3. **Every node has a home.** Give it the parent it belongs under; the only roots are the plan root and the
   concept root (plus a project or goal root if the user asks for one). `store_lint` flags strays.
4. **Undecided is temporary.** A PROPOSED or DRAFT node needs `attrs.owner` (who decides) and should be
   decided within two weeks: ratify it (status DEFINED or CONFIRMED), supersede it, or let the sweep park it.
   Don't create a PROPOSED node you aren't prepared to chase.
5. **Reversals supersede; dead ends drop with a reason.** Never leave two live nodes that disagree.
6. **Close what you finish.** State `done` with a receipt when the exit test passes; done nodes stay as
   history and drop out of everyone's working view.
7. **Bodies are short and current.** A body states what is true now, in a few sentences, with `#id` links.
   History lives in the node's edit history and in superseded nodes, not in an ever-growing body.

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
