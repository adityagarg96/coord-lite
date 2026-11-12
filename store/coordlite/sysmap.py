"""The system map as portable data: load lanes, tables, invariants, decisions and flows from JSON into the store,
find the flows a change touches, and record a second reader's check of a step.

  python -m coordlite map-load <file.json | directory>      (a directory: lanes.json, tables.json, invariants.json,
                                                             decisions.json, then flows/*.json; any may be missing)

Every node the loader writes carries a stable key (source_key `map:<kind>:<key>`), so loading is idempotent: run it
again and nodes are updated in place, never duplicated. References between items are by key, not by id, so a map
written on one machine loads into any store. A file is one transaction: a bad reference loads nothing from it.

  lanes.json        [{key, title, body, order}]                                    components, top to bottom
  tables.json       [{name, columns: [..], body?, layer?}]                         layer groups tables on the board
  invariants.json   [{key, title, body, enforced_by, breaks_if, evidence: [..], guards: [table name | lane key]}]
  decisions.json    [{key, title, body, decided_by, decided_at, decided_in, under?: domain title}]
  flows/<x>.json    {key, title, domain, trigger, steps: [{lane: <lane key>, text, at: "path:line@sha",
                     reads: [..], writes: [..], pin?: <invariant or decision key>}]}
"""
import json
import re
from pathlib import Path

from . import views
from .db import get_node, now_iso, tx

ENFORCED = views.ENFORCEMENT
AT = re.compile(r"^[^\s:@]+:\d+(-\d+)?@[0-9a-f]{6,40}$")
VERDICTS = ("confirmed", "wrong", "unclear")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _by_key(con, key: str):
    return con.execute("SELECT id FROM node WHERE source_key=?", (key,)).fetchone()


def _upsert(con, key: str, kind: str, title: str, body: str, parent: int | None, attrs: dict, actor: str) -> int:
    """One map node by stable key: created once, then edited in place (history kept by node_edit)."""
    attrs = {**attrs, "map": True}
    row = _by_key(con, key)
    if row is None:
        nid = views.node_create(con, kind, title, body, parent, actor=actor, attrs=attrs)["id"]
        con.execute("UPDATE node SET source_key=? WHERE id=?", (key, nid))
        return nid
    nid = row["id"]
    n = get_node(con, nid)
    fields = {k: v for k, v in (("title", title), ("body", body), ("parent_id", parent)) if v is not None and n.get(k) != v}
    changed = {k: v for k, v in attrs.items() if n["attrs"].get(k) != v}
    if fields or changed:
        views.node_edit(con, nid, fields, actor=actor, attrs=changed or None)
    return nid


def _root(con, actor: str) -> int:
    return _upsert(con, "map:root", "concept", "System map",
                   "The system as a stage: components (lanes), what it keeps (tables), and flows across them. "
                   "Render it with `python -m coordlite storyboard`.", None, {}, actor)


def _group(con, name: str, body: str, actor: str) -> int:
    return _upsert(con, f"map:group:{_slug(name)}", "concept", name, body, _root(con, actor), {}, actor)


