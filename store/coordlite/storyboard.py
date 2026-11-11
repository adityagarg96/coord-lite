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
Layout is stable (lanes and tables never move); a flow lights a path across it. Amber is the only colour and marks
the current step. The human's understanding is an overlay, off by default. Node text is rendered with textContent.
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import learn
from .db import now_iso


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
    try:
        if sha:
            text = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{path}"], capture_output=True, text=True,
                                  timeout=10).stdout
            if not text:
                return False
        else:
            text = (repo / path).read_text()
    except (OSError, subprocess.SubprocessError):
        return False
    return last <= len(text.splitlines())


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
    flows, broken = [], 0
    for n in sorted((n for n in rows.values() if isinstance(n["attrs"].get("steps"), list)), key=lambda n: n["id"]):
        steps = []
        for s in n["attrs"]["steps"]:
            ok = _line_ok(repo_path, s.get("at", ""))
            broken += ok is False
            steps.append({**s, "ok": ok, "pin": card(s.get("pin")) if s.get("pin") else None})
        flows.append({**card(n["id"]), "trigger": n["attrs"].get("trigger", ""), "steps": steps})
    return {
        "generated": now_iso()[:16].replace("T", " "), "checked": bool(repo_path), "broken": broken,
        "lanes": [{**card(n["id"]), "invariants": [card(p["id"]) for p in pins
                                                    if p["parent_id"] == n["id"] or n["id"] in guards.get(p["id"], ())]}
                  for n in lanes],
        "entities": [{**card(n["id"]), "table": n["attrs"]["entity"], "columns": n["attrs"].get("columns", []),
                      "invariants": [card(p["id"]) for p in pins
                                     if p["parent_id"] == n["id"] or n["id"] in guards.get(p["id"], ())]}
                     for n in sorted(entities, key=lambda n: n["id"])],
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
.grid{display:grid;grid-template-columns:230px minmax(0,1fr) 260px;min-height:calc(100vh - 50px)}
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
<aside id="tables"></aside></div>
<script>
const D=/*DATA*/null;let fi=0,si=0,showMind=false,timer=null;
const $=id=>document.getElementById(id);const el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e};
const S=(t,a,p)=>{const e=document.createElementNS("http://www.w3.org/2000/svg",t);for(const k in a)e.setAttribute(k,a[k]);p&&p.appendChild(e);return e};
const V=k=>getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const laneIx=new Map(D.lanes.map((l,i)=>[l.id,i]));
function mindClass(n){return showMind&&n?" m-"+n.mind:""}
function nav(){const n=$("flows");n.textContent="";if(!D.flows.length){n.append(el("p","muted","No flows yet. A flow is a concept with attrs.steps; the trace-flow skill writes them."));return}
 D.flows.forEach((f,i)=>{const d=el("div","f"+(i===fi?" on":"")+mindClass(f),f.title);d.append(el("small",null,f.steps.length+" steps"+(showMind?" · "+f.mind:"")));
  d.onclick=()=>{fi=i;si=0;render()};n.append(d)})}
function board(){const svg=$("board");svg.textContent="";const f=D.flows[fi];const laneH=58,left=170,stepW=118;
 const n=f?f.steps.length:0;const W=Math.max(640,left+40+n*stepW),H=D.lanes.length*laneH+20;svg.setAttribute("width",W);svg.setAttribute("height",H);
 D.lanes.forEach((l,i)=>{const y=10+i*laneH;S("rect",{x:0,y,width:W,height:laneH-6,rx:6,fill:V("--band")},svg);
  const t=S("text",{x:12,y:y+(laneH-6)/2,class:"lane-label"+mindClass(l)},svg);t.textContent=l.title.length>20?l.title.slice(0,19)+"…":l.title;
  t.style.cursor="pointer";t.onclick=()=>detailFor(l)});
 if(!f)return;const pos=f.steps.map((s,i)=>({x:left+30+i*stepW,y:10+(laneIx.has(s.lane)?laneIx.get(s.lane):0)*laneH+(laneH-6)/2}));
 for(let i=1;i<n;i++){const a=pos[i-1],b=pos[i];const done=i<=si;
  S("path",{d:`M${a.x+13},${a.y} C${a.x+stepW/2},${a.y} ${b.x-stepW/2},${b.y} ${b.x-13},${b.y}`,fill:"none",stroke:done?V("--ink"):V("--faint"),"stroke-width":done?1.8:1.2},svg)}
 f.steps.forEach((s,i)=>{const p=pos[i];const g=S("g",{transform:`translate(${p.x},${p.y})`,style:"cursor:pointer"},svg);
  const cur=i===si,past=i<si;S("circle",{r:12,fill:cur?V("--hi"):past?V("--ink"):V("--panel"),stroke:cur?V("--hi"):past?V("--ink"):V("--faint"),"stroke-width":1.6},g);
  const t=S("text",{"text-anchor":"middle",y:1},g);t.textContent=i+1;t.style.fill=cur||past?V("--bg"):V("--muted");t.style.fontWeight="600";
  if(s.pin){const k=s.pin.kind==="invariant";S("rect",{x:-4,y:-27,width:8,height:8,rx:k?0:1.5,transform:k?"rotate(45 0 -23)":"",fill:cur?V("--ink"):V("--faint")},g)}
  if(s.ok===false)S("circle",{r:16,fill:"none",stroke:V("--hi"),"stroke-dasharray":"3 3"},g);
  g.onclick=()=>{si=i;stop();render()}})}
