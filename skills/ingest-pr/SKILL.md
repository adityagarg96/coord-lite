---
name: ingest-pr
description: "Use when asked to ingest, absorb or learn from a pull request (a teammate's PR number or URL): updates the store's codebase map, decisions and requirements from what the PR changed."
---

Ingest a pull request (`<pr>`: a number or URL) into the store. The goal is to keep our knowledge current with what the team
shipped, not to review the code. Follow the store's upkeep rules throughout: search before you create, edit
beats add, supersede rather than contradict.

1. **Fetch.** With `gh` (it works against GitHub Enterprise too; pass `-R owner/repo` if the PR is in another
   repo): `gh pr view <pr> --json number,title,body,author,state,mergedAt,headRefOid,baseRefName,url,files,commits,reviews,comments`.
   Note the head SHA. If the PR is not merged, say so and ask whether to ingest it as PROPOSED or stop.

2. **Skip what is already in.** Query `SELECT dedupe_key FROM receipt WHERE dedupe_key LIKE 'pr:<owner/repo>#<number>@%'`.
   If this head SHA is already recorded, stop and say so. If an older SHA is, ingest only the commits since it
   (`gh api repos/<owner/repo>/compare/<old>...<new>`).

3. **Read at the right depth.** List the changed files with their additions and deletions and group them by
   area (directory or module). Read the description, the review threads (decisions often live there) and the
   diff of the groups that change behaviour: new modules, interfaces, schemas, configs, data flow. Skim tests
   and generated files only for what they reveal. For a large PR (more than ~30 files or ~1,500 changed lines), give
   each big group to a **background** subagent that returns, without writing to the store: what changed, why
   (quoting the PR text), the files involved, and what it replaces.

4. **Map it onto the graph.** For each area, `search` the store and `plan_render` the concept branch it belongs to.
   - A concept whose behaviour changed: `node_edit` its body so it states what is true now, with file paths.
   - A documented design that the PR reverses: `node_supersede`, citing the PR in the new body.
   - A genuinely new subsystem, interface or data flow: one new `concept` under the right parent, linked to at
     least two existing nodes with `#id` references or edges.
   - A decision made in the PR or its review: a `decision` node under the concept it governs, status CONFIRMED
     if merged, with who decided and the PR link in its body.
   - A gotcha or failed approach the review surfaced: a `lesson` node under the concept.
   - A requirement this PR advances: add the receipt (step 5) and tell its owner with `peers send`. Mark it
     `done` only if its exit test now passes, and say how you checked.
   Leave out what doesn't change our understanding: renames, formatting, dependency bumps, test-only churn.

5. **Record evidence.** On every node you created or changed, `receipt_add` with source `pr`, a payload of
   `{url, number, author, head_sha, merged_at}`, and `dedupe_key` `pr:<owner/repo>#<number>@<head_sha>`.

6. **Check and report.** `plan_lint` the concept root (and the plan root if requirements moved), fix what you
   introduced, then reply with: nodes updated (ids), superseded (old → new), created (ids), decisions recorded,
   requirements touched, and what you deliberately skipped. If the master should know, `peers send` it the same
   summary.
