"""What the human understands of what the store knows: a spaced-repetition layer over the knowledge graph.

Parallel to the journal: per-person time series kept OUT of the graph (no nodes, no change-log noise, nothing in the
knowledge index or map exports). Agents see it only through the learner_* tools, so they can calibrate to it.

  card      one FSRS memory per knowledge node (concept / invariant / decision) the human has been asked about,
            plus the question level reached there: 1 what and where, 2 how and why, 3 what-if and trade-offs
  question  prepared ahead, with its private answer key and evidence, by a background subagent, so the quiz
            session shows the human the prompt and nothing else until they have answered (closed book)
  review    every answer, append-only

Scheduling is py-fsrs (open-spaced-repetition, MIT), with the minute-level learning steps turned off: this is a
once-a-day practice, so every card goes straight to day-level intervals. The unit scheduled is the CONCEPT, not a
question: each review asks a fresh question about it, so the human cannot pass by remembering a card's wording.

Fog of war: a node nobody has asked about is fog. Children of a root are the first frontier; a node's children join
the frontier once the node itself has been answered correct or partly correct. Shallow first (foundations), the
least-guarded invariants first, then the most connected nodes.
"""
import json
from datetime import datetime, timezone

from fsrs import Card, Rating, Scheduler

from .db import get_node, now_iso, parse_iso, tx
from .views import imported_ids, node_edit

QUIZ_KINDS = ("concept", "invariant", "decision")
VERDICTS = ("correct", "partial", "incorrect", "skipped", "disputed")
FUZZ = True                                          # tests turn this off for exact intervals

_FRAGILE = {e: k for k, e in enumerate(("none", "convention", "code", "db"))}


def scheduler() -> Scheduler:
    return Scheduler(desired_retention=0.9, learning_steps=(), relearning_steps=(), enable_fuzzing=FUZZ)


def rating_for(verdict: str, confidence: int | None) -> Rating | None:
    """The grader's verdict plus the human's confidence (1 guess, 2 unsure, 3 fairly sure, 4 certain, given BEFORE
    seeing the key) become an FSRS rating. A correct guess is Hard, not Good: it was not retrieved, it was lucky."""
    if verdict == "incorrect":
        return Rating.Again
    if verdict == "partial":
        return Rating.Hard
    if verdict == "correct":
        c = confidence or 3
        return Rating.Hard if c <= 2 else Rating.Easy if c == 4 else Rating.Good
    return None                                      # skipped / disputed: no evidence about memory either way


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _card_row(con, node_id: int):
    return con.execute("SELECT * FROM learner_card WHERE node_id=?", (node_id,)).fetchone()


def _retrievability(row, sched: Scheduler, now: datetime) -> float:
    return round(sched.get_card_retrievability(Card.from_dict(json.loads(row["card"])), now), 3)


def _candidates(con, include_imported: bool) -> dict[int, dict]:
    rows = {r["id"]: dict(r) for r in con.execute(
        "SELECT id, kind, title, parent_id, attrs FROM node WHERE state NOT IN ('done','dropped','parked') "
        "AND json_extract(attrs,'$.superseded_by') IS NULL")}
    if not include_imported:
        for i in imported_ids(con):
            rows.pop(i, None)
    return rows


def queue(con, limit: int = 8, new: int = 3, include_imported: bool = False) -> dict:
    """Today's session: due cards first (least retrievable first), then up to `new` nodes from the frontier of the
    fog. Each item says whether a prepared question is waiting for it (prepare the rest before the session)."""
    sched, now = scheduler(), _now()
    rows = _candidates(con, include_imported)
    cards = {r["node_id"]: r for r in con.execute("SELECT * FROM learner_card")}
    edges = {}
    for e in con.execute("SELECT src, dst FROM edge"):
        edges[e["src"]] = edges.get(e["src"], 0) + 1
        edges[e["dst"]] = edges.get(e["dst"], 0) + 1
    kids = {}
    for i, n in rows.items():
        kids[n["parent_id"]] = kids.get(n["parent_id"], 0) + 1

    def depth(i):
        d, seen = 0, set()
        while rows.get(i) and rows[i]["parent_id"] is not None and i not in seen:
            seen.add(i); i = rows[i]["parent_id"]; d += 1
        return d

    due = sorted(((_retrievability(c, sched, now), nid) for nid, c in cards.items()
                  if nid in rows and parse_iso(c["due"]) <= now), key=lambda x: x)
    frontier = []
    for i, n in rows.items():
        if n["kind"] not in QUIZ_KINDS or i in cards:
            continue
        p = n["parent_id"]
        if p is None:
            continue                                 # a root is a container; its children are the first frontier
        par = rows.get(p)                            # a container (root, plan, requirement) is never quizzed: open
        parent_open = par is not None and (par["parent_id"] is None or par["kind"] not in QUIZ_KINDS
                                           or (cards.get(p) and cards[p]["last_verdict"] in ("correct", "partial")))
        if parent_open:
            a = json.loads(n["attrs"] or "{}")
            frontier.append(((depth(i), _FRAGILE.get(a.get("enforced_by"), 9) if n["kind"] == "invariant" else 9,
                              -(edges.get(i, 0) + kids.get(i, 0)), i), i))
    frontier.sort()
    picked_due = [nid for _, nid in due][: max(limit - new, 0)]
    picked_new = [nid for _, nid in frontier][:new]
    spare = limit - len(picked_due) - len(picked_new)
    if spare > 0:                                    # a quiet day: top up from whichever list still has items
        picked_due += [nid for _, nid in due][len(picked_due):len(picked_due) + spare]
        spare = limit - len(picked_due) - len(picked_new)
        picked_new += [nid for _, nid in frontier][len(picked_new):len(picked_new) + spare]

    def item(nid, why):
        n, c = rows[nid], cards.get(nid)
        q = con.execute("SELECT id, level, prompt FROM learner_question WHERE node_id=? AND answered_at IS NULL "
                        "ORDER BY id DESC LIMIT 1", (nid,)).fetchone()
        return {"node_id": nid, "kind": n["kind"], "title": n["title"], "why": why,
                "level": c["level"] if c else 1,
                "retrievability": _retrievability(c, sched, now) if c else None,
                "question_id": q["id"] if q else None, "prompt": q["prompt"] if q else None}
    items = [item(n, "due") for n in picked_due] + [item(n, "frontier") for n in picked_new]
    return {"items": items, "due_total": len(due), "frontier_total": len(frontier),
            "unprepared": [i["node_id"] for i in items if i["question_id"] is None]}


