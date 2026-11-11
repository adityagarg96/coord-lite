---
name: quiz-prepare
description: "Use when asked to prepare quiz questions for the human (learn_queue's `unprepared` list, or drill nodes after a miss): writes one closed-book question per knowledge node with a private answer key grounded in evidence. Run in a background subagent, never in the quiz session itself."
---

Write quiz questions that build a staff engineer's understanding of this system: its **data model**, **architecture**,
**invariants** and **decisions**. Never its trivia. You are writing for one human; `learner_profile(node_id)` tells
you what they already know.

**Why a background subagent.** The quiz is closed book. Everything you read here (node bodies, code, keys) would be
on the human's screen if the quiz session read it. Report back only the node ids and question ids you prepared,
never a prompt's key.

For each node id you were given (and its level, from `learn_queue`; level 1 if none):

1. **Read it.** `node_get`, then `plan_render(node_id, depth=1)` for its children, edges and parent. Look at the
   questions already asked about it, so this one is new:
   `query("SELECT level, prompt FROM learner_question WHERE node_id = <id> ORDER BY id DESC LIMIT 5")`.

2. **Ground it.** The node is the store's account, which is secondhand. The answer key must rest on something
   checked:
   - A claim about the code: open the file at the current commit and cite `path:line@<short sha>`.
   - An invariant: its `attrs.evidence`, re-checked at HEAD when it cites code.
   - A decision: what was decided, by whom and why, citing `attrs.decided_in` (a session and turn, a PR, a message,
     a meeting). Say it is attested, not verified.
   - Anything you cannot ground: do not ask about it. Add it to the node's `attrs.to_verify` (append to any list
     already there) and move on. A question on a wrong premise teaches the wrong thing.
   Skip a node that already carries `to_verify` claims, and say so in your report.

3. **Write one question** at the node's level, about consequences, not names:
   - **Level 1, what and where.** What this part is responsible for, what feeds it, what reads from it, which entity
     is the source of truth.
   - **Level 2, how and why.** How data or control moves through it and why it is shaped that way: cardinality,
     lifecycle and state, ordering, who writes and who reads.
   - **Level 3, what-if and trade-offs.** A concrete scenario: a crash mid-run, a duplicate input, two writers
     racing, a schema change, a requirement that cuts across it. What happens, what stops it (or nothing), and
     what would you change.
   By kind:
   - **invariant**: build the scenario from `attrs.breaks_if`. Ask whether it holds there and what enforces it.
     `convention` and `none` laws are the important ones: ask what a newcomer could do to break them.
   - **decision**: ask what problem it solved and what was given up, or what would have to change for the rejected
     option to be right. Never "who decided" alone.
   - **concept**: its role and its boundary: what it owns, what it deliberately does not.
   Rules (from Sphinx's reviewer, which learned them the hard way):
   - Ask for a **prediction and its cause**. Never a file name, function name, line number or literal from the code.
   - **Neutral wording.** Do not give the outcome away: ask "what happens when the job reruns, and why?", not
     "why is the rerun safe?". No presupposition in the second half of the question either: "which of A or B
     survives, and what makes it the one you can rely on?" has already said one does. Ask "what does each of A and
     B carry across, and why?" instead. Before storing, reread the prompt as the human: if the wording points at
     the answer, rewrite it.
   - State the conditions a defensible answer needs. One question, one idea: exactly two asks, **what happens**
     and **why**. "What does X consist of, which component builds it, from what, and what carries over?" is four
     questions; pick the one that matters and leave the rest for another day.
   - A new scenario every time. Rephrasing an earlier prompt with new labels is not a new question.

4. **The key** is the minimum a correct answer must establish: one prediction and one causal reason, in a few
   sentences. Keep extra detail out of it, or the grader will demand it. Evidence is the list from step 2.

5. **Store it.** `learn_prepare(actor, node_id, prompt, key, evidence, level)`.

6. **Report** one line per node: `#id -> question <qid> (level N)`, plus the nodes skipped and why.
