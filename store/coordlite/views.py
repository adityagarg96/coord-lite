"""Reads and the ONE write path. Every MCP write tool lands here, so validation and attribution are identical
for every session. Each write records who made it (`actor`) in node history and the change log."""
import json
import re

from . import timing
from .db import add_edge, add_receipt, get_node, insert_node, log_change, node_dict, now_iso, tx, update_node, KINDS, RELS

EDITABLE = ("title", "body", "summary", "state", "why_dropped", "parent_id", "status", *timing.TIME_FIELDS)
STATUSES = {"DEFINED", "DEEP_DIVE", "PUNT", "PROPOSED", "RATIFIED", "CONFIRMED", "DRAFT", "BLOCKED", None}
STATES_SETTABLE = {"raw", "seed", "active", "parked", "done", "dropped"}


def _fill_from_parent(con, parent_id):
    p = get_node(con, int(parent_id)) if parent_id else None
    return p, (p or {}).get("capital"), (p or {}).get("mission")


# ---------------------------------------------------------------- reads

def node_detail(con, node_id: int) -> dict | None:
    node = get_node(con, node_id)
    if node is None:
        return None
    parent = get_node(con, node["parent_id"]) if node["parent_id"] else None
    children = [node_dict(con, r) for r in con.execute("SELECT * FROM node WHERE parent_id = ? ORDER BY id", (node_id,))]
    edges = []
    for r in con.execute("SELECT * FROM edge WHERE src = ? OR dst = ? ORDER BY id", (node_id, node_id)):
        out = r["src"] == node_id
        other = get_node(con, r["dst"] if out else r["src"])
        edges.append({"id": r["id"], "rel": r["rel"], "dir": "out" if out else "in", "note": r["note"],
                      "node": {k: other[k] for k in ("id", "kind", "title", "state", "status")} if other else None})
    return {"node": node, "parent": parent, "children": children, "edges": edges}


def search(con, q: str, kind: str | None = None, include_closed: bool = True, limit: int = 20) -> list[dict]:
    """bm25 over title+body. A malformed FTS query (stray quote, bare operator) is retried as a quoted phrase
    per word, so a model's natural-language query never errors."""
    def run(match):
        where, args = ["node_fts MATCH ?"], [match]
        if kind:
            where.append("n.kind = ?"); args.append(kind)
        if not include_closed:
            where.append("n.state NOT IN ('done','dropped')")
        sql = (f"SELECT n.id, n.kind, n.state, n.status, n.title, n.parent_id, "
               f"snippet(node_fts, 1, '[', ']', '…', 12) AS snippet, bm25(node_fts, 5.0, 1.0) AS score "
               f"FROM node_fts JOIN node n ON n.id = node_fts.rowid WHERE {' AND '.join(where)} "
               f"ORDER BY score LIMIT ?")
        return [dict(r) for r in con.execute(sql, (*args, int(limit)))]
    try:
        return run(q)
    except Exception:
        words = [w for w in re.findall(r"\w+", q or "")]
        if not words:
            return []
        return run(" OR ".join(f'"{w}"' for w in words))


