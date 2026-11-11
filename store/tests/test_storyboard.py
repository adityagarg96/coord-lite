"""The storyboard at scale: pointers across several checkouts, flows grouped by domain, tables grouped and split
into the ones the current flow touches. Fresh SQLite and throwaway git repos under tmp_path per test."""
import subprocess

import pytest

from coordlite import views
from coordlite.db import connect
from coordlite.storyboard import MAX_LANES, OPEN_ALL_TABLES, board_data, board_html


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "store.db")
    yield c
    c.close()


def mk(con, title, parent=None, kind="concept", **attrs):
    return views.node_create(con, kind, title, "a body", parent, actor="t", attrs=attrs)["id"]


def git_repo(path, files):
    path.mkdir(parents=True)
    for name, text in files.items():
        (path / name).write_text(text)
    g = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(g + ["init", "-q"], check=True)
    subprocess.run(g + ["add", "."], check=True)
    subprocess.run(g + ["commit", "-qm", "init"], check=True)
    return subprocess.run(g + ["rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def stage(con, n_lanes=2):
    root = mk(con, "System")
    lanes = [mk(con, f"Lane {i}", root, lane=i) for i in range(n_lanes)]
    return root, lanes


def test_pointers_resolve_across_several_checkouts_and_in_one(con, tmp_path):
    parent = tmp_path / "code"
    a = git_repo(parent / "alpha", {"a.py": "one\ntwo\nthree\n"})
    b = git_repo(parent / "beta", {"b.py": "x\n"})
    root, (lane, _) = stage(con)
    steps = [{"lane": lane, "text": "in alpha", "at": f"alpha/a.py:2-3@{a}"},
             {"lane": lane, "text": "past the end", "at": f"beta/b.py:9@{b}"},
             {"lane": lane, "text": "wrong sha for this checkout", "at": f"beta/b.py:1@{a}"},
             {"lane": lane, "text": "working tree", "at": "alpha/a.py:3"},
             {"lane": lane, "text": "no such checkout", "at": f"gamma/c.py:1@{a}"}]
    flow = mk(con, "Cross-repo flow", root, trigger="t", steps=steps)
    d = board_data(con, repo=str(parent))
    assert [s["ok"] for s in d["flows"][0]["steps"]] == [True, False, False, True, False]
    assert d["broken"] == 3 and d["pointers"] == 5 and d["steps"] == 5
    # a single checkout keeps working: paths are relative to it, as before
    views.node_edit(con, flow, {}, actor="t", attrs={"steps": [{"lane": lane, "text": "s", "at": f"a.py:2@{a}"}]})
    single = board_data(con, repo=str(parent / "alpha"))
    assert single["flows"][0]["steps"][0]["ok"] is True and single["broken"] == 0


def test_flows_are_grouped_by_domain_in_a_fixed_order(con):
    root, (lane, _) = stage(con)
    ingest, recon = mk(con, "Ingest", root), mk(con, "Reconcile", root)
    step = [{"lane": lane, "text": "s"}]
    late = mk(con, "Match", recon, trigger="nightly", steps=step)
    early = mk(con, "Scrape", ingest, trigger="nightly", steps=step)
    loose = mk(con, "Loose", None, trigger="x", steps=step)
    d = board_data(con)
    assert [x["title"] for x in d["domains"]] == ["Ingest", "Reconcile"]
    assert [x["flows"] for x in d["domains"]] == [1, 1]
    assert [f["id"] for f in d["flows"]] == [early, late, loose]          # by domain, then the rest
    assert [f["domain"] for f in d["flows"]] == [ingest, recon, None]


def test_tables_group_by_schema_or_parent_and_the_flow_knows_what_it_touches(con):
    root, (lane, _) = stage(con)
    data, misc = mk(con, "Data", root), mk(con, "Misc", root)
    res = mk(con, "core.reservations", data, entity="core.reservations", columns=["id", "status"])
    raw = mk(con, "public.raw_rows", data, entity="public.raw_rows", columns=["id"])
    log = mk(con, "log", misc, entity="log", columns=["at"])
    perm = mk(con, "auth.perm", data, entity="auth.perm", columns=["key"])
    mk(con, "Flow", root, trigger="t", steps=[
        {"lane": lane, "text": "read", "reads": ["public.raw_rows.id", "core.reservations_archive.id"]},
        {"lane": lane, "text": "write", "writes": ["core.reservations.status", "log"]}])
    d = board_data(con)
    groups = {e["table"]: e["group"] for e in d["entities"]}
    assert groups == {"core.reservations": "core", "public.raw_rows": "public", "log": "Misc", "auth.perm": "auth"}
    assert [e["group"] for e in d["entities"]] == sorted(e["group"] for e in d["entities"])
    f = d["flows"][0]
    assert set(f["tables"]) == {res, raw, log} and perm not in f["tables"]
    assert f["unknown"] == ["core.reservations_archive.id"]               # a prefix of a real table is not that table
    assert d["open_all"] is True


def test_a_big_schema_starts_collapsed(con):
    root, _ = stage(con)
    for i in range(OPEN_ALL_TABLES + 1):
        mk(con, f"core.t{i}", root, entity=f"core.t{i}", columns=["id"])
    assert board_data(con)["open_all"] is False


def test_too_many_lanes_and_stray_steps_are_reported(con):
    root, lanes = stage(con, MAX_LANES + 1)
    mk(con, "Flow", root, trigger="t", steps=[{"lane": lanes[0], "text": "ok"}, {"lane": 999999, "text": "stray"}])
    w = board_data(con)["warnings"]
    assert len(w) == 2 and w[0].startswith(f"{MAX_LANES + 1} lanes") and "no known lane" in w[1]


def test_the_page_has_the_filter_and_never_embeds_text_as_html(con):
    root, (lane, _) = stage(con)
    dom = mk(con, "</script><script>alert(1)</script>", root)
    mk(con, "core.x", root, entity="core.x", columns=["</script>"])
    mk(con, "Flow", dom, trigger="t", steps=[{"lane": lane, "text": "s", "reads": ["core.x"]}])
    html = board_html(con)
    assert 'id="tq"' in html and "</script><script>alert" not in html and "innerHTML" not in html
