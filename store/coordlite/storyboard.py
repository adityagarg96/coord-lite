"""The system storyboard: the system itself as a stage you can play flows across. One self-contained local HTML file.

  python -m coordlite storyboard --out board.html [--repo <checkout>] [--root <id>]

It reads three kinds of concept nodes (no schema change; they are plain concepts with attrs):
  component  attrs.lane = order on the canvas (0 at the top): a session, a server, a store, a plugin
  entity     attrs.entity = table name, attrs.columns = [names]: what the system keeps
  flow       attrs.steps = [{text, lane: <component id>, at: "path:line@sha", reads: ["table.col"], writes: [...],
             pin: <invariant or decision id>}], attrs.trigger = what starts it: one scenario, end to end
Invariants and decisions pin to the step where they bite; an invariant also shows on the entity it guards (its
parent, or a relates_to edge). With --repo every step's `at` is re-checked (file and line exist at that sha, or at
the working tree when no sha), and a broken pointer is flagged on the page: evidence that rots should show.
--repo may also be a parent directory holding several checkouts: a step then names its checkout first,
`<checkout-dir>/<path>:<line>@sha`, and is checked with `git -C <parent>/<checkout-dir> show <sha>:<path>`.
Layout is stable (lanes and tables never move); a flow lights a path across it. Amber is the only colour and marks
the current step. The human's understanding is an overlay, off by default. Node text is rendered with textContent.

At scale: flows are grouped by domain (the flow's parent concept) in a collapsible rail. Tables are grouped by the
schema prefix of a dotted name (`core.reservations` -> core) or else by the table node's parent; the tables the
current flow touches are listed first and open, the rest stay in collapsed groups behind a filter box. Group and
table order is fixed for the whole page, so nothing reorders as you step. More than MAX_LANES lanes is reported.
"""
import functools
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import learn
from .db import now_iso

MAX_LANES = 9          # past this the stage stops being readable: merge components instead
OPEN_ALL_TABLES = 24   # at or below this many tables every group starts open