def prepare(con, node_id: int, prompt: str, key: str, evidence: list, level: int = 1, actor: str = "unknown") -> dict:
    """Store a question with its PRIVATE answer key. The key is the minimum a correct answer must establish (the
    prediction and its cause); evidence is the `path:line@sha`, `#id` or quoted source it rests on."""
    if not (prompt or "").strip() or not (key or "").strip():
        raise ValueError("prompt and key are required")
    if not evidence:
        raise ValueError("evidence is required: a question nobody can check is a rumour with a question mark")
    if level not in (1, 2, 3):
        raise ValueError("level is 1 (what/where), 2 (how/why) or 3 (what-if/trade-offs)")
    n = get_node(con, node_id)
    if n is None:
        raise KeyError(node_id)
    qid = con.execute("INSERT INTO learner_question (node_id, level, prompt, key, evidence, prepared_by, created_at) "
                      "VALUES (?,?,?,?,?,?,?)", (node_id, level, prompt.strip(), key.strip(),
                                                  json.dumps(evidence, ensure_ascii=False), actor, now_iso())).lastrowid
    return {"question_id": qid, "node_id": node_id, "level": level}


def key(con, question_id: int) -> dict:
    """The answer key and evidence, for grading. Call it only AFTER the human has answered."""
    q = con.execute("SELECT * FROM learner_question WHERE id=?", (question_id,)).fetchone()
    if q is None:
        raise KeyError(question_id)
    return {"question_id": q["id"], "node_id": q["node_id"], "level": q["level"], "prompt": q["prompt"],
            "key": q["key"], "evidence": json.loads(q["evidence"])}