def changes_since(con, since: str | None = None, limit: int = 100, exclude_actor: str | None = None) -> dict:
    """The coordination feed: every write after `since` (an ISO timestamp from a previous call's `now`)."""
    where, args = [], []
    if since:
        where.append("at > ?"); args.append(since)
    if exclude_actor:
        where.append("actor != ?"); args.append(exclude_actor)
    rows = con.execute("SELECT * FROM change" + (f" WHERE {' AND '.join(where)}" if where else "") +
                       " ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()
    return {"now": now_iso(), "changes": [dict(r) for r in reversed(rows)]}


# ---------------------------------------------------------------- writes

def node_create(con, kind: str, title: str, body: str | None = None, parent_id=None, status=None,
                attrs: dict | None = None, state: str | None = None, actor: str = "unknown",
                why_dropped: str | None = None, time: dict | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    if not (title or "").strip():
        raise ValueError("title required")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(s for s in STATUSES if s)}")
    if state is not None and state not in STATES_SETTABLE:
        raise ValueError(f"state must be one of {sorted(STATES_SETTABLE)}")
    with tx(con):
        parent, capital, mission = _fill_from_parent(con, parent_id)
        if parent_id and parent is None:
            raise ValueError("no such parent")
        state = state or ("seed" if kind in ("idea", "task") else "active")
        if state == "dropped" and not why_dropped:
            raise ValueError("a dropped node needs why_dropped")
        tf = {k: v for k, v in (time or {}).items() if k in timing.TIME_FIELDS}
        nid = insert_node(con, kind=kind, title=title.strip(), body=body, parent_id=parent["id"] if parent else None,
                          capital=capital, mission=mission, state=state, status=status, why_dropped=why_dropped,
                          attrs={**(attrs or {}), "created_by": actor}, **tf)
        log_change(con, actor, nid, f"created {kind} '{title.strip()[:60]}'" + (f" under #{parent['id']}" if parent else ""))
    return get_node(con, nid)


def node_edit(con, node_id: int, fields: dict, actor: str = "unknown") -> dict:
    """Only the fields passed change. History (who, when, what it was before) is kept on the node."""
    if set(fields) - set(EDITABLE):
        raise ValueError(f"editable fields: {EDITABLE}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"status must be one of {sorted(s for s in STATUSES if s)}")
    if "state" in fields and fields["state"] not in STATES_SETTABLE:
        raise ValueError(f"state must be one of {sorted(STATES_SETTABLE)}")
    with tx(con):
        n = get_node(con, node_id)
        if n is None:
            raise KeyError(node_id)
        changes = {k: v for k, v in fields.items() if v != n.get(k)}
        summary = changes.pop("summary", None)
        if "parent_id" in changes and changes["parent_id"] is not None:
            if changes["parent_id"] == node_id or get_node(con, changes["parent_id"]) is None:
                raise ValueError("parent_id must be an existing, different node")
            if node_id in _ancestors(con, changes["parent_id"]):
                raise ValueError("parent_id would create a cycle")
            _, cap, mis = _fill_from_parent(con, changes["parent_id"])
            changes.setdefault("capital", cap)
            changes.setdefault("mission", mis)
        if changes.get("state") == "dropped" and not (changes.get("why_dropped") or n.get("why_dropped")):
            raise ValueError("dropping needs why_dropped")
        if changes.get("state") and changes["state"] != "dropped" and n["state"] == "dropped":
            changes.setdefault("why_dropped", None)
        if not changes and summary is None:
            return n
        patch = {}
        if "body" in changes:
            bv = n["attrs"].get("body_version", 0) + 1
            patch.update(body_version=bv, summary=summary, summary_body_version=bv if summary else None)
        elif summary is not None:
            patch.update(summary=summary, summary_body_version=n["attrs"].get("body_version", 0))
        history = (n["attrs"].get("history") or []) + [{"at": now_iso(), "by": actor, "before": {k: n.get(k) for k in changes}}]
        patch["history"] = history[-20:]
        update_node(con, node_id, attrs_patch=patch, **changes)
        log_change(con, actor, node_id, f"#{node_id} {n['title'][:40]}: " + ", ".join(
            [f"{k}→{str(v)[:30]}" if k != "body" else "body" for k, v in changes.items()] + (["summary"] if summary is not None else [])))
    return get_node(con, node_id)


def _ancestors(con, node_id: int) -> set[int]:
    seen, cur = set(), node_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        r = con.execute("SELECT parent_id FROM node WHERE id=?", (cur,)).fetchone()
        cur = r["parent_id"] if r else None
    return seen


def node_supersede(con, old_id: int, kind: str, title: str, body: str | None, parent_id=None,
                   status: str | None = "DEFINED", actor: str = "unknown") -> dict:
    """A reversal is RECORDED, never erased: new node + `supersedes` edge new→old; the old row keeps its text
    and gains attrs.superseded_by so renders strike it through."""
    with tx(con):
        old = get_node(con, old_id)
        if old is None:
            raise KeyError(old_id)
        new = node_create(con, kind, title, body, parent_id if parent_id is not None else old["parent_id"], status,
                          attrs={"supersedes": old_id}, actor=actor)
        add_edge(con, new["id"], old_id, "supersedes", note=f"by {actor}")
        update_node(con, old_id, attrs_patch={"superseded_by": new["id"]})
        log_change(con, actor, old_id, f"#{new['id']} supersedes #{old_id}")
    return {"new": get_node(con, new["id"]), "old": get_node(con, old_id)}


def subtree_create(con, parent_id: int | None, nodes: list, actor: str = "unknown") -> dict:
    """A nested list of {kind,title,body,status,state,attrs,time,edges,children} lands as ONE transaction, in
    document order. parent_id None makes the top-level items roots. Returns {"ids", "map": {'0.2.1': id}}."""
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("nodes must be a non-empty list")

    def check(items, path=""):
        for i, it in enumerate(items):
            if not isinstance(it, dict) or not (it.get("title") or "").strip():
                raise ValueError(f"node {path}{i}: title required")
            if it.get("kind", "plan") not in KINDS:
                raise ValueError(f"node {path}{i}: kind must be one of {sorted(KINDS)}")
            if it.get("status") not in STATUSES:
                raise ValueError(f"node {path}{i}: bad status {it.get('status')}")
            for ed in it.get("edges") or []:
                if not (isinstance(ed, (list, tuple)) and len(ed) == 2 and ed[0] in RELS):
                    raise ValueError(f"node {path}{i}: edges are [rel, existing_node_id] pairs, rel in {sorted(RELS)}")
            check(it.get("children") or [], f"{path}{i}.")
    check(nodes)
    ids, idmap = [], {}
    with tx(con):
        parent = get_node(con, int(parent_id)) if parent_id is not None else None
        if parent_id is not None and parent is None:
            raise ValueError("no such parent")

        def walk(items, pid, path):
            for i, it in enumerate(items):
                p = f"{path}{i}"
                n = node_create(con, it.get("kind", "plan"), it["title"], it.get("body"), pid, it.get("status"),
                                attrs=it.get("attrs"), state=it.get("state"), actor=actor,
                                why_dropped=it.get("why_dropped"), time=it.get("time"))
                ids.append(n["id"]); idmap[p] = n["id"]
                for rel, dst in it.get("edges") or []:
                    if get_node(con, int(dst)) is None:
                        raise ValueError(f"node {p}: edge target #{dst} does not exist")
                    add_edge(con, n["id"], int(dst), rel)
                walk(it.get("children") or [], n["id"], f"{p}.")
        walk(nodes, parent["id"] if parent else None, "")
    return {"parent_id": parent["id"] if parent else None, "ids": ids, "map": idmap}


def edge_add(con, src: int, dst: int, rel: str, note: str | None = None, actor: str = "unknown") -> dict:
    if rel not in RELS:
        raise ValueError(f"rel must be one of {sorted(RELS)}")
    with tx(con):
        if get_node(con, src) is None or get_node(con, dst) is None:
            raise ValueError("src and dst must be existing nodes")
        eid = add_edge(con, src, dst, rel, note)
        log_change(con, actor, src, f"edge #{eid}: #{src} {rel} #{dst}")
    return {"id": eid, "src": src, "dst": dst, "rel": rel, "note": note}


def edge_remove(con, edge_id: int, why: str, actor: str = "unknown") -> dict:
    """Hard-delete ONE edge row (a wrong relates_to silently misleads every later query). The removal and its
    reason stay in the change log."""
    if not (why or "").strip():
        raise ValueError("edge_remove needs a why")
    with tx(con):
        r = con.execute("SELECT * FROM edge WHERE id=?", (edge_id,)).fetchone()
        if r is None:
            raise KeyError(edge_id)
        con.execute("DELETE FROM edge WHERE id=?", (edge_id,))
        log_change(con, actor, r["src"], f"edge #{edge_id} removed (#{r['src']} {r['rel']} #{r['dst']}): {why.strip()[:80]}")
    return {"removed": edge_id, "src": r["src"], "rel": r["rel"], "dst": r["dst"]}


def receipt_add(con, source: str, node_id: int, payload: dict | None = None, ts: str | None = None,
                actor: str = "unknown", dedupe_key: str | None = None) -> dict:
    """Evidence that work happened (a PR, a test run, a doc sent). The only thing that moves touched_at.
    A repeated dedupe_key (e.g. 'pr:org/repo#123@<head sha>') is a no-op that reports duplicate=True."""
    with tx(con):
        if get_node(con, node_id) is None:
            raise KeyError(node_id)
        rid = add_receipt(con, source, timing.normalize(ts) if ts else now_iso(), node_id,
                          {**(payload or {}), "by": actor}, dedupe_key=dedupe_key)
        if rid is not None:
            log_change(con, actor, node_id, f"receipt #{rid} ({source}) on #{node_id}")
    return {"receipt_id": rid, "duplicate": rid is None, "node": get_node(con, node_id)}


# ---------------------------------------------------------------- plan documents

def _subtree(con, root_id: int, depth: int):
    root = get_node(con, root_id)
    if root is None:
        return None
    nodes, order = {root_id: root}, [root_id]

    def walk(pid, d):
        if d <= 0:
            return
        for r in con.execute("SELECT * FROM node WHERE parent_id = ? ORDER BY id", (pid,)):
            n = node_dict(con, r)
            nodes[n["id"]] = n; order.append(n["id"])
            walk(n["id"], d - 1)
    walk(root_id, depth)
    marks = ",".join("?" * len(order))
    edges = [dict(r) for r in con.execute(f"SELECT * FROM edge WHERE src IN ({marks}) OR dst IN ({marks})", (*order, *order))]
    return root, nodes, order, edges


def plan_render(con, root_id: int, depth: int = 99) -> str | None:
    """The subtree as a DOCUMENT: numbered branches, [STATUS] claims, the body as the argument, exit tests, due
    dates, and the graph as → lines. Verbatim node text, always."""
    got = _subtree(con, root_id, depth)
    if got is None:
        return None
    root, nodes, order, edges = got
    out_e, in_e = {}, {}
    for e in edges:
        out_e.setdefault(e["src"], []).append(e)
        in_e.setdefault(e["dst"], []).append(e)
    lines = [f"# {root['title']}  (#{root['id']} · {root['kind']} · {root['state']}" + (f" · {root['status']}" if root["status"] else "") + ")"]
    if root.get("body"):
        lines += ["", root["body"].strip()]
    counts = {}
    for i in order[1:]:
        s = nodes[i].get("status") or nodes[i]["state"]
        counts[s] = counts.get(s, 0) + 1
    if counts:
        lines += ["", "Counts: " + " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))]

    def title_of(i):
        n = nodes.get(i) or get_node(con, i)
        return f"#{i} {n['title'][:60]}" if n else f"#{i}"

    def emit(pid, level, prefix):
        kids = [i for i in order if nodes[i]["parent_id"] == pid and i != root_id]
        for k, i in enumerate(kids, 1):
            n = nodes[i]
            own = re.match(r"^([A-Z]{0,4}-?\d+[a-z]?)\s*[·.):]", n["title"])   # "R7 · …", "REQ-7: …" keep their own number
            num = own.group(1) if own else f"{prefix}{k}"
            strike = "~~" if n["state"] == "dropped" or n["attrs"].get("superseded_by") else ""
            pill = f"[{n['status']}] " if n.get("status") else ""
            shown = f"{strike}{n['title']}{strike}" if own else f"{num} · {strike}{n['title']}{strike}"
            head = f"{'#' * min(level + 1, 6)} {pill}{shown}  (#{i} · {n['kind']} · {n['state']})"
            lines.extend(["", head])
            if n.get("body"):
                lines.append(n["body"].strip())
            a = n["attrs"]
            if a.get("exit_test"):
                lines.append(f"Exit test: {a['exit_test']}")
            if a.get("owner"):
                lines.append(f"Owner: {a['owner']}")
            if n.get("due_at"):
                t = n["time"]
                lines.append(f"Due: {n['due_at'][:10]}" + (" (OVERDUE)" if t.get("overdue") else f" ({t.get('days_to_due')}d)"))
            if n["state"] == "dropped" and n.get("why_dropped"):
                lines.append(f"DEAD — {n['why_dropped']}")
            if a.get("superseded_by"):
                lines.append(f"→ superseded by {title_of(a['superseded_by'])}")
            for e in out_e.get(i, []):
                if e["dst"] != n["parent_id"] and e["rel"] != "supersedes":
                    lines.append(f"→ {e['rel']} {title_of(e['dst'])}" + (f" ({e['note']})" if e.get("note") else ""))
                elif e["rel"] == "supersedes":
                    lines.append(f"→ supersedes {title_of(e['dst'])}")
            for e in in_e.get(i, []):
                if e["src"] not in nodes:
                    lines.append(f"← {e['rel']} from {title_of(e['src'])}")
            emit(i, level + 1, f"{num}.")
    emit(root_id, 1, "")
    dead = [nodes[i] for i in order[1:] if nodes[i]["state"] == "dropped"]
    if dead:
        lines += ["", "## DEAD — killed with reasons"] + [f"- #{d['id']} {d['title']}: {d.get('why_dropped') or '(no reason recorded)'}" for d in dead]
    return "\n".join(lines)


_REF = re.compile(r"#(\d{1,7})\b")


def plan_lint(con, root_id: int) -> list[dict] | None:
    """Deterministic consistency checks over a subtree. No model."""
    got = _subtree(con, root_id, 99)
    if got is None:
        return None
    root, nodes, order, edges = got
    out, titles = [], {}
    open_ = lambda n: n["state"] not in ("done", "dropped")   # noqa: E731
    for i in order[1:]:
        n = nodes[i]
        titles.setdefault(n["title"].strip().lower(), []).append(i)
        parent = nodes.get(n["parent_id"])
        if parent and parent["state"] == "dropped" and open_(n) and n.get("status") == "DEFINED":
            out.append({"node_id": i, "check": "defined_under_dropped", "message": f"#{i} is DEFINED under dropped #{parent['id']}"})
        if parent and parent["state"] == "done" and open_(n):
            out.append({"node_id": i, "check": "open_under_done", "message": f"#{i} is still {n['state']} under done #{parent['id']}"})
        if n["kind"] in ("checkpoint", "requirement") and not n["attrs"].get("exit_test") and open_(n):
            out.append({"node_id": i, "check": f"{n['kind']}_without_exit_test", "message": f"{n['kind']} #{i} has no exit test (attrs.exit_test)"})
        if n.get("status") and not (n.get("body") or "").strip():
            out.append({"node_id": i, "check": "status_without_body", "message": f"#{i} carries {n['status']} but no argument"})
        if n["attrs"].get("superseded_by") and n.get("status") in ("DEFINED", "RATIFIED", "CONFIRMED") and open_(n):
            out.append({"node_id": i, "check": "superseded_still_defined", "message": f"#{i} is superseded by #{n['attrs']['superseded_by']} yet still {n['status']}"})
        if n["time"].get("overdue") and open_(n):
            out.append({"node_id": i, "check": "overdue", "message": f"#{i} was due {n['due_at'][:10]}"})
        for ref in {int(x) for x in _REF.findall(n.get("body") or "")}:
            if ref not in nodes and get_node(con, ref) is None:
                out.append({"node_id": i, "check": "dangling_reference", "message": f"#{i} mentions #{ref}, which does not exist"})
    links = {i: set() for i in order}
    for e in edges:
        links.setdefault(e["src"], set()).add(e["dst"]); links.setdefault(e["dst"], set()).add(e["src"])
    for i in order[1:]:
        p = nodes[i]["parent_id"]                       # containment is a link too ("part of [N]" in the map)
        links[i].add(p); links.setdefault(p, set()).add(i)
    for i in order:
        for ref in {int(x) for x in _REF.findall(nodes[i].get("body") or "")}:
            links[i].add(ref); links.setdefault(ref, set()).add(i)
    for i in order[1:]:
        n = nodes[i]
        if n["kind"] == "concept" and open_(n) and len(links.get(i, set()) - {i}) < 2:
            out.append({"node_id": i, "check": "concept_underlinked",
                        "message": f"concept #{i} links to {len(links.get(i, set()) - {i})} node(s); a map node needs 2+ (#refs in its body or edges)"})
    for e in edges:
        if e["rel"] == "blocks" and e["src"] in nodes and e["dst"] in nodes:
            s, d = nodes[e["src"]], nodes[e["dst"]]
            if d["state"] == "done" and open_(s):
                out.append({"node_id": d["id"], "check": "done_while_blocked", "message": f"#{d['id']} is done but #{s['id']} (blocks it) is still {s['state']}"})
    for t, ids in titles.items():
        if len(ids) > 1:
            out.append({"node_id": ids[0], "check": "duplicate_title", "message": f"duplicate title '{t[:50]}' on {ids}"})
    return out


# ---------------------------------------------------------------- hygiene (store-wide)

ROOT_KINDS = {"plan", "concept", "goal", "project", "mission", "capital"}
LIVE = ("raw", "seed", "active")
UNDECIDED = ("PROPOSED", "DRAFT")
_STOP = {"the", "a", "an", "of", "to", "and", "for", "in", "on", "with", "by", "is"}


def _words(title: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", title.lower())) - _STOP


def store_lint(con, stale_days: int = 14, proposed_budget: int = 15) -> list[dict]:
    """Everything plan_lint checks, over every open root, plus what only a store-wide view can see: orphan roots,
    undecided nodes nobody has touched in `stale_days`, roots carrying more than `proposed_budget` undecided
    nodes, undecided nodes with no owner, and likely-duplicate siblings."""
    rows = {r["id"]: dict(r) for r in con.execute(
        "SELECT id, kind, title, parent_id, state, status, attrs, updated_at, touched_at, source_key FROM node")}
    out = []

    def root_of(i):
        seen = set()
        while rows[i]["parent_id"] is not None and i not in seen:
            seen.add(i); i = rows[i]["parent_id"]
        return i
    for i, n in rows.items():
        if n["parent_id"] is None and n["state"] in LIVE and n["kind"] not in ROOT_KINDS \
                and not (n["source_key"] or "").startswith("brief:"):
            out.append({"node_id": i, "check": "orphan_root",
                        "message": f"#{i} {n['kind']} '{n['title'][:40]}' has no parent: place it under a plan or concept root"})
    cutoff = con.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)", (f"-{int(stale_days)} days",)).fetchone()[0]
    per_root = {}
    for i, n in rows.items():
        if n["status"] not in UNDECIDED or n["state"] not in LIVE:
            continue
        per_root.setdefault(root_of(i), []).append(i)
        last = max(n["updated_at"] or "", n["touched_at"] or "")
        if last < cutoff:
            out.append({"node_id": i, "check": "stale_proposed",
                        "message": f"#{i} has been {n['status']} since {last[:10]}: ratify, supersede or park it"})
        if not json.loads(n["attrs"] or "{}").get("owner"):
            out.append({"node_id": i, "check": "proposed_without_owner",
                        "message": f"#{i} is {n['status']} with no attrs.owner: someone must own the decision"})
    for r, ids in per_root.items():
        if len(ids) > proposed_budget:
            out.append({"node_id": r, "check": "proposed_budget",
                        "message": f"root #{r} carries {len(ids)} undecided nodes (budget {proposed_budget}): decide or park before adding more"})
    siblings = {}
    for i, n in rows.items():
        if n["state"] in LIVE:
            siblings.setdefault((n["parent_id"], n["kind"]), []).append(i)
    for ids in siblings.values():
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                ta, tb = rows[ids[x]]["title"], rows[ids[y]]["title"]
                if ta.strip().lower() == tb.strip().lower():
                    continue                                 # plan_lint's duplicate_title already reports it
                a, b = _words(ta), _words(tb)
                if a and b and len(a & b) / len(a | b) >= 0.75:
                    out.append({"node_id": ids[x], "check": "possible_duplicate",
                                "message": f"#{ids[x]} and #{ids[y]} look like the same thing: merge (supersede one) or retitle"})
    for i, n in rows.items():
        if n["parent_id"] is None and n["state"] in LIVE and n["kind"] in ROOT_KINDS:
            out.extend(plan_lint(con, i) or [])
    return out


def hygiene_sweep(con, days: int = 14, apply: bool = False, actor: str = "unknown") -> dict:
    """Park undecided (PROPOSED/DRAFT) nodes untouched for `days`. Dry run unless apply=True. Parking is reversible
    (state back to active); nothing is deleted, and each parked node's history says why."""
    cutoff = con.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)", (f"-{int(days)} days",)).fetchone()[0]
    stale = [dict(r) for r in con.execute(
        f"SELECT id, title, status, updated_at FROM node WHERE status IN ({','.join('?' * len(UNDECIDED))}) "
        f"AND state IN ({','.join('?' * len(LIVE))}) AND MAX(updated_at, COALESCE(touched_at, '')) < ? ORDER BY id",
        (*UNDECIDED, *LIVE, cutoff))]
    if apply:
        for n in stale:
            node_edit(con, n["id"], {"state": "parked"}, actor=actor)
            log_change(con, actor, n["id"], f"#{n['id']} parked by hygiene sweep: {n['status']} with no decision in {days}d")
    return {"applied": apply, "days": days, "parked" if apply else "would_park": stale}


MAP_HEADER = ("> **For AI agents:** this map is exported from the coordination store and is read-only here: edit the store, "
              "then re-export. Read nodes [1-5] first, then follow [N] links. Node numbers are local to this file; "
              "the store id is in the trailing (#id).")


def map_export(con, root_id: int, depth: int = 99) -> str | None:
    """A subtree in the plain-text mind-map format: one line per node, `[N] **Title** - body`, with inline #id
    references rewritten to local [N] numbers and typed edges appended as links. Breadth-first, so the root's
    direct children (the overview) come first. Dropped nodes are left out; superseded ones say so."""
    got = _subtree(con, root_id, depth)
    if got is None:
        return None
    root, nodes, order, edges = got
    bfs, frontier = [root_id], [root_id]
    while frontier:
        nxt = [i for p in frontier for i in order if nodes[i]["parent_id"] == p and nodes[i]["state"] != "dropped"]
        bfs += nxt
        frontier = nxt
    num = {nid: k for k, nid in enumerate(bfs, 1)}
    local = lambda i: f"[{num[i]}]" if i in num else f"(#{i})"   # noqa: E731
    out_e = {}
    for e in edges:
        out_e.setdefault(e["src"], []).append(e)
    lines = [MAP_HEADER, ""]
    for i in bfs:
        n = nodes[i]
        body = " ".join((n.get("body") or "").split())
        body = _REF.sub(lambda m: local(int(m.group(1))), body)
        tail = []
        if n["parent_id"] in num:
            tail.append(f"part of {local(n['parent_id'])}")
        kids = [c for c in bfs if nodes[c]["parent_id"] == i]
        if kids:
            tail.append("covers " + "".join(local(c) for c in kids))
        for e in out_e.get(i, []):
            if e["dst"] != n["parent_id"]:
                tail.append(f"{e['rel'].replace('_', ' ')} {local(e['dst'])}")
        if n["attrs"].get("superseded_by"):
            tail.append(f"superseded by {local(n['attrs']['superseded_by'])}")
        state = "" if n["state"] == "active" else f" [{n['state']}]"
        text = body + (" " if body else "") + ("(" + "; ".join(tail) + ")" if tail else "")
        lines.append(f"[{num[i]}] **{n['title']}**{state} - {text} (#{i})".rstrip())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- briefs + sessions (handoff)

BRIEF_SLOTS = ("purpose", "happened", "open", "next", "ruled_out")


def brief_node(con, scope: str):
    return con.execute("SELECT * FROM node WHERE source_key = ?", (f"brief:{scope}",)).fetchone()


def brief_write(con, scope: str, slots: dict, actor: str = "unknown") -> dict:
    """Fixed slots, REWRITTEN never appended, ~4KB cap. The only thing a session hands to its successor."""
    clean = {k: str(slots.get(k) or "")[:1200] for k in BRIEF_SLOTS}
    body = "\n".join(f"{k}: {v}" for k, v in clean.items())[:4096]
    with tx(con):
        row = brief_node(con, scope)
        if row is None:
            nid = insert_node(con, kind="note", title=f"Brief · {scope}", body=body, state="active",
                              source_key=f"brief:{scope}", attrs={"slots": clean, "scope": scope, "written_by": actor})
        else:
            nid = row["id"]
            update_node(con, nid, body=body, attrs_patch={"slots": clean, "written_by": actor, "rewritten_at": now_iso()})
        log_change(con, actor, nid, f"brief for {scope} rewritten")
    return get_node(con, nid)


def session_open(con, scope: str, opencode_session_id: str | None = None) -> dict:
    r = con.execute("SELECT * FROM session WHERE scope=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (scope,)).fetchone()
    if r:
        if opencode_session_id and r["opencode_session_id"] != opencode_session_id:
            con.execute("UPDATE session SET opencode_session_id=? WHERE id=?", (opencode_session_id, r["id"]))
        return dict(con.execute("SELECT * FROM session WHERE id=?", (r["id"],)).fetchone())
    sid = con.execute("INSERT INTO session (scope, opencode_session_id, started_at) VALUES (?,?,?)",
                      (scope, opencode_session_id, now_iso())).lastrowid
    return dict(con.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone())


def session_handoff(con, scope: str, opencode_session_id: str | None, why: str = "handoff", tokens: int | None = None) -> dict:
    """End the scope's open session (recording the brief it left) and open its successor."""
    with tx(con):
        cur = session_open(con, scope, opencode_session_id)
        b = brief_node(con, scope)
        con.execute("UPDATE session SET ended_at=?, ended_why=?, tokens_at_end=?, brief_node_id=? WHERE id=?",
                    (now_iso(), why, tokens, b["id"] if b else None, cur["id"]))
        nxt = session_open(con, scope, opencode_session_id)
    return {"ended": cur["id"], "opened": nxt["id"]}
