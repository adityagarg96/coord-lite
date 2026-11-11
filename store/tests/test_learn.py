"""The learner layer: fog-of-war frontier, closed-book questions, FSRS cards, disputes, the profile.
Fresh tmp SQLite per test; nothing here touches a real store."""
import sqlite3

import pytest

from coordlite import learn, views
from coordlite.__main__ import boot_packet
from coordlite.db import connect, get_node

learn.FUZZ = False


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "store.db")
    yield c
    c.close()


def mk(con, title, kind="concept", parent=None, **kw):
    return views.node_create(con, kind, title, kw.pop("body", "a body with enough words to describe it"), parent,
                             actor="master", **kw)["id"]


@pytest.fixture
def system(con):
    root = mk(con, "Codebase map")
    ingest = mk(con, "Ingest", parent=root)
    recon = mk(con, "Reconciliation", parent=root)
    canon = mk(con, "Canonical rows", parent=recon)
    law = mk(con, "One canonical row per booking", kind="invariant", parent=recon,
             attrs={"enforced_by": "convention", "breaks_if": "two sources race", "evidence": ["#1"]})
    views.edge_add(con, ingest, recon, "informs", actor="master")
    return {"root": root, "ingest": ingest, "recon": recon, "canon": canon, "law": law}


def ask(con, nid, level=1):
    return learn.prepare(con, nid, f"What happens to #{nid} when X, and why?", "it does Y because Z", ["#1"], level,
                         actor="prep")["question_id"]


def test_the_first_frontier_is_the_overview_not_the_root(con, system):
    q = learn.queue(con)
    ids = [i["node_id"] for i in q["items"]]
    assert set(ids) == {system["ingest"], system["recon"]}            # depth 1 only: the fog hides the rest
    assert system["root"] not in ids and q["unprepared"] == ids


def test_a_nodes_children_open_once_it_is_understood(con, system):
    learn.record(con, ask(con, system["recon"]), "it reconciles", "correct", 3)
    ids = [i["node_id"] for i in learn.queue(con, limit=8, new=8)["items"]]
    assert system["canon"] in ids and system["law"] in ids
    assert ids.index(system["law"]) < ids.index(system["canon"])       # a law held by convention comes first


def test_a_miss_keeps_the_fog_and_names_what_to_drill(con, system):
    out = learn.record(con, ask(con, system["recon"]), "no idea", "incorrect", 4)
    assert out["rating"] == "Again" and out["hypercorrection"] is True
    assert {d["node_id"] for d in out["drill"]} == {system["canon"], system["law"]}
    ids = [i["node_id"] for i in learn.queue(con, new=8)["items"]]
    assert system["canon"] not in ids                                   # children stay fogged until the parent lands


def test_the_queue_shows_a_prepared_prompt_but_never_its_key(con, system):
    qid = ask(con, system["ingest"])
    item = next(i for i in learn.queue(con)["items"] if i["node_id"] == system["ingest"])
    assert item["question_id"] == qid and item["prompt"].startswith("What happens") and "key" not in item
    assert learn.key(con, qid)["key"] == "it does Y because Z"


def test_a_question_needs_evidence_and_is_answered_once(con, system):
    with pytest.raises(ValueError, match="evidence"):
        learn.prepare(con, system["ingest"], "q?", "k", [], 1)
    qid = ask(con, system["ingest"])
    learn.record(con, qid, "a", "correct", 3)
    with pytest.raises(ValueError, match="already answered"):
        learn.record(con, qid, "a", "correct", 3)


def test_confidence_shapes_the_rating():
    assert learn.rating_for("correct", 1).name == "Hard"                # a lucky guess is not retrieval
    assert learn.rating_for("correct", 3).name == "Good"
    assert learn.rating_for("correct", 4).name == "Easy"
    assert learn.rating_for("partial", 4).name == "Hard"
    assert learn.rating_for("skipped", 3) is None


