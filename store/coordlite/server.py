"""The `store` MCP server (stdio). OpenCode launches one per instance; every instance opens the same SQLite file.

Reads: `schema` / `query` run on a separate connection that is read-only at the driver (mode=ro + query_only),
so no statement a model writes can change a row. Writes: shaped tools only, each landing in views.py, each
attributed to an `actor` (the calling session's peer name) in node history and the change log.
"""
import json
import sqlite3
import time

from mcp.server.mcpserver import MCPServer

from . import views
from .db import DB_PATH, connect

MAX_ROWS = 200
MAX_SECONDS = 3.0

mcp = MCPServer("store")
_con = None


def con():
    global _con
    if _con is None:
        _con = connect(DB_PATH)
    return _con


def _ro() -> sqlite3.Connection:
    con()  # make sure the file and schema exist before a read-only open
    c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only = ON")
    deadline = time.monotonic() + MAX_SECONDS
    c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    return c


def _j(fn, *a, **kw) -> str:
    try:
        return json.dumps(fn(*a, **kw), ensure_ascii=False, default=str)
    except KeyError as e:
        return json.dumps({"error": f"no such id: {e}"})
    except (ValueError, sqlite3.Error) as e:
        return json.dumps({"error": str(e)})


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------- reads

@mcp.tool()
def schema() -> str:
    """The store's tables and columns (CREATE statements). Call this before writing a non-trivial query."""
    c = _ro()
    try:
        return "\n\n".join(r["sql"] for r in c.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'node_fts_%' ORDER BY type, name"))
    finally:
        c.close()


@mcp.tool()
def query(sql: str) -> str:
    """Run ONE read-only SQL statement against the store (SQLite). Returns JSON rows, capped at 200.
    node.attrs is JSON (json_extract(attrs,'$.key')); parent_id is the single containment parent;
    edge(src,dst,rel) is the many-to-many graph; change(at,actor,node_id,what) is the write log."""
    c = _ro()
    try:
        cur = c.execute(sql)
        rows = cur.fetchmany(MAX_ROWS + 1)
        cols = [d[0] for d in cur.description or []]
        return json.dumps({"columns": cols, "rows": [list(r) for r in rows[:MAX_ROWS]], "truncated": len(rows) > MAX_ROWS},
                          ensure_ascii=False, default=str)
    except sqlite3.Error as e:
        return json.dumps({"error": str(e)})
    finally:
        c.close()


@mcp.tool()
def search(q: str, kind: str | None = None, include_closed: bool = True, limit: int = 20) -> str:
    """Full-text search (bm25) over node titles and bodies; returns id, kind, state, title and a [highlighted]
    snippet. Prefer this over LIKE queries. When the top hits are close and sit under different parents, show
    the candidates instead of picking one."""
    return _j(views.search, con(), q, kind, include_closed, limit)


@mcp.tool()
def node_get(node_id: int) -> str:
    """One node with its parent, children and edges (both directions)."""
    return _j(lambda: views.node_detail(con(), node_id) or {"error": f"no node #{node_id}"})


@mcp.tool()
def plan_render(root_id: int, depth: int = 99) -> str:
    """A subtree as a readable document: numbered branches, [STATUS] pills, bodies as arguments, exit tests,
    due dates, dead branches with reasons, and edges as → lines. Read this before editing a plan; re-run after."""
    return views.plan_render(con(), root_id, depth) or json.dumps({"error": f"no node #{root_id}"})


@mcp.tool()
def plan_lint(root_id: int) -> str:
    """Deterministic consistency checks over a subtree: DEFINED under dropped, open under done, requirement or
    checkpoint without an exit test, status without an argument, superseded yet still DEFINED, overdue, done
    while a blocker is open, dangling #refs, duplicate titles. Run after every batch of edits."""
    return _j(lambda: (lambda r: {"error": f"no node #{root_id}"} if r is None else r)(views.plan_lint(con(), root_id)))


@mcp.tool()
def map_export(root_id: int, depth: int = 99) -> str:
    """A subtree as a plain-text mind map ([N] **Title** - body with [N] links), for humans and teammates without
    the store. Write it to MIND_MAP.md in the repo when the user asks, or after the map changes materially."""
    return views.map_export(con(), root_id, depth) or json.dumps({"error": f"no node #{root_id}"})


@mcp.tool()
def changes_since(since: str | None = None, limit: int = 100, exclude_actor: str | None = None) -> str:
    """Every write after `since` (pass the `now` from your previous call; omit for the latest). Use it after a
    peer message, and whenever you resume, to see what other sessions changed. exclude_actor hides your own writes."""
    return _j(views.changes_since, con(), since, limit, exclude_actor)


@mcp.tool()
def brief_read(scope: str = "master") -> str:
    """The brief a scope's previous session left (purpose · happened · open · next · ruled_out)."""
    r = views.brief_node(con(), scope)
    return json.dumps({"scope": scope, "body": r["body"], "updated_at": r["updated_at"]} if r else {"scope": scope, "body": None})


# ---------------------------------------------------------------- writes (actor = your peer name, e.g. "master", "w3-auth")

@mcp.tool()
def node_create(actor: str, kind: str, title: str, body: str = "", parent_id: int | None = None,
                status: str | None = None, state: str | None = None, attrs: dict | None = None,
                due_at: str | None = None) -> str:
    """Create ONE node. kind: requirement|concept|project|plan|task|decision|checkpoint|lesson|note|idea|goal (concept = a knowledge-map node: architecture, data flow, a subsystem; link it with #id refs in the body). parent_id is its
    single containment parent (omit for a root). status: DEFINED|DEEP_DIVE|PUNT|PROPOSED|RATIFIED|CONFIRMED|DRAFT|BLOCKED
    or null. state defaults to active (seed for idea/task). attrs is free JSON; attrs.exit_test (how we know it is
    done) and attrs.owner (which session) are read by render and lint. For a whole branch use subtree_create."""
    return _j(views.node_create, con(), kind, title, body or None, parent_id, status, attrs, state, actor,
              time=_clean({"due_at": due_at}))


@mcp.tool()
def node_edit(actor: str, node_id: int, title: str | None = None, body: str | None = None, summary: str | None = None,
              state: str | None = None, why_dropped: str | None = None, parent_id: int | None = None,
              status: str | None = None, due_at: str | None = None, attrs: dict | None = None) -> str:
    """Edit an EXISTING node; only the fields you pass change, and the prior values are kept in its history.
    state: seed|active|parked|done|dropped (dropping needs why_dropped; nothing is deleted). Do NOT use this to
    change your mind about a DEFINED decision or requirement: use node_supersede so the reversal is recorded.
    attrs merges keys into the node's attrs (null removes one): e.g. owner, exit_test, to_verify (unverified claims),
    imported: true (marks a whole branch as history that lint and the sweep leave alone)."""
    return _j(views.node_edit, con(), node_id, _clean({"title": title, "body": body, "summary": summary, "state": state,
                                                       "why_dropped": why_dropped, "parent_id": parent_id,
                                                       "status": status, "due_at": due_at}), actor, attrs)


@mcp.tool()
def node_supersede(actor: str, old_id: int, kind: str, title: str, body: str, parent_id: int | None = None,
                   status: str = "DEFINED") -> str:
    """Record a REVERSAL: a new node that supersedes old_id (edge new→old; the old node keeps its text and is
    struck through in renders). Use when a requirement or decision changes, so the history of why survives."""
    return _j(views.node_supersede, con(), old_id, kind, title, body, parent_id, status, actor)


@mcp.tool()
def subtree_create(actor: str, nodes: list, parent_id: int | None = None) -> str:
    """Create a nested branch in ONE transaction. nodes: [{kind, title, body?, status?, state?, attrs?,
    time?: {due_at}, edges?: [[rel, existing_id]], children?: [...]}]. Returns ids and a path→id map ('0.2.1')."""
    return _j(views.subtree_create, con(), parent_id, nodes, actor)


@mcp.tool()
def edge_add(actor: str, src: int, dst: int, rel: str, note: str | None = None) -> str:
    """Add a graph edge src→dst. rel: blocks|relates_to|derived_from|evidences|informs|duplicates|serves|targets|
    instance_of|supersedes (prefer node_supersede for reversals)."""
    return _j(views.edge_add, con(), src, dst, rel, note, actor)


@mcp.tool()
def edge_remove(actor: str, edge_id: int, why: str) -> str:
    """Delete ONE wrong edge, with a reason (kept in the change log)."""
    return _j(views.edge_remove, con(), edge_id, why, actor)


@mcp.tool()
def receipt_add(actor: str, source: str, node_id: int, payload: dict | None = None, ts: str | None = None,
                dedupe_key: str | None = None) -> str:
    """Record evidence of progress on a node (source: 'pr', 'test', 'commit', 'doc', 'meeting'…; payload: url, sha,
    result). Receipts are append-only and are the only thing that marks a node as recently worked on. Pass a
    dedupe_key (e.g. 'pr:org/repo#123@<head sha>') to make re-recording the same evidence a no-op."""
    return _j(views.receipt_add, con(), source, node_id, payload, ts, actor, dedupe_key)


@mcp.tool()
def knowledge_index() -> str:
    """The catalogue of what we know: one line per live concept, decision, lesson and Q&A, grouped by parent.
    Read it before answering a question about the codebase or past decisions, then open nodes with node_get."""
    return views.knowledge_index(con())


@mcp.tool()
def journal_write(actor: str, context: str, decisions: str = "", proposals: str = "", lessons: str = "", actions: str = "") -> str:
    """Note what mattered in this stretch of work, for the knowledge-compile skill to fold into the graph later:
    context (one line: what you were doing), decisions (with why), lessons (gotchas, what failed and why),
    actions (follow-ups). decisions = only choices actually made or agreed; proposals = suggestions not yet agreed
    (never promote one to the other). Skip routine tool calls and trivia. Use it at milestones; it does not touch the graph."""
    return _j(views.journal_write, con(), actor,
              views.journal_sections({"context": context, "decisions": decisions, "proposals": proposals,
                                         "lessons": lessons, "actions": actions}),
              kind="manual")


@mcp.tool()
def journal_pending(limit: int = 20) -> str:
    """Uncompiled journal entries, oldest first, with the total backlog. Input to the knowledge-compile skill."""
    return _j(views.journal_pending, con(), limit)


@mcp.tool()
def journal_mark_compiled(actor: str, journal_ids: list, node_ids: list) -> str:
    """Mark journal entries as compiled, recording the nodes they went into (empty list if nothing was worth keeping)."""
    return _j(views.journal_mark_compiled, con(), journal_ids, node_ids, actor)


@mcp.tool()
def store_lint(stale_days: int = 14, proposed_budget: int = 15, include_imported: bool = False) -> str:
    """Store-wide hygiene: every root's plan_lint plus orphan roots, PROPOSED/DRAFT nodes untouched for stale_days,
    undecided nodes without an owner, roots over the undecided budget, and likely-duplicate siblings. The master
    runs this at boot and after each batch of worker reports, and clears what it can. Imported history (nodes
    loaded from a record, or under a root marked attrs.imported) is skipped unless include_imported."""
    return _j(views.store_lint, con(), stale_days, proposed_budget, include_imported)


@mcp.tool()
def hygiene_sweep(actor: str, days: int = 14, apply: bool = False) -> str:
    """Park PROPOSED/DRAFT nodes nobody has touched for `days`. Dry run by default: call once without apply, show
    the user the list, then apply. Parking is reversible (set state back to active); nothing is deleted."""
    return _j(views.hygiene_sweep, con(), days, apply, actor)


@mcp.tool()
def brief_write(actor: str, scope: str, purpose: str, happened: str, open: str, next: str, ruled_out: str) -> str:
    """REWRITE (never append) a scope's brief for its successor session: purpose · happened · open · next ·
    ruled_out, ≤1200 chars each. Called at handoff; write it so a fresh session can continue without asking."""
    return _j(views.brief_write, con(), scope, {"purpose": purpose, "happened": happened, "open": open,
                                               "next": next, "ruled_out": ruled_out}, actor)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
