"""Every test runs on a fresh tmp SQLite file. Nothing here touches a real store."""
import json
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

from coordlite import views
from coordlite.__main__ import boot_packet
from coordlite.db import connect, get_node, KINDS


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "store.db")
    yield c
    c.close()


def mk(con, title, kind="requirement", parent=None, **kw):
    return views.node_create(con, kind, title, kw.pop("body", "why it matters"), parent, actor=kw.pop("actor", "master"), **kw)


def test_active_requirement_needs_no_mission(con):
    n = mk(con, "R1 · SSO login")
    assert n["state"] == "active" and n["mission"] is None


def test_dropping_needs_a_reason_and_keeps_the_row(con):
    n = mk(con, "R2 · Export")
    with pytest.raises(ValueError):
        views.node_edit(con, n["id"], {"state": "dropped"}, actor="w1")
    d = views.node_edit(con, n["id"], {"state": "dropped", "why_dropped": "out of scope per PM"}, actor="w1")
    assert d["state"] == "dropped" and d["attrs"]["history"][-1]["by"] == "w1"


def test_supersede_records_the_reversal(con):
    old = mk(con, "R3 · Store in Postgres", status="DEFINED")
    got = views.node_supersede(con, old["id"], "requirement", "R3 · Store in DynamoDB", "infra mandate", actor="w2")
    assert got["old"]["attrs"]["superseded_by"] == got["new"]["id"]
    assert got["old"]["title"] == "R3 · Store in Postgres"          # the old belief keeps its words
    rel = con.execute("SELECT rel FROM edge WHERE src=? AND dst=?", (got["new"]["id"], old["id"])).fetchone()
    assert rel["rel"] == "supersedes"


def test_subtree_is_one_transaction(con):
    root = mk(con, "Q4 requirements", kind="plan")
    with pytest.raises(ValueError):
        views.subtree_create(con, root["id"], [{"kind": "requirement", "title": "ok"},
                                               {"kind": "requirement", "title": "bad edge", "edges": [["blocks", 99999]]}], "master")
    assert con.execute("SELECT COUNT(*) FROM node WHERE parent_id=?", (root["id"],)).fetchone()[0] == 0
    got = views.subtree_create(con, root["id"], [
        {"kind": "requirement", "title": "R1 · Auth", "attrs": {"exit_test": "SSO e2e passes", "owner": "w1"},
         "children": [{"kind": "task", "title": "Wire SAML"}]},
        {"kind": "requirement", "title": "R2 · Audit log", "time": {"due_at": "2026-11-30"}}], "master")
    assert set(got["map"]) == {"0", "0.0", "1"}
    assert get_node(con, got["map"]["1"])["due_at"].startswith("2026-12-01T07:59:59")   # end of day, Pacific → UTC


def test_render_and_lint(con):
    root = mk(con, "Plan", kind="plan")
    a = mk(con, "R1 · Auth", parent=root["id"], attrs={"exit_test": "e2e"})
    b = mk(con, "R2 · Billing", parent=root["id"], body="needs #99999")
    views.edge_add(con, a["id"], b["id"], "blocks", actor="master")
    views.node_edit(con, b["id"], {"state": "done"}, actor="w2")
    doc = views.plan_render(con, root["id"])
    assert "R1 · Auth" in doc and "→ blocks" in doc and "Exit test: e2e" in doc
    checks = {f["check"] for f in views.plan_lint(con, root["id"])}
    assert "done_while_blocked" in checks and "dangling_reference" in checks
    assert "requirement_without_exit_test" not in checks       # b is done, a has one


def test_search_tracks_edits_and_survives_bad_syntax(con):
    n = mk(con, "R5 · Rate limiting", body="token bucket per tenant")
    assert [r["id"] for r in views.search(con, "bucket")] == [n["id"]]
    views.node_edit(con, n["id"], {"body": "sliding window per tenant"}, actor="w3")
    assert views.search(con, "bucket") == []
    assert views.search(con, "window")[0]["id"] == n["id"]
    assert views.search(con, 'tenant" AND (') [0]["id"] == n["id"]    # malformed FTS falls back to words