def load(con, data: dict, actor: str = "map-load") -> dict:
    """Load any of lanes / tables / invariants / decisions / flows from one dict, in one transaction."""
    out = {k: 0 for k in ("lanes", "tables", "invariants", "decisions", "flows")}
    with tx(con):
        for ln in data.get("lanes", []):
            g = _group(con, "Runtime", "The system's moving parts: one lane each on the storyboard.", actor)
            _upsert(con, f"map:lane:{ln['key']}", "concept", ln["title"], ln.get("body") or ln["title"], g,
                    {"lane": int(ln["order"])}, actor)
            out["lanes"] += 1
        for t in data.get("tables", []):
            g = _group(con, "Data", "What the system keeps: one node per table.", actor)
            a = {"entity": t["name"], "columns": list(t.get("columns", []))}
            if t.get("layer"):
                a["layer"] = t["layer"]
            _upsert(con, f"map:table:{t['name']}", "concept", t["name"], t.get("body") or f"Table {t['name']}.", g, a, actor)
            out["tables"] += 1
        for inv in data.get("invariants", []):
            if inv.get("enforced_by") not in ENFORCED:
                raise ValueError(f"invariant {inv.get('key')}: enforced_by must be one of {ENFORCED}")
            guards = [_by_key(con, f"map:table:{g}") or _by_key(con, f"map:lane:{g}") for g in inv.get("guards", [])]
            if not all(guards):
                raise ValueError(f"invariant {inv['key']}: unknown guard in {inv.get('guards')}")
            parent = guards[0]["id"] if guards else _root(con, actor)
            nid = _upsert(con, f"map:invariant:{inv['key']}", "invariant", inv["title"], inv.get("body") or inv["title"],
                          parent, {k: inv[k] for k in ("enforced_by", "breaks_if", "evidence") if inv.get(k)}, actor)
            have = {r["dst"] for r in con.execute("SELECT dst FROM edge WHERE src=? AND rel='relates_to'", (nid,))}
            for g in guards[1:]:
                if g["id"] not in have:
                    views.edge_add(con, nid, g["id"], "relates_to", "guards", actor=actor)
            out["invariants"] += 1
        for d in data.get("decisions", []):
            parent = _group(con, d["under"], f"Flows about {d['under'].lower()}.", actor) if d.get("under") else _root(con, actor)
            nid = _upsert(con, f"map:decision:{d['key']}", "decision", d["title"], d.get("body") or d["title"], parent,
                          {k: d[k] for k in ("decided_by", "decided_at", "decided_in") if d.get(k)}, actor)
            if get_node(con, nid)["status"] is None:
                views.node_edit(con, nid, {"status": "CONFIRMED"}, actor=actor)
            out["decisions"] += 1
        for f in data.get("flows", []):
            steps = []
            for i, s in enumerate(f["steps"]):
                lane = _by_key(con, f"map:lane:{s['lane']}")
                if lane is None:
                    raise ValueError(f"flow {f['key']} step {i + 1}: unknown lane '{s['lane']}'")
                if not AT.match(s.get("at", "")):
                    raise ValueError(f"flow {f['key']} step {i + 1}: at must be path:line[-line]@sha, got '{s.get('at')}'")
                pin = None
                if s.get("pin"):
                    pin = _by_key(con, f"map:invariant:{s['pin']}") or _by_key(con, f"map:decision:{s['pin']}")
                    if pin is None:
                        raise ValueError(f"flow {f['key']} step {i + 1}: unknown pin '{s['pin']}'")
                    pin = pin["id"]
                step = {"lane": lane["id"], "text": s["text"], "at": s["at"], "reads": list(s.get("reads", [])),
                        "writes": list(s.get("writes", [])), "pin": pin}
                if isinstance(s.get("check"), dict) and s["check"].get("verdict") in VERDICTS:
                    step["check"] = s["check"]               # a verdict saved with the map travels with it
                steps.append(step)
            old = _by_key(con, f"map:flow:{f['key']}")
            if old:                                  # a second reader's check survives a reload of an unchanged step
                claim = lambda x: json.dumps({k: x.get(k) for k in ("lane", "text", "at", "reads", "writes", "pin")},  # noqa: E731
                                             sort_keys=True)  # the whole claim: change any part and the check is void
                prev = {claim(x): x.get("check") for x in get_node(con, old["id"])["attrs"].get("steps", [])}
                for x in steps:
                    if "check" not in x and prev.get(claim(x)):
                        x["check"] = prev[claim(x)]
            dom = _group(con, f["domain"], f"Flows about {f['domain'].lower()}.", actor)
            _upsert(con, f"map:flow:{f['key']}", "concept", f["title"], f"A flow: {f['trigger']}.", dom,
                    {"steps": steps, "trigger": f["trigger"], "traced_by": f.get("traced_by", actor)}, actor)
            out["flows"] += 1
    return out


