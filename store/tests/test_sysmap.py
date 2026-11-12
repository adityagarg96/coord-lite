"""The system map as data: idempotent loading by key, all-or-nothing files, flows a change touches, the second
reader rule, and drift (does a pinned step still describe the code at HEAD?). Fresh tmp SQLite and a throwaway git
repo under pytest's tmp_path; nothing here touches a real store or checkout."""
import subprocess

import pytest

from coordlite import storyboard, sysmap, views
from coordlite.db import connect


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "store.db")
    yield c
    c.close()


MAP = {
    "lanes": [{"key": "api", "title": "API", "body": "serves requests", "order": 0},
              {"key": "db", "title": "Database", "body": "keeps rows", "order": 1}],
    "tables": [{"name": "core.booking", "columns": ["id", "hotel"], "layer": "canonical"}],
    "invariants": [{"key": "one-row", "title": "One row per booking", "body": "never two", "enforced_by": "db",
                    "breaks_if": "the unique index is dropped", "evidence": ["a.py:2@abc1234"], "guards": ["core.booking"]}],
    "flows": [{"key": "book", "title": "A booking arrives", "domain": "Ingestion", "trigger": "an OTA posts a booking",
               "traced_by": "tracer", "steps": [
                   {"lane": "api", "text": "parse it", "at": "svc/a.py:2@abc1234", "writes": ["core.booking.hotel"], "pin": "one-row"},
                   {"lane": "db", "text": "store it", "at": "b.py:1@abc1234"}]}],
}


def test_loading_twice_updates_in_place(con):
    assert sysmap.load(con, MAP) == {"lanes": 2, "tables": 1, "invariants": 1, "decisions": 0, "flows": 1}
    n = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    changed = {**MAP, "lanes": [{**MAP["lanes"][0], "title": "Public API"}] + MAP["lanes"][1:]}
    sysmap.load(con, changed)
    assert con.execute("SELECT COUNT(*) FROM node").fetchone()[0] == n
    assert con.execute("SELECT title FROM node WHERE source_key='map:lane:api'").fetchone()[0] == "Public API"
    assert not views.store_lint(con)                      # map stage nodes are not lint noise
    assert views.imported_ids(con) == set()               # map: keys are live knowledge, not imported history


def test_a_bad_reference_loads_nothing_from_that_file(con):
    bad = {**MAP, "flows": [{**MAP["flows"][0], "steps": [{**MAP["flows"][0]["steps"][0], "lane": "nope"}]}]}
    with pytest.raises(ValueError, match="unknown lane"):
        sysmap.load(con, bad)
    assert con.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 0
    with pytest.raises(ValueError, match="path:line"):
        sysmap.load(con, {**MAP, "flows": [{**MAP["flows"][0], "steps": [{**MAP["flows"][0]["steps"][0], "at": "a.py"}]}]})


def test_a_pr_finds_the_flows_and_steps_it_touches(con):
    sysmap.load(con, MAP)
    hit = sysmap.flows_touching(con, ["a.py", "unrelated.py"])
    assert [(h["title"], h["steps"]) for h in hit] == [("A booking arrives", [1])]


def test_the_tracer_cannot_be_the_second_reader(con):
    sysmap.load(con, MAP)
    fid = con.execute("SELECT id FROM node WHERE source_key='map:flow:book'").fetchone()[0]
    with pytest.raises(ValueError, match="second reader"):
        sysmap.check_step(con, fid, 1, "confirmed", actor="tracer")
    sysmap.check_step(con, fid, 2, "wrong", "it stores two rows", actor="reader")
    steps = con.execute("SELECT json_extract(attrs,'$.steps[1].check.verdict') FROM node WHERE id=?", (fid,)).fetchone()[0]
    assert steps == "wrong"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_drift_says_whether_a_step_still_describes_the_code(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.py").write_text("def f():\n    save(row)\n    log(row)\n")
    git(repo, "add", "."); git(repo, "commit", "-qm", "one")
    sha = git(repo, "rev-parse", "--short", "HEAD")
    assert storyboard._drift(repo, f"a.py:2@{sha}") == ("current", None)
    (repo / "a.py").write_text("import x\n\ndef f():\n    save(row)\n    log(row)\n")
    git(repo, "commit", "-qam", "two")
    storyboard._git_show.cache_clear()
    assert storyboard._drift(repo, f"a.py:2@{sha}") == ("moved", "a.py:4")
    (repo / "a.py").write_text("def f():\n    upsert(row)\n")
    git(repo, "commit", "-qam", "three")
    storyboard._git_show.cache_clear()
    assert storyboard._drift(repo, f"a.py:2@{sha}") == ("changed", None)
    assert storyboard._drift(repo, "a.py:2") == (None, None)            # unpinned: cannot tell


def test_a_reload_keeps_checks_on_unchanged_steps_and_drops_them_on_retraced_ones(con):
    sysmap.load(con, MAP)
    fid = con.execute("SELECT id FROM node WHERE source_key='map:flow:book'").fetchone()[0]
    sysmap.check_step(con, fid, 1, "confirmed", actor="reader")
    sysmap.check_step(con, fid, 2, "confirmed", actor="reader")
    flow = MAP["flows"][0]
    retraced = {**flow, "steps": [flow["steps"][0], {**flow["steps"][1], "at": "b.py:3@abc1234"}]}
    sysmap.load(con, {"flows": [retraced]})
    got = [con.execute(f"SELECT json_extract(attrs,'$.steps[{i}].check.verdict') FROM node WHERE id=?", (fid,)).fetchone()[0] for i in (0, 1)]
    assert got == ["confirmed", None]
