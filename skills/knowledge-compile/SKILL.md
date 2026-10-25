---
name: knowledge-compile
description: "Use when asked to compile the journal, or when a KNOWLEDGE COMPILE message arrives: folds uncompiled journal entries (what sessions learned) into the store's concept, decision and lesson nodes, following the upkeep rules."
---

Compile the journal into the graph. The journal is the raw material: each entry is one session's own account of
a stretch of work (Context / Decisions / Proposals / Lessons / Action items), or a raw excerpt of recent turns captured just
before a compaction. The graph is the compiled result. Your job is the compiler's: fold what is worth knowing into
the right nodes, keep the graph small and true, and leave nothing half-done.

**Run this in a background subagent** if more than five entries are pending, so the calling session's context
stays small. The subagent has the same store tools; it reports a summary back.

**Facts, decisions and proposals are different things.** A concept body states only what is true of the code
now; a decision node records a choice that was actually made, by whom. A suggestion, an opinion, a plan or
a "we could" is a **proposal**: it becomes a PROPOSED `decision` with `attrs.owner`, never a sentence in a concept
body and never a CONFIRMED decision. When an entry is ambiguous about which it is, treat it as a proposal.

**The code outranks the journal.** A journal entry is a session's own account of its work: secondhand. Before a
claim about what the code does goes into a concept body, check it against the code at the current commit (open
the file, find the line) and cite it in the body as `path:line@<short sha>`. If you can't check it now, don't touch
the body: add the claim to the node with `node_edit(attrs={"to_verify": [{"claim": ..., "journal_id": ...}]})`
(append to any list already there); `store_lint` lists those until someone verifies or drops them. Never let a
journal claim overwrite a statement that already cites code unless you confirmed the code changed.

**Imported history stays history.** Nodes loaded from an earlier record (they carry `attrs.source` or a
`source_key`, or sit under a root marked `attrs.imported`) are what that record said at the time. Don't edit them
to match the journal; if the journal shows one is no longer true, supersede it with a live node.

1. **Load.** `journal_pending` (20 at a time). Read `knowledge_index` once so you know what exists. For a
   precompact entry (raw turns), first reduce it in your head to the same five sections; most of a raw excerpt is
   routine and should be dropped.

2. **For each piece of knowledge, find its home before writing.** `search` its key words, open candidates with
   `node_get`. Then, in this order of preference:
   - **Update** an existing node with a fact the entry observed and you verified: `node_edit` its body so it states
     what is true now, citing `path:line@sha` and `#id` links. Unverified claims go to `to_verify` (above).
     A single entry usually touches 1–5 existing nodes.
   - **Reverse** one: if the entry shows an existing DEFINED or CONFIRMED node is now wrong, `node_supersede` it,
     citing the entry. If it's unclear which is right, create a PROPOSED `decision` with `attrs.owner` set to the
     owner of the area (or `master`) instead of guessing.
   - **Create** only for something genuinely new: a `concept` (a subsystem, interface, data flow, convention)
     under the right concept parent, linked to at least two nodes; a `decision` (status CONFIRMED when the entry
     says it was decided, with who and why; PROPOSED with an owner for anything under **Proposals:**); a `lesson` (what was tried, why it failed, what to do instead)
     under the concept it is about.
   - **Connect**: when an entry reveals a non-obvious relationship between two existing concepts, add an edge
     (`relates_to`, `informs`, `blocks`, `derived_from`) with a `note` saying what the link is. Make it a new
     concept only if the insight needs more than a sentence.
   - **Action items** that aren't done: a `task` under the requirement or concept it belongs to, with
     `attrs.owner`. Skip ones the store already shows as done.
   - **Skip** what isn't knowledge: progress chatter, things obvious from the code, one-off debugging steps that
     taught nothing.

3. **Leave a trail.** On each node you created or changed, `receipt_add` with source `journal`, payload
   `{journal_id, actor}`, dedupe_key `journal:<journal_id>:<node_id>`.

4. **Mark and check.** `journal_mark_compiled` with the entry ids and the node ids they went into (an empty node
   list for entries with nothing worth keeping). Then `store_lint`, and fix what this compile introduced:
   duplicates, sparse concepts, under-linked concepts, strays. While the relevant nodes are open, look for claims
   that contradict each other and resolve them the same way as step 2's reversals.

5. **Report** in a few lines: entries compiled, nodes updated / superseded / created (ids), decisions recorded,
   anything you parked as PROPOSED for someone to decide, and the backlog left.