def test_receipts_append_only_and_only_they_touch(con):
    n = mk(con, "R6")
    views.node_edit(con, n["id"], {"title": "R6 · renamed"}, actor="w1")
    assert get_node(con, n["id"])["touched_at"] is None
    views.receipt_add(con, "pr", n["id"], {"url": "https://example/pr/1"}, actor="w1")
    assert get_node(con, n["id"])["touched_at"] is not None
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM receipt")


def test_parent_cycle_refused(con):
    a = mk(con, "A", kind="plan")
    b = mk(con, "B", parent=a["id"])
    with pytest.raises(ValueError):
        views.node_edit(con, a["id"], {"parent_id": b["id"]}, actor="master")


def test_changes_since_is_attributed(con):
    first = views.changes_since(con)["now"]
    n = mk(con, "R7", actor="w4")
    views.node_edit(con, n["id"], {"status": "BLOCKED", "body": "waiting on infra"}, actor="w4")
    feed = views.changes_since(con, first)["changes"]
    assert [c["actor"] for c in feed] == ["w4", "w4"]
    assert views.changes_since(con, first, exclude_actor="w4")["changes"] == []


def test_concurrent_writers_lose_nothing(tmp_path):
    path = tmp_path / "store.db"
    seed = connect(path)
    n = mk(seed, "shared")
    errors = []

    def worker(name):
        c = connect(path)
        try:
            for i in range(25):
                views.node_edit(c, n["id"], {"body": f"{name} {i}"}, actor=name)
                views.node_create(c, "task", f"{name}-{i}", None, n["id"], actor=name)
        except Exception as e:   # noqa: BLE001
            errors.append(e)
        finally:
            c.close()
    ts = [threading.Thread(target=worker, args=(f"w{k}",)) for k in range(5)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert errors == []
    assert seed.execute("SELECT COUNT(*) FROM node WHERE parent_id=?", (n["id"],)).fetchone()[0] == 125
    assert seed.execute("SELECT COUNT(*) FROM change WHERE what LIKE '#%: body'").fetchone()[0] == 125


def test_handoff_boot_packet(con):
    root = mk(con, "Plan", kind="plan")
    mk(con, "R1 · Auth", parent=root["id"])
    assert "first session" in boot_packet(con)
    views.brief_write(con, "master", {"purpose": "ship 20 reqs", "happened": "R1 split", "open": "R4 owner",
                                      "next": "ask w2 about R4", "ruled_out": "monorepo"}, actor="master")
    later = "2999-01-01T00:00:00Z"
    pkt = boot_packet(con, "master", since=later)
    assert "ask w2 about R4" in pkt and "R1 · Auth" in pkt and "WARNING" in pkt
    assert "WARNING" not in boot_packet(con, "master", since="2000-01-01T00:00:00Z")
    out = views.session_handoff(con, "master", "ses_abc", tokens=500_000)
    ended = con.execute("SELECT * FROM session WHERE id=?", (out["ended"],)).fetchone()
    assert ended["ended_why"] == "handoff" and ended["brief_node_id"] is not None and out["opened"] != out["ended"]


def test_widening_kinds_keeps_rows_and_search(tmp_path, monkeypatch):
    path = tmp_path / "store.db"
    c = connect(path)
    n = mk(c, "R9 · keep me", body="searchable text")
    c.close()
    import coordlite.db as db
    monkeypatch.setattr(db, "KINDS", KINDS | {"risk"})
    c = db.connect(path)
    assert db.get_node(c, n["id"])["title"] == "R9 · keep me"
    c.execute("INSERT INTO node (kind,title,state,created_at,updated_at) VALUES ('risk','vendor lock-in','active','x','x')")
    assert {r["id"] for r in views.search(c, "searchable")} == {n["id"]}
    assert views.search(c, "vendor")


def test_mcp_server_over_stdio(tmp_path):
    """The real server, launched the way OpenCode launches it, answering a real MCP client."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "COORD_DB": str(tmp_path / "store.db")}

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "coordlite", "serve"], env=env)
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            made = await s.call_tool("node_create", {"actor": "master", "kind": "plan", "title": "Root"})
            root = json.loads(made.content[0].text)
            await s.call_tool("subtree_create", {"actor": "master", "parent_id": root["id"],
                                                 "nodes": [{"kind": "requirement", "title": "R1 · Auth"}]})
            rendered = (await s.call_tool("plan_render", {"root_id": root["id"]})).content[0].text
            denied = (await s.call_tool("query", {"sql": "DELETE FROM node"})).content[0].text
            return names, rendered, denied
    names, rendered, denied = anyio.run(run)
    assert {"node_create", "node_edit", "node_supersede", "subtree_create", "plan_render", "plan_lint", "search",
            "changes_since", "brief_write", "brief_read", "query", "schema", "receipt_add", "edge_add"} <= names
    assert "R1 · Auth" in rendered
    assert "readonly" in denied.lower() or "read-only" in denied.lower()


def test_cli_handoff_prints_boot(tmp_path):
    env = {**os.environ, "COORD_DB": str(tmp_path / "store.db")}
    out = subprocess.run([sys.executable, "-m", "coordlite", "handoff", "--scope", "master", "--session", "ses_1",
                          "--tokens", "123"], env=env, capture_output=True, text=True, check=True).stdout
    assert out.startswith("# HANDOFF")


def test_map_export_and_concept_links(con):
    root = mk(con, "Codebase map", kind="concept", body="entry point")
    a = mk(con, "Auth service", kind="concept", parent=root["id"], body="issues tokens")
    b = mk(con, "Billing", kind="concept", parent=root["id"], body=f"calls #{a['id']} for tokens")
    views.edge_add(con, b["id"], a["id"], "derived_from", actor="w1")
    gone = mk(con, "Old cron", kind="concept", parent=root["id"])
    views.node_edit(con, gone["id"], {"state": "dropped", "why_dropped": "deleted in 2025"}, actor="w1")
    doc = views.map_export(con, root["id"])
    lines = doc.splitlines()
    assert lines[0].startswith("> **For AI agents:**")
    assert lines[2].startswith("[1] **Codebase map** - entry point (covers [2][3])")
    assert lines[4].startswith("[3] **Billing** - calls [2] for tokens (part of [1]; derived from [2])")
    assert "Old cron" not in doc
    under = {f["node_id"] for f in views.plan_lint(con, root["id"]) if f["check"] == "concept_underlinked"}
    assert a["id"] not in under and b["id"] not in under            # parent link counts once, plus each other


def _age(con, node_id, days):
    con.execute("UPDATE node SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now',?) WHERE id=?", (f"-{days} days", node_id))


def test_store_lint_sees_what_one_root_cannot(con):
    root = mk(con, "Plan", kind="plan")
    stray = mk(con, "Loose task", kind="task")                                     # no parent, not a root kind
    old = mk(con, "Use Kafka for events", kind="decision", parent=root["id"], status="PROPOSED")
    _age(con, old["id"], 30)
    mk(con, "Cache user sessions in Redis", kind="task", parent=root["id"])
    mk(con, "Cache the user sessions in Redis", kind="task", parent=root["id"])
    checks = {(f["check"], f["node_id"]) for f in views.store_lint(con)}
    assert ("orphan_root", stray["id"]) in checks
    assert ("stale_proposed", old["id"]) in checks and ("proposed_without_owner", old["id"]) in checks
    assert any(c == "possible_duplicate" for c, _ in checks)
    over = {f["check"] for f in views.store_lint(con, proposed_budget=0)}
    assert "proposed_budget" in over


def test_hygiene_sweep_is_a_dry_run_until_applied(con):
    root = mk(con, "Plan", kind="plan")
    old = mk(con, "Maybe GraphQL", kind="decision", parent=root["id"], status="PROPOSED")
    fresh = mk(con, "Maybe gRPC", kind="decision", parent=root["id"], status="PROPOSED")
    _age(con, old["id"], 20)
    dry = views.hygiene_sweep(con, days=14, actor="master")
    assert [n["id"] for n in dry["would_park"]] == [old["id"]] and get_node(con, old["id"])["state"] == "active"
    views.hygiene_sweep(con, days=14, apply=True, actor="master")
    assert get_node(con, old["id"])["state"] == "parked" and get_node(con, fresh["id"])["state"] == "active"
    assert get_node(con, old["id"])["attrs"]["history"][-1]["before"]["state"] == "active"   # reversible, with history
    assert "stale_proposed" not in {f["check"] for f in views.store_lint(con)}


def test_receipt_dedupe_makes_reingest_a_noop(con):
    n = mk(con, "Auth concept", kind="concept")
    first = views.receipt_add(con, "pr", n["id"], {"url": "u"}, actor="w1", dedupe_key="pr:org/repo#7@abc")
    again = views.receipt_add(con, "pr", n["id"], {"url": "u"}, actor="w1", dedupe_key="pr:org/repo#7@abc")
    assert first["duplicate"] is False and again["duplicate"] is True
    assert con.execute("SELECT COUNT(*) FROM receipt").fetchone()[0] == 1


def test_journal_is_append_only_and_dedupes_a_burst(con):
    a = views.journal_write(con, "w1", "**Context:** auth", session_id="s1", kind="flush")
    b = views.journal_write(con, "w1", "**Context:** again", session_id="s1", kind="flush")
    assert b == {"id": a["id"], "duplicate": True}
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE journal SET body='rewritten' WHERE id=?", (a["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM journal")
    with pytest.raises(ValueError):
        views.journal_write(con, "w1", "   ")


def test_compile_loop_bookkeeping(con):
    j1 = views.journal_write(con, "w1", views.journal_sections({"context": "billing", "lessons": "retries double-charge"}))
    j2 = views.journal_write(con, "w2", "**Context:** nothing much")
    assert views.journal_pending(con)["pending_total"] == 2
    assert "**Lessons:**" in views.journal_pending(con)["entries"][0]["body"]
    root = mk(con, "Codebase map", kind="concept", body="the system")
    lesson = mk(con, "Retries double-charge", kind="lesson", parent=root["id"], body="Idempotency key missing on retry.")
    stats = views.journal_mark_compiled(con, [j1["id"], j2["id"]], [lesson["id"]], actor="master")
    assert stats["pending"] == 0 and stats["last_compiled"]
    con.execute("UPDATE journal SET compiled_at=NULL WHERE id=?", (j2["id"],))   # bookkeeping columns stay mutable
    idx = views.knowledge_index(con)
    assert f"#{lesson['id']} lesson: Retries double-charge — Idempotency key missing on retry." in idx


def test_lint_flags_journal_backlog_and_sparse_concepts(con):
    root = mk(con, "Codebase map", kind="concept", body="entry")
    thin = mk(con, "Cache", kind="concept", parent=root["id"], body="it caches")
    old = views.journal_write(con, "w1", "**Context:** old")
    con.execute("PRAGMA ignore_check_constraints=1")
    con.execute("DROP TRIGGER journal_body_immutable")                            # test-only: age the entry
    con.execute("UPDATE journal SET at=strftime('%Y-%m-%dT%H:%M:%fZ','now','-3 days') WHERE id=?", (old["id"],))
    checks = {f["check"]: f for f in views.store_lint(con)}
    assert checks["concept_sparse"]["node_id"] == thin["id"]
    assert "journal_backlog" in checks


def test_boot_packet_carries_index_and_backlog(con):
    root = mk(con, "Codebase map", kind="concept", body="entry point of the system, see children")
    views.journal_write(con, "w1", "**Context:** x")
    pkt = boot_packet(con)
    assert "## Knowledge index" in pkt and f"#{root['id']} concept: Codebase map" in pkt
    assert "1 uncompiled journal entries" in pkt


def test_an_undecided_node_is_owned_by_its_creator_unless_named(con):
    root = mk(con, "Plan", kind="plan")
    a = mk(con, "Maybe a circuit breaker", kind="decision", parent=root["id"], status="PROPOSED", actor="master")
    b = mk(con, "Maybe gRPC", kind="decision", parent=root["id"], status="PROPOSED", actor="master", attrs={"owner": "w2"})
    assert a["attrs"]["owner"] == "master" and b["attrs"]["owner"] == "w2"