@functools.lru_cache(maxsize=4096)
def _git_show(repo: str, sha: str, path: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", repo, "show", f"{sha}:{path}"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _resolve(repo: Path, path: str) -> tuple[Path, str]:
    """In a parent of several checkouts a pointer names its checkout first: `<checkout-dir>/<path>`."""
    head, sep, rest = path.partition("/")
    if sep and rest and (repo / head / ".git").exists():
        return repo / head, rest
    return repo, path


def _line_ok(repo: Path, at: str) -> bool | None:
    """True if `path:line[-line]@sha` points at an existing line, False if not, None if it cannot be checked."""
    if not repo or not at or ":" not in at:
        return None
    loc, _, sha = at.partition("@")
    path, _, lines = loc.partition(":")
    try:
        last = max(int(x) for x in lines.split("-") if x.strip())
    except ValueError:
        return None
    checkout, path = _resolve(repo, path)
    if sha:
        text = _git_show(str(checkout), sha, path)
        if not text:
            return False
    else:
        try:
            text = (checkout / path).read_text()
        except OSError:
            return False
    return last <= len(text.splitlines())


def _drift(repo: Path, at: str) -> tuple[str | None, str | None]:
    """Has the code a pinned step cites moved on since it was traced? Compares the cited lines at the step's sha with
    the same file at the checkout's HEAD: ('current', None) when they still read the same at the same lines,
    ('moved', 'path:line') when the same text sits elsewhere in the file, ('changed', None) when it is gone (edited,
    or the file was moved or deleted), and (None, None) when it cannot be told (no sha, no repo, a bad pointer).
    A valid pointer only proves the line existed; drift says whether the step still describes the code."""
    if not repo or not at or "@" not in at or ":" not in at:
        return None, None
    loc, _, sha = at.partition("@")
    path, _, lines = loc.partition(":")
    try:
        nums = [int(x) for x in lines.split("-") if x.strip()]
        first, last = nums[0], nums[-1]
    except (ValueError, IndexError):
        return None, None
    checkout, rel = _resolve(repo, path)
    then = _git_show(str(checkout), sha, rel)
    if then is None:
        return None, None
    cited = [x.strip() for x in then.splitlines()[first - 1:last]]
    if not any(cited):
        return None, None
    now = _git_show(str(checkout), "HEAD", rel)
    if now is None:
        return "changed", None
    head = [x.strip() for x in now.splitlines()]
    if head[first - 1:last] == cited:
        return "current", None
    n = len(cited)
    hits = [i + 1 for i in range(len(head) - n + 1) if head[i:i + n] == cited]
    if hits:
        best = min(hits, key=lambda i: abs(i - first))
        return "moved", f"{path}:{best}" + (f"-{best + n - 1}" if n > 1 else "")
    return "changed", None


def _table_of(ref: str, tables: list[str]) -> str | None:
    """The table a `table` / `table.column` ref names: the longest known table it equals or starts with."""
    hits = [t for t in tables if ref == t or ref.startswith(t + ".")]
    return max(hits, key=len) if hits else None


def board_data(con, repo: str | None = None, root: int | None = None) -> dict:
    rows = {r["id"]: dict(r) for r in con.execute(
        "SELECT id, kind, title, body, parent_id, attrs FROM node WHERE state NOT IN ('dropped','parked') "
        "AND json_extract(attrs,'$.superseded_by') IS NULL")}
    if root is not None:
        keep, frontier = {root}, [root]
        while frontier:
            frontier = [i for i, n in rows.items() if n["parent_id"] in frontier and i not in keep]
            keep.update(frontier)
        rows = {i: rows[i] for i in keep if i in rows}
    for n in rows.values():
        n["attrs"] = json.loads(n["attrs"] or "{}")
    sched, now = learn.scheduler(), datetime.now(timezone.utc)
    cards = {r["node_id"]: dict(r) for r in con.execute("SELECT * FROM learner_card")}

    def mind(i):
        c = cards.get(i)
        if not c:
            return "fog"
        r = learn._retrievability(c, sched, now)
        return "solid" if r >= 0.9 and c["reviews"] >= 2 and c["last_verdict"] == "correct" else "shaky"

    def card(i):
        n = rows.get(i)
        if n is None:
            return None
        a = n["attrs"]
        return {"id": i, "kind": n["kind"], "title": n["title"], "body": (n["body"] or "")[:1200], "mind": mind(i),
                **{k: a[k] for k in ("enforced_by", "breaks_if", "evidence", "decided_by", "decided_at", "decided_in")
                   if a.get(k)}}
    guards = {}
    for e in con.execute("SELECT src, dst FROM edge WHERE rel='relates_to'"):
        guards.setdefault(e["src"], set()).add(e["dst"])
    lanes = sorted((n for n in rows.values() if "lane" in n["attrs"]), key=lambda n: (n["attrs"]["lane"], n["id"]))
    entities = [n for n in rows.values() if n["attrs"].get("entity")]
    pins = [n for n in rows.values() if n["kind"] == "invariant"]
    repo_path = Path(repo).expanduser() if repo else None

    def group(n):
        t = n["attrs"]["entity"]
        if n["attrs"].get("layer"):
            return n["attrs"]["layer"]
        if "." in t:
            return t.split(".", 1)[0]
        p = rows.get(n["parent_id"])
        return p["title"] if p else ""
    entities.sort(key=lambda n: (group(n), n["id"]))
    names = [n["attrs"]["entity"] for n in entities]
    by_name = {n["attrs"]["entity"]: n["id"] for n in entities}
    lane_ids = {n["id"] for n in lanes}
    flow_nodes = [n for n in rows.values() if isinstance(n["attrs"].get("steps"), list)]
    domain_ids = sorted({n["parent_id"] for n in flow_nodes if n["parent_id"] in rows})
    order = {d: i for i, d in enumerate(domain_ids)}
    flows, broken, checked, warnings = [], 0, 0, []
    drifted = {"moved": 0, "changed": 0}
    for n in sorted(flow_nodes, key=lambda n: (order.get(n["parent_id"], len(order)), n["id"])):
        steps, touched, unknown = [], set(), []
        for s in n["attrs"]["steps"]:
            ok = _line_ok(repo_path, s.get("at", ""))
            broken += ok is False
            checked += ok is not None
            drift, now_at = _drift(repo_path, s.get("at", "")) if ok else (None, None)
            if drift in drifted:
                drifted[drift] += 1
            for ref in (s.get("reads") or []) + (s.get("writes") or []):
                t = _table_of(ref, names)
                if t:
                    touched.add(by_name[t])
                elif ref not in unknown:
                    unknown.append(ref)
            if s.get("lane") not in lane_ids:
                warnings.append(f"#{n['id']} '{n['title']}' has a step on no known lane ({s.get('lane')})")
            steps.append({**s, "ok": ok, "drift": drift, "now_at": now_at,
                          "pin": card(s.get("pin")) if s.get("pin") else None})
        flows.append({**card(n["id"]), "trigger": n["attrs"].get("trigger", ""), "steps": steps,
                      "domain": n["parent_id"] if n["parent_id"] in order else None,
                      "tables": [e["id"] for e in entities if e["id"] in touched], "unknown": unknown})
    if len(lanes) > MAX_LANES:
        warnings.insert(0, f"{len(lanes)} lanes: past {MAX_LANES} the stage stops being readable, merge components")
    return {
        "generated": now_iso()[:16].replace("T", " "), "checked": bool(repo_path), "broken": broken, "drifted": drifted,
        "pointers": checked, "steps": sum(len(f["steps"]) for f in flows), "warnings": warnings, "open_all": len(entities) <= OPEN_ALL_TABLES,
        "domains": [{**card(d), "flows": sum(f["domain"] == d for f in flows)} for d in domain_ids],
        "lanes": [{**card(n["id"]), "invariants": [card(p["id"]) for p in pins
                                                    if p["parent_id"] == n["id"] or n["id"] in guards.get(p["id"], ())]}
                  for n in lanes],
        "entities": [{**card(n["id"]), "table": n["attrs"]["entity"], "columns": n["attrs"].get("columns", []),
                      "group": group(n),
                      "invariants": [card(p["id"]) for p in pins
                                     if p["parent_id"] == n["id"] or n["id"] in guards.get(p["id"], ())]}
                     for n in entities],
        "flows": flows,
    }


def board_html(con, repo: str | None = None, root: int | None = None) -> str:
    data = json.dumps(board_data(con, repo, root), ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("/*DATA*/null", data)


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>System storyboard</title>
<style>
:root{--bg:#f7f6f2;--panel:#fff;--ink:#1d1d1b;--muted:#6b6a64;--faint:#b9b5ab;--line:#e2dfd6;--band:#efede6;--hi:#d08a1c;--hi-soft:#f6e3c2}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#16171a;--panel:#1f2125;--ink:#ece9e2;--muted:#9a988f;
 --faint:#55575e;--line:#2e3035;--band:#1c1e22;--hi:#e6a33a;--hi-soft:#4a3a1e}}
:root[data-theme="dark"]{--bg:#16171a;--panel:#1f2125;--ink:#ece9e2;--muted:#9a988f;--faint:#55575e;--line:#2e3035;--band:#1c1e22;--hi:#e6a33a;--hi-soft:#4a3a1e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0}.muted{color:var(--muted)}button{font:inherit;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer}
button:hover{border-color:var(--ink)}label{color:var(--muted);cursor:pointer}
.grid{display:grid;grid-template-columns:240px minmax(0,1fr) 290px;min-height:calc(100vh - 50px)}
.dh{display:flex;justify-content:space-between;gap:8px;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);
 padding:6px 4px;margin:8px 0 4px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none}.dh:hover{color:var(--ink)}
#tq{width:100%;font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink);margin-bottom:6px}
aside .row{font-family:ui-monospace,Menlo,monospace;font-size:12px;padding:2px 4px;cursor:pointer;color:var(--muted);display:flex;justify-content:space-between}
aside .row:hover{color:var(--ink)}aside .x{float:right;color:var(--faint);cursor:pointer;font-size:12px}.unk{font-size:12px;color:var(--muted);margin:-4px 0 10px}
@media (max-width:980px){.grid{grid-template-columns:1fr}}
nav{border-right:1px solid var(--line);padding:12px}nav .f{padding:8px 10px;border-radius:6px;cursor:pointer;margin-bottom:4px}
nav .f:hover{background:var(--band)}nav .f.on{background:var(--panel);outline:1.5px solid var(--ink)}nav .f small{display:block;color:var(--muted)}
.stage{padding:12px 16px;overflow:auto}svg text{fill:var(--ink);font-size:12.5px;dominant-baseline:middle}
.lane-label{font-weight:600}.trig{margin:4px 0 10px;color:var(--muted)}
.detail{margin-top:12px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.detail .k{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-top:10px}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}.chip{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:0 8px;margin:2px 4px 2px 0;font-size:12.5px}
.chip.w{background:var(--hi-soft);border-color:var(--hi)}.pin{border-left:3px solid var(--ink);padding:6px 10px;margin-top:8px;background:var(--band);border-radius:0 6px 6px 0}
.pin p{margin:4px 0;white-space:pre-wrap}.bad{color:var(--hi);font-weight:600}
aside{border-left:1px solid var(--line);padding:12px;overflow:auto}aside .t{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:10px}
aside .t b{display:block}aside .col{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;padding:0 5px;margin:2px 2px 0 0;border-radius:4px;color:var(--faint)}
aside .col.r{color:var(--ink);outline:1px solid var(--ink)}aside .col.w{color:var(--ink);background:var(--hi-soft);outline:1px solid var(--hi)}
aside .t.lit{border-color:var(--ink)}aside .inv{font-size:12px;color:var(--muted);margin-top:4px;cursor:pointer}
.m-fog{opacity:.45}.m-shaky{text-decoration:underline dotted}
</style></head><body>
<header><h1>System storyboard</h1><span class="muted" id="meta"></span>
<label><input type="checkbox" id="mind"> my understanding</label>
<button id="quiz" title="Copies a /quiz command for this flow">Quiz me on this flow</button><span class="muted" id="copied"></span>
<span class="muted">← → step · ↑ ↓ flow · space play</span></header>
<div class="grid"><nav id="flows"></nav>
<div class="stage"><div class="trig" id="trig"></div><svg id="board"></svg><div class="detail" id="detail"></div></div>
<aside><input id="tq" type="search" placeholder="Filter tables or columns" aria-label="Filter tables or columns"><div id="tables"></div></aside></div>
<script>
const D=/*DATA*/null;let fi=0,si=0,showMind=false,timer=null,q="";
const shutD=new Set(),flipG=new Set(),flipT=new Set();  // user toggles: closed domains, groups/tables flipped from their default
const GROUPS=[];for(const e of D.entities){let g=GROUPS[GROUPS.length-1];if(!g||g.name!==e.group)GROUPS.push(g={name:e.group,all:[]});g.all.push(e)}
const $=id=>document.getElementById(id);const el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e};
const S=(t,a,p)=>{const e=document.createElementNS("http://www.w3.org/2000/svg",t);for(const k in a)e.setAttribute(k,a[k]);p&&p.appendChild(e);return e};
const V=k=>getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const laneIx=new Map(D.lanes.map((l,i)=>[l.id,i]));
function mindClass(n){return showMind&&n?" m-"+n.mind:""}
function nav(){const n=$("flows");n.textContent="";if(!D.flows.length){n.append(el("p","muted","No flows yet. A flow is a concept with attrs.steps; the trace-flow skill writes them."));return}
 for(const dm of [...D.domains,{id:null,title:"Other"}]){const idx=D.flows.map((f,i)=>i).filter(i=>D.flows[i].domain===dm.id);if(!idx.length)continue;
  const shut=shutD.has(dm.id);const h=el("div","dh"+(dm.mind?mindClass(dm):""));h.title=dm.body||"";
  h.append(el("span",null,(shut?"▸ ":"▾ ")+dm.title),el("span",null,String(idx.length)));
  h.onclick=()=>{shut?shutD.delete(dm.id):shutD.add(dm.id);nav()};n.append(h);if(shut)continue;
  for(const i of idx){const f=D.flows[i];const d=el("div","f"+(i===fi?" on":"")+mindClass(f),f.title);d.append(el("small",null,f.steps.length+" steps"+(showMind?" · "+f.mind:"")));
   d.onclick=()=>go(i);n.append(d)}}}
function go(i){fi=i;si=0;stop();if(D.flows[fi])shutD.delete(D.flows[fi].domain);render()}
function board(){const svg=$("board");svg.textContent="";const f=D.flows[fi];const laneH=58,left=200,stepW=118;
 const n=f?f.steps.length:0;const W=Math.max(640,left+40+n*stepW),H=D.lanes.length*laneH+20;svg.setAttribute("width",W);svg.setAttribute("height",H);
 D.lanes.forEach((l,i)=>{const y=10+i*laneH;S("rect",{x:0,y,width:W,height:laneH-6,rx:6,fill:V("--band")},svg);
  const t=S("text",{x:12,y:y+(laneH-6)/2,class:"lane-label"+mindClass(l)},svg);t.textContent=l.title.length>25?l.title.slice(0,24)+"…":l.title;S("title",{},t).textContent=l.title;
  t.style.cursor="pointer";t.onclick=()=>detailFor(l)});
 if(!f)return;const pos=f.steps.map((s,i)=>({x:left+30+i*stepW,y:10+(laneIx.has(s.lane)?laneIx.get(s.lane):0)*laneH+(laneH-6)/2}));
 for(let i=1;i<n;i++){const a=pos[i-1],b=pos[i];const done=i<=si;
  S("path",{d:`M${a.x+13},${a.y} C${a.x+stepW/2},${a.y} ${b.x-stepW/2},${b.y} ${b.x-13},${b.y}`,fill:"none",stroke:done?V("--ink"):V("--faint"),"stroke-width":done?1.8:1.2},svg)}
 f.steps.forEach((s,i)=>{const p=pos[i];const g=S("g",{transform:`translate(${p.x},${p.y})`,style:"cursor:pointer"},svg);
  const cur=i===si,past=i<si;S("circle",{r:12,fill:cur?V("--hi"):past?V("--ink"):V("--panel"),stroke:cur?V("--hi"):past?V("--ink"):V("--faint"),"stroke-width":1.6},g);
  const t=S("text",{"text-anchor":"middle",y:1},g);t.textContent=i+1;t.style.fill=cur||past?V("--bg"):V("--muted");t.style.fontWeight="600";
  if(s.pin){const k=s.pin.kind==="invariant";S("rect",{x:-4,y:-27,width:8,height:8,rx:k?0:1.5,transform:k?"rotate(45 0 -23)":"",fill:cur?V("--ink"):V("--faint")},g)}
  if(s.ok===false||s.drift==="changed")S("circle",{r:16,fill:"none",stroke:V("--hi"),"stroke-dasharray":"3 3"},g);
  else if(s.drift==="moved")S("circle",{r:16,fill:"none",stroke:V("--faint"),"stroke-dasharray":"2 3"},g);
  if(s.check)S("text",{x:11,y:-13,"font-size":"10"},g).textContent=s.check.verdict==="confirmed"?"✓":s.check.verdict==="wrong"?"✗":"?";
  g.onclick=()=>{si=i;stop();render()}})}
function chips(parent,label,list,cls){if(!list||!list.length)return;parent.append(el("div","k",label));const d=el("div");list.forEach(c=>d.append(el("span","chip"+(cls?" "+cls:""),c)));parent.append(d)}
function pinCard(parent,p){const box=el("div","pin");box.append(el("b",null,(p.kind==="invariant"?"◆ invariant · ":"■ decision · ")+p.title));
 if(showMind)box.append(el("div","muted","you: "+p.mind));if(p.body)box.append(el("p",null,p.body));
 if(p.enforced_by)box.append(el("p",null,"Enforced by "+p.enforced_by+(p.breaks_if?". Breaks if "+p.breaks_if:"")));
 if(p.decided_by||p.decided_in)box.append(el("p","muted","Decided "+[p.decided_by,p.decided_at,p.decided_in].filter(Boolean).join(" · ")));
 if(p.evidence)box.append(el("p","mono",[].concat(p.evidence).join("  ")));parent.append(box)}
function detail(){const d=$("detail");d.textContent="";const f=D.flows[fi];if(!f){d.append(el("p","muted","Pick a flow."));return}
 const s=f.steps[si];const lane=D.lanes[laneIx.get(s.lane)];d.append(el("b",null,`Step ${si+1} of ${f.steps.length} · ${lane?lane.title:"?"}`));
 d.append(el("p",null,s.text));const at=el("div","mono",s.at||"");if(s.ok===false)at.append(el("span","bad","  pointer broken at this sha"));
 else if(s.drift==="changed")at.append(el("span","bad","  the code here has changed since this was traced: re-trace"));
 else if(s.drift==="moved")at.append(el("span","muted","  same code now at "+s.now_at));d.append(at);
 if(s.check)d.append(el("div",s.check.verdict==="wrong"?"bad":"muted",`second reader (${s.check.by}, ${String(s.check.at||"").slice(0,10)}): ${s.check.verdict}`+(s.check.note?" · "+s.check.note:"")));
 chips(d,"reads",s.reads);chips(d,"writes",s.writes,"w");if(s.pin)pinCard(d,s.pin)}
function detailFor(n){stop();const d=$("detail");d.textContent="";d.append(el("b",null,n.title));if(showMind)d.append(el("div","muted","you: "+n.mind));
 if(n.body)d.append(el("p",null,n.body));(n.invariants||[]).forEach(p=>pinCard(d,p))}
const touch=(t,c,list)=>(list||[]).some(x=>x===t||x===t+"."+c||(c==null&&x.startsWith(t+".")));
function tableCard(e,s,hide){const lit=s&&(touch(e.table,null,s.reads)||touch(e.table,null,s.writes));const box=el("div","t"+(lit?" lit":"")+mindClass(e));
 if(hide){const x=el("span","x","hide");x.onclick=hide;box.append(x)}
 box.append(el("b",null,e.table));for(const c of e.columns){const w=s&&touch(e.table,c,s.writes)||s&&(s.writes||[]).includes(e.table),r=s&&touch(e.table,c,s.reads)||s&&(s.reads||[]).includes(e.table);
  box.append(el("span","col"+(w?" w":r?" r":""),c))}
 (e.invariants||[]).forEach(p=>{const i=el("div","inv","◆ "+p.title);i.onclick=()=>detailFor({title:e.table,body:e.body,invariants:[p],mind:e.mind});box.append(i)});
 box.querySelector("b").style.cursor="pointer";box.querySelector("b").onclick=()=>detailFor(e);return box}
function toggle(set,k){set.has(k)?set.delete(k):set.add(k);tables()}
function tables(){const a=$("tables");a.textContent="";const f=D.flows[fi],s=f?.steps[si];const inFlow=new Set(f?f.tables:[]);
 if(f){a.append(el("div","dh","In this flow · "+inFlow.size));for(const e of D.entities)if(inFlow.has(e.id))a.append(tableCard(e,s));
  if(f.unknown&&f.unknown.length)a.append(el("div","unk","Named here but not in the table list: "+f.unknown.join(", ")))}
 const ql=q.trim().toLowerCase();const match=e=>!ql||e.table.toLowerCase().includes(ql)||e.columns.some(c=>c.toLowerCase().includes(ql));
 a.append(el("div","muted",f?"Everything else the system keeps":"What the system keeps"));
 for(const g of GROUPS){const rest=g.all.filter(e=>!inFlow.has(e.id)),shown=rest.filter(match);if(!rest.length||(ql&&!shown.length))continue;
  const open=!!ql||(D.open_all!==flipG.has(g.name));const h=el("div","dh");
  h.append(el("span",null,(open?"▾ ":"▸ ")+(g.name||"tables")),el("span",null,ql?shown.length+" of "+rest.length:String(rest.length)));
  h.onclick=()=>toggle(flipG,g.name);a.append(h);if(!open)continue;
  for(const e of shown){if(D.open_all!==flipT.has(e.id)){a.append(tableCard(e,s,()=>toggle(flipT,e.id)));continue}
   const r=el("div","row"+mindClass(e));r.append(el("span",null,e.table),el("span",null,(e.invariants.length?"◆ ":"")+e.columns.length));
   r.onclick=()=>toggle(flipT,e.id);a.append(r)}}}
function render(){nav();board();detail();tables();const f=D.flows[fi];$("trig").textContent=f?(f.trigger?"Starts when: "+f.trigger:""):""}
function stop(){if(timer){clearInterval(timer);timer=null}}
$("tq").oninput=e=>{q=e.target.value;tables()};
document.addEventListener("keydown",e=>{const f=D.flows[fi];if(!f||e.target.tagName==="INPUT")return;
 if(e.key==="ArrowRight"){si=Math.min(si+1,f.steps.length-1);stop();render()}else if(e.key==="ArrowLeft"){si=Math.max(si-1,0);stop();render()}
 else if(e.key==="ArrowDown"){go(Math.min(fi+1,D.flows.length-1))}else if(e.key==="ArrowUp"){go(Math.max(fi-1,0))}
 else if(e.key===" "){e.preventDefault();if(timer)stop();else{if(si>=f.steps.length-1)si=0;timer=setInterval(()=>{if(si>=f.steps.length-1)return stop();si++;render()},1600);render()}}});
$("mind").onchange=e=>{showMind=e.target.checked;render()};
$("quiz").onclick=()=>{const f=D.flows[fi];if(!f)return;const ids=[f.id,...f.steps.map(s=>s.pin&&s.pin.id).filter(Boolean)];
 const cmd="/quiz on "+ids.map(i=>"#"+i).join(" ");(navigator.clipboard?navigator.clipboard.writeText(cmd):Promise.reject()).then(()=>$("copied").textContent="copied: "+cmd,()=>$("copied").textContent=cmd)};
const ptr=!D.checked?"pointers not checked":D.broken?`${D.broken} of ${D.pointers} pointers broken`:
 D.pointers===D.steps?`every pointer checked (${D.pointers}, 0 broken)`:`${D.pointers} of ${D.steps} pointers checked, 0 broken`;
const dr=D.drifted||{};const drift=(dr.changed?` · ${dr.changed} steps changed since traced`:"")+(dr.moved?` · ${dr.moved} moved`:"");
const nck=D.flows.reduce((a,f)=>a+f.steps.filter(s=>s.check).length,0),nbad=D.flows.reduce((a,f)=>a+f.steps.filter(s=>s.check&&s.check.verdict==="wrong").length,0);
const chk=nck?` · ${nck} steps second-read${nbad?`, ${nbad} wrong`:""}`:"";
$("meta").textContent=`${D.flows.length} flows in ${D.domains.length} domains · ${D.lanes.length} components · ${D.entities.length} tables · ${ptr}${drift}${chk} · ${D.generated} UTC`;
if(D.warnings.length){const w=el("span","bad",D.warnings.length+(D.warnings.length>1?" warnings: ":" warning: ")+D.warnings.join(" · "));$("meta").after(w)}
render();
</script></body></html>
"""