def test_two_correct_answers_in_a_row_raise_the_level(con, system):
    n = system["ingest"]
    assert learn.record(con, ask(con, n, 1), "a", "correct", 3)["level"] == 1
    out = learn.record(con, ask(con, n, 1), "a", "correct", 3)
    assert out["level"] == 2 and out["next_due"] > views.now_iso()[:10]


def test_a_dispute_changes_no_memory_and_lands_on_the_node(con, system):
    n = system["ingest"]
    out = learn.record(con, ask(con, n), "", "disputed", None, dispute="the premise is wrong: ingest never retries")
    assert out["rating"] is None and out["to_verify"] == 1
    assert con.execute("SELECT COUNT(*) FROM learner_card").fetchone()[0] == 0
    node = get_node(con, n)
    assert node["attrs"]["to_verify"][0]["dispute"].startswith("the premise is wrong")
    assert any(f["check"] == "unverified_claims" and f["node_id"] == n for f in views.store_lint(con))


def test_reviews_are_append_only(con, system):
    learn.record(con, ask(con, system["ingest"]), "a", "correct", 3)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE learner_review SET verdict='incorrect'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM learner_review")


def test_the_profile_and_boot_packet_tell_agents_what_the_human_knows(con, system):
    assert learn.profile_line(con) is None and "What the human understands" not in boot_packet(con)
    learn.record(con, ask(con, system["recon"]), "x", "incorrect", 4)
    learn.record(con, ask(con, system["ingest"]), "y", "correct", 3)
    p = learn.profile(con, node_id=system["recon"])
    assert p["shaky"] == 2 and p["weakest"][0]["node_id"] == system["recon"]
    assert p["sure_and_wrong"][0]["node_id"] == system["recon"] and p["node"]["recent"][0]["verdict"] == "incorrect"
    assert "What the human understands" in boot_packet(con)


def test_learner_data_stays_out_of_the_shared_views(con, system):
    learn.record(con, ask(con, system["ingest"]), "my private answer", "correct", 3)
    assert "my private answer" not in views.knowledge_index(con)
    assert "my private answer" not in views.map_export(con, system["root"])
    assert not views.changes_since(con)["changes"][-1]["what"].startswith("learn")


def test_the_map_shows_state_and_never_embeds_text_as_html(con, system):
    from coordlite.learnmap import map_data, map_html
    views.node_edit(con, system["ingest"], {"body": "evil </script><script>alert(1)</script>"}, actor="w1")
    learn.record(con, ask(con, system["recon"]), "x", "correct", 3)
    d = map_data(con)
    state = {n["id"]: n["state"] for n in d["nodes"]}
    assert state[system["root"]] == "container" and state[system["recon"]] == "shaky"
    assert state[system["canon"]] == "frontier" and state[system["ingest"]] == "frontier"
    html = map_html(con)
    assert "</script><script>alert" not in html and "<\\/script>" in html


def test_the_storyboard_plays_flows_and_flags_broken_pointers(con, system, tmp_path):
    from coordlite.storyboard import board_data, board_html
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
    lane = mk(con, "Server", parent=system["root"], attrs={"lane": 0})
    mk(con, "reservation", parent=system["root"], attrs={"entity": "reservation", "columns": ["id", "hotel"]})
    mk(con, "A booking arrives", parent=system["recon"], body="</script><script>alert(1)</script>",
       attrs={"trigger": "an OTA sends a booking", "steps": [
           {"lane": lane, "text": "parse", "at": "a.py:2", "writes": ["reservation.hotel"], "pin": system["law"]},
           {"lane": lane, "text": "ghost", "at": "a.py:99"}]})
    d = board_data(con, repo=str(tmp_path))
    f = d["flows"][0]
    assert [s["ok"] for s in f["steps"]] == [True, False] and d["broken"] == 1
    assert f["steps"][0]["pin"]["kind"] == "invariant" and f["trigger"] == "an OTA sends a booking"
    assert d["lanes"][0]["title"] == "Server" and d["entities"][0]["columns"] == ["id", "hotel"]
    assert "</script><script>alert" not in board_html(con)
