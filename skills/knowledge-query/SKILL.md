---
name: knowledge-query
description: "Use when asked what the team knows, has decided, or has learned about something (the codebase, a past decision, why something is the way it is): answers from the store with #id citations, and can file the answer back as a Q&A node."
---

Answer from the store, not from memory, and make good answers compound.

1. **Find.** Read `knowledge_index` (the catalogue), pick the 3–10 most relevant nodes, and `search` for anything
   the index might name differently. Open each with `node_get`; use `plan_render` when the answer is a whole branch.
   Superseded nodes tell you what used to be true and why it changed; say so when that matters.

2. **Answer** with `#id` citations for every claim. Say plainly what the store does not cover rather than filling
   the gap from general knowledge; if you do add outside knowledge, mark it as such.

3. **File it back** when the answer is reusable (someone will ask again) or the user asks you to: find or create a
   `concept` child titled "Q&A" under the concept root, then a `qa` node beneath it, titled `Q: <the question>`,
   whose body is the answer with its `#id` citations. Add a `relates_to` edge from it to each node you cited. Don't
   file one-off questions. If answering showed a node to be wrong or thin, fix that node too (`node_edit`), since
   that is the real compounding.