def load_path(con, path: str, actor: str = "map-load") -> dict:
    p = Path(path).expanduser()
    if p.is_file():
        return load(con, json.loads(p.read_text()), actor)
    total = {}
    for name in ("lanes", "tables", "invariants", "decisions"):
        f = p / f"{name}.json"
        if f.exists():
            for k, v in load(con, {name: json.loads(f.read_text())}, actor).items():
                total[k] = total.get(k, 0) + v
    for f in sorted((p / "flows").glob("*.json")) if (p / "flows").is_dir() else []:
        for k, v in load(con, {"flows": [json.loads(f.read_text())]}, actor).items():
            total[k] = total.get(k, 0) + v
    return total


def save_checks(con, path: str) -> dict:
    """Write the store's second-reader verdicts back into a map directory's flow files, onto steps whose claim in the
    file still matches the store's (a re-traced step keeps no stale verdict). Makes the checks portable."""
    saved, flows = 0, 0
    lanes = {r["id"]: r["source_key"][len("map:lane:"):] for r in con.execute("SELECT id, source_key FROM node WHERE source_key LIKE 'map:lane:%'")}
    pins = {r["id"]: r["source_key"].split(":", 2)[2] for r in con.execute(
        "SELECT id, source_key FROM node WHERE source_key LIKE 'map:invariant:%' OR source_key LIKE 'map:decision:%'")}
    for f in sorted((Path(path).expanduser() / "flows").glob("*.json")):
        data = json.loads(f.read_text())
        row = _by_key(con, f"map:flow:{data['key']}")
        if row is None:
            continue
        stored = get_node(con, row["id"])["attrs"].get("steps", [])
        claim = lambda x: (x.get("lane"), x.get("text"), x.get("at"), tuple(x.get("reads", [])), tuple(x.get("writes", [])), x.get("pin"))  # noqa: E731
        by_claim = {claim({**x, "lane": lanes.get(x["lane"]), "pin": pins.get(x.get("pin"))}): x.get("check") for x in stored}
        changed = False
        for st in data["steps"]:
            c = by_claim.get(claim(st))
            if c and st.get("check") != c:
                st["check"] = c; saved += 1; changed = True
        if changed:
            f.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n"); flows += 1
    return {"checks_saved": saved, "flow_files": flows}


def _at_path(at: str) -> str:
    return at.partition("@")[0].rpartition(":")[0]


def flows_touching(con, paths: list[str]) -> list[dict]:
    """Flows with a step citing one of `paths` (repo-relative; a step that names its checkout first still matches).
    Use after a PR: every flow listed needs its affected steps re-traced at the new head."""
    want = [p.strip("/") for p in paths if p]
    out = []
    for r in con.execute("SELECT id, title, attrs FROM node WHERE json_extract(attrs,'$.steps') IS NOT NULL "
                         "AND state NOT IN ('dropped','parked')"):
        steps = json.loads(r["attrs"]).get("steps") or []
        hit = [i + 1 for i, s in enumerate(steps)
               if any(_at_path(s.get("at", "")) == p or _at_path(s.get("at", "")).endswith("/" + p) for p in want)]
        if hit:
            out.append({"flow_id": r["id"], "title": r["title"], "steps": hit})
    return out


def check_step(con, flow_id: int, step: int, verdict: str, note: str = "", actor: str = "unknown") -> dict:
    """A second reader's verdict on one step's CLAIM (not just its line): confirmed, wrong or unclear. The reader
    must not be the flow's tracer: a check by the author is not a check."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    with tx(con):
        n = get_node(con, flow_id)
        if n is None or not isinstance(n["attrs"].get("steps"), list):
            raise KeyError(flow_id)
        if actor in (n["attrs"].get("traced_by"), n["attrs"].get("created_by")):
            raise ValueError(f"{actor} traced this flow; a second reader must be someone else")
        steps = [dict(s) for s in n["attrs"]["steps"]]
        if not 1 <= step <= len(steps):
            raise ValueError(f"step must be 1..{len(steps)}")
        steps[step - 1]["check"] = {"by": actor, "at": now_iso(), "verdict": verdict, "note": (note or "")[:500]}
        views.node_edit(con, flow_id, {}, actor=actor, attrs={"steps": steps})
    return {"flow_id": flow_id, "step": step, "verdict": verdict}
