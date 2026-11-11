---
name: quiz
description: "Use when the human asks for their quiz, daily practice, or to be tested on the system: runs a 15-20 minute closed-book session over the store's knowledge (data model, architecture, invariants, decisions), grades each answer against a prepared key, and schedules what comes back."
---

Run today's quiz. The goal is a human who understands this system well enough to make decisions in a meeting
without asking an agent: what the data model is, how the parts fit, which laws hold and why, what was decided and
what it gave up. Spaced repetition (FSRS in the store) decides what comes back when; your job is to ask well and
grade fairly.

**Closed book.** Do not call `node_get`, `plan_render`, `search` or read code about a quiz node before the human has
answered it. Tool output is on their screen. You see a question's prompt from `learn_queue` and its key only via
`learn_key`, after they answer.

1. **Plan the session.** `learn_queue(limit=8, new=3)`. If `unprepared` is not empty, start a **background**
   subagent on the `quiz-prepare` skill for those node ids (with their levels), and begin with the prepared
   items. Pick up the rest when it reports. If nothing is due and the frontier is empty, say so and stop: the
   store needs more knowledge first (see knowledge-compile and ingest-pr).

2. **Ask in rounds of up to three**, numbered, like this:

   ```
   Q1 · (level 2 · invariant) <prompt>
   Q2 · (level 1 · concept) <prompt>
   ```

   Then ask for an answer to each, plus a **confidence** from 1 (guess) to 4 (certain). Short typed answers are
   fine: a prediction and the reason. They can answer `skip`, or `dispute Qn: <why the question is wrong>`.
   If the harness has a structured question tool (a form or multiple-choice prompt), use it: per quiz question,
   one free-text answer (with Skip and Dispute as choices) and one confidence choice (1 guess, 2 unsure,
   3 fairly sure, 4 certain). Two quiz questions per form keeps it readable. Never put the answer among the choices.

3. **Grade each answer.** `learn_key(question_id)`, then judge it against the key like a fair senior engineer in
   conversation, not an exam marker:
   - **correct**: the prediction and its cause are there, in any words.
   - **partial**: the right prediction without the cause, or the cause with a wrong detail that changes nothing.
   - **incorrect**: a wrong prediction, or a cause that would lead to wrong predictions elsewhere.
   An answer that explains real behaviour the key missed is correct; if the key looks wrong, say so and record a
   dispute yourself. Then give **feedback** in two to four sentences: what was right, the missing or wrong link,
   and the evidence (`path:line@sha`, `#id`, or the decision's source). Record it with
   `learn_record(actor, question_id, answer, verdict, confidence, feedback, dispute)`.

4. **Follow the result.**
   - `hypercorrection` (certain and wrong): name it plainly. It is the most useful miss of the day.
   - `drill` after a miss: queue those children for the background `quiz-prepare` subagent, so the gap is
     localised next round or tomorrow. Do not re-ask the same node today; FSRS brings it back.
   - A dispute changes nothing in the human's record and puts the claim on the node for upkeep; the master's lint
     will show it.

5. **Stop at about 15-20 minutes or eight questions**, whichever comes first, or when the human says stop. End with
   three lines: today's result (correct / partial / incorrect), what comes back tomorrow, and `learner_profile`'s
   solid / shaky / never-asked counts.

Never answer a question for the human before they have tried, and never lecture past the feedback unless they ask.
If they want to learn a topic properly rather than be tested, that is a different session: explain it with
citations, then offer to prepare questions on it for tomorrow.