def record(con, question_id: int, answer: str, verdict: str, confidence: int | None = None, feedback: str = "",
           dispute: str | None = None, actor: str = "unknown") -> dict:
    """Grade one answer: append the review, move the node's FSRS card, and say what to drill next.
    A disputed question changes no memory; the dispute lands on the node as an unverified claim for upkeep."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    if confidence is not None and confidence not in (1, 2, 3, 4):
        raise ValueError("confidence is 1 (guess) to 4 (certain)")
    if verdict == "disputed" and not (dispute or "").strip():
        raise ValueError("a dispute needs its reason")
    sched, now = scheduler(), _now()
    with tx(con):
        q = con.execute("SELECT * FROM learner_question WHERE id=?", (question_id,)).fetchone()
        if q is None:
            raise KeyError(question_id)
        if q["answered_at"]:
            raise ValueError(f"question {question_id} was already answered; prepare a fresh one")
        nid, level = q["node_id"], q["level"]
        rating = rating_for(verdict, confidence)
        row = _card_row(con, nid)
        out = {"node_id": nid, "verdict": verdict, "rating": rating.name if rating else None}
        if rating is not None:
            card = Card.from_dict(json.loads(row["card"])) if row else Card()
            card, _ = sched.review_card(card, rating, now)
            prev = con.execute("SELECT verdict FROM learner_review WHERE node_id=? AND level=? AND verdict IN "
                               "('correct','partial','incorrect') ORDER BY id DESC LIMIT 1", (nid, level)).fetchone()
            new_level = min(level + 1, 3) if verdict == "correct" and prev and prev["verdict"] == "correct" else level
            if row:
                con.execute("UPDATE learner_card SET level=?, card=?, due=?, reviews=reviews+1, lapses=lapses+?, "
                            "last_verdict=?, updated_at=? WHERE node_id=?",
                            (max(new_level, row["level"]), json.dumps(card.to_dict()), card.due.isoformat(),
                             int(verdict == "incorrect"), verdict, now_iso(), nid))
            else:
                con.execute("INSERT INTO learner_card (node_id, level, card, due, reviews, lapses, last_verdict, updated_at) "
                            "VALUES (?,?,?,?,1,?,?,?)", (nid, new_level, json.dumps(card.to_dict()), card.due.isoformat(),
                                                         int(verdict == "incorrect"), verdict, now_iso()))
            out.update(level=max(new_level, row["level"] if row else 1), next_due=card.due.isoformat()[:10],
                       stability_days=round(card.stability or 0, 1))
        con.execute("INSERT INTO learner_review (at, node_id, question_id, level, verdict, confidence, rating, answer, "
                    "feedback, actor) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (now_iso(), nid, question_id, level, verdict, confidence, int(rating) if rating else None,
                     (answer or "")[:4000], (feedback or "")[:4000], actor))
        con.execute("UPDATE learner_question SET answered_at=? WHERE id=?", (now_iso(), question_id))
        if verdict == "disputed":
            n = get_node(con, nid)
            claims = list(n["attrs"].get("to_verify") or []) + [
                {"claim": q["prompt"][:300], "dispute": dispute.strip()[:500], "from": f"quiz:{question_id}"}]
            node_edit(con, nid, {}, actor=actor, attrs={"to_verify": claims})
            out["to_verify"] = len(claims)
        if verdict in ("incorrect", "partial"):
            out["drill"] = [dict(r) for r in con.execute(
                "SELECT n.id AS node_id, n.kind, n.title FROM node n LEFT JOIN learner_card c ON c.node_id = n.id "
                "WHERE n.parent_id=? AND n.kind IN ('concept','invariant','decision') AND n.state NOT IN ('done','dropped','parked') "
                "AND json_extract(n.attrs,'$.superseded_by') IS NULL AND (c.node_id IS NULL OR c.last_verdict != 'correct') "
                "ORDER BY n.id LIMIT 3", (nid,))]
        if verdict == "incorrect" and confidence == 4:
            out["hypercorrection"] = True            # sure and wrong: the most valuable miss, say so
    return out


def profile(con, node_id: int | None = None, limit: int = 8) -> dict:
    """What the human understands, for an agent to calibrate to. Use it before explaining part of the system, or
    to pick what to ask them to predict. Never use it to hold information back."""
    sched, now = scheduler(), _now()
    cards = [dict(r) for r in con.execute(
        "SELECT c.*, n.title, n.kind FROM learner_card c JOIN node n ON n.id = c.node_id")]
    for c in cards:
        c["retrievability"] = _retrievability(c, sched, now)
    solid = [c for c in cards if c["retrievability"] >= 0.9 and c["reviews"] >= 2 and c["last_verdict"] == "correct"]
    shaky = [c for c in cards if c not in solid]
    fog = con.execute(f"SELECT COUNT(*) FROM node n WHERE n.kind IN ({','.join('?' * len(QUIZ_KINDS))}) "
                      "AND n.parent_id IS NOT NULL AND n.state NOT IN ('done','dropped','parked') "
                      "AND n.id NOT IN (SELECT node_id FROM learner_card)", QUIZ_KINDS).fetchone()[0]
    brief = lambda c: {"node_id": c["node_id"], "kind": c["kind"], "title": c["title"], "level": c["level"],  # noqa: E731
                       "retrievability": c["retrievability"], "lapses": c["lapses"], "last": c["last_verdict"]}
    out = {"solid": len(solid), "shaky": len(shaky), "fog": fog,
           "weakest": [brief(c) for c in sorted(shaky, key=lambda c: (-c["lapses"], c["retrievability"]))[:limit]],
           "strongest": [brief(c) for c in sorted(solid, key=lambda c: (-c["level"], -c["retrievability"]))[:limit]],
           "sure_and_wrong": [dict(r) for r in con.execute(
               "SELECT r.node_id, n.title, r.at FROM learner_review r JOIN node n ON n.id = r.node_id "
               "WHERE r.verdict='incorrect' AND r.confidence=4 ORDER BY r.id DESC LIMIT ?", (limit,))]}
    if node_id is not None:
        c = next((x for x in cards if x["node_id"] == node_id), None)
        out["node"] = brief(c) if c else {"node_id": node_id, "state": "fog: never asked"}
        out["node"]["recent"] = [dict(r) for r in con.execute(
            "SELECT at, level, verdict, confidence FROM learner_review WHERE node_id=? ORDER BY id DESC LIMIT 5", (node_id,))]
    return out


def profile_line(con) -> str | None:
    """One paragraph for the master's boot packet; None until the human has been quizzed."""
    p = profile(con, limit=5)
    if not (p["solid"] or p["shaky"]):
        return None
    weak = "; ".join(f"#{w['node_id']} {w['title'][:50]}" for w in p["weakest"])
    return (f"{p['solid']} solid, {p['shaky']} shaky, {p['fog']} never asked." + (f" Weakest: {weak}." if weak else "")
            + " Call learner_profile(node_id) before explaining part of the system, and calibrate to it.")