function chips(parent,label,list,cls){if(!list||!list.length)return;parent.append(el("div","k",label));const d=el("div");list.forEach(c=>d.append(el("span","chip"+(cls?" "+cls:""),c)));parent.append(d)}
function pinCard(parent,p){const box=el("div","pin");box.append(el("b",null,(p.kind==="invariant"?"◆ invariant · ":"■ decision · ")+p.title));
 if(showMind)box.append(el("div","muted","you: "+p.mind));if(p.body)box.append(el("p",null,p.body));
 if(p.enforced_by)box.append(el("p",null,"Enforced by "+p.enforced_by+(p.breaks_if?". Breaks if "+p.breaks_if:"")));
 if(p.decided_by||p.decided_in)box.append(el("p","muted","Decided "+[p.decided_by,p.decided_at,p.decided_in].filter(Boolean).join(" · ")));
 if(p.evidence)box.append(el("p","mono",[].concat(p.evidence).join("  ")));parent.append(box)}
function detail(){const d=$("detail");d.textContent="";const f=D.flows[fi];if(!f){d.append(el("p","muted","Pick a flow."));return}
 const s=f.steps[si];const lane=D.lanes[laneIx.get(s.lane)];d.append(el("b",null,`Step ${si+1} of ${f.steps.length} · ${lane?lane.title:"?"}`));
 d.append(el("p",null,s.text));const at=el("div","mono",s.at||"");if(s.ok===false)at.append(el("span","bad","  pointer broken at this sha"));d.append(at);
 chips(d,"reads",s.reads);chips(d,"writes",s.writes,"w");if(s.pin)pinCard(d,s.pin)}
function detailFor(n){stop();const d=$("detail");d.textContent="";d.append(el("b",null,n.title));if(showMind)d.append(el("div","muted","you: "+n.mind));
 if(n.body)d.append(el("p",null,n.body));(n.invariants||[]).forEach(p=>pinCard(d,p))}
function tables(){const a=$("tables");a.textContent="";a.append(el("div","muted","What the system keeps"));const s=D.flows[fi]?.steps[si];
 const touch=(t,c,list)=>(list||[]).some(x=>x===t||x===t+"."+c||(c==null&&x.startsWith(t+".")));
 for(const e of D.entities){const lit=s&&(touch(e.table,null,s.reads)||touch(e.table,null,s.writes));const box=el("div","t"+(lit?" lit":"")+mindClass(e));
  box.append(el("b",null,e.table));for(const c of e.columns){const w=s&&touch(e.table,c,s.writes)||s&&(s.writes||[]).includes(e.table),r=s&&touch(e.table,c,s.reads)||s&&(s.reads||[]).includes(e.table);
   box.append(el("span","col"+(w?" w":r?" r":""),c))}
  (e.invariants||[]).forEach(p=>{const i=el("div","inv","◆ "+p.title);i.onclick=()=>detailFor({title:e.table,body:e.body,invariants:[p],mind:e.mind});box.append(i)});
  box.querySelector("b").style.cursor="pointer";box.querySelector("b").onclick=()=>detailFor(e);a.append(box)}}
function render(){nav();board();detail();tables();const f=D.flows[fi];$("trig").textContent=f?(f.trigger?"Starts when: "+f.trigger:""):""}
function stop(){if(timer){clearInterval(timer);timer=null}}
document.addEventListener("keydown",e=>{const f=D.flows[fi];if(!f)return;
 if(e.key==="ArrowRight"){si=Math.min(si+1,f.steps.length-1);stop();render()}else if(e.key==="ArrowLeft"){si=Math.max(si-1,0);stop();render()}
 else if(e.key==="ArrowDown"){fi=Math.min(fi+1,D.flows.length-1);si=0;render()}else if(e.key==="ArrowUp"){fi=Math.max(fi-1,0);si=0;render()}
 else if(e.key===" "){e.preventDefault();if(timer)stop();else{if(si>=f.steps.length-1)si=0;timer=setInterval(()=>{if(si>=f.steps.length-1)return stop();si++;render()},1600);render()}}});
$("mind").onchange=e=>{showMind=e.target.checked;render()};
$("quiz").onclick=()=>{const f=D.flows[fi];if(!f)return;const ids=[f.id,...f.steps.map(s=>s.pin&&s.pin.id).filter(Boolean)];
 const cmd="/quiz on "+ids.map(i=>"#"+i).join(" ");(navigator.clipboard?navigator.clipboard.writeText(cmd):Promise.reject()).then(()=>$("copied").textContent="copied: "+cmd,()=>$("copied").textContent=cmd)};
$("meta").textContent=`${D.flows.length} flows · ${D.lanes.length} components · ${D.entities.length} tables`+(D.checked?(D.broken?` · ${D.broken} broken pointers`:" · every pointer checked"):" · pointers not checked")+` · ${D.generated} UTC`;
render();
</script></body></html>
"""
