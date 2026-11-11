"""The fog-of-war map: one self-contained HTML file showing what the store knows and what the human understands of it.

  python -m coordlite learnmap --out ~/coord-lite-data/map.html     (then open it in a browser)

Local only: no network, no external scripts or fonts, so it works with a work store that must not leave the machine.
Every tree in the store is drawn left to right; each knowledge node carries the human's state (solid, shaky, due,
frontier, fog). Unexplored regions stay collapsed as "N unexplored" until the human reaches them, or lifts the fog.
Node text is data: the page renders it with textContent, never as HTML.
"""
import json
from datetime import datetime, timezone

from . import learn
from .db import now_iso, parse_iso

SHOWN = ("concept", "invariant", "decision")      # what the map is about; their ancestors come along for structure


def map_data(con, include_imported: bool = False) -> dict:
    sched, now = learn.scheduler(), datetime.now(timezone.utc)
    rows = learn._candidates(con, include_imported)
    cards = {r["node_id"]: dict(r) for r in con.execute("SELECT * FROM learner_card")}
    frontier = set(learn._frontier(con, rows, cards))
    keep = set()
    for i, n in rows.items():
        if n["kind"] in SHOWN:
            j, seen = i, set()
            while j is not None and j in rows and j not in seen:
                seen.add(j); keep.add(j); j = rows[j]["parent_id"]
    bodies = {r["id"]: (r["body"] or "") for r in con.execute(
        f"SELECT id, body FROM node WHERE id IN ({','.join(str(i) for i in keep) or '0'})")}
    nodes = []
    for i in sorted(keep):
        n, a, c = rows[i], json.loads(rows[i]["attrs"] or "{}"), cards.get(i)
        if c:
            r = learn._retrievability(c, sched, now)
            solid = r >= 0.9 and c["reviews"] >= 2 and c["last_verdict"] == "correct"
            state = "solid" if solid else "shaky"
            extra = {"retrievability": r, "level": c["level"], "reviews": c["reviews"], "lapses": c["lapses"],
                     "last": c["last_verdict"], "due": c["due"][:10], "due_now": parse_iso(c["due"]) <= now}
        else:
            container = n["kind"] not in learn.QUIZ_KINDS or n["parent_id"] is None      # roots are never quizzed
            state = "frontier" if i in frontier else ("container" if container else "fog")
            extra = {}
        facts = {k: a[k] for k in ("enforced_by", "breaks_if", "evidence", "decided_by", "decided_at", "decided_in",
                                   "reference") if a.get(k)}
        nodes.append({"id": i, "parent": n["parent_id"] if n["parent_id"] in keep else None, "kind": n["kind"],
                      "title": n["title"], "body": bodies.get(i, "")[:1500], "state": state, "facts": facts, **extra})
    links = [dict(r) for r in con.execute("SELECT src, dst, rel, note FROM edge WHERE rel != 'supersedes'")
             if r["src"] in keep and r["dst"] in keep]
    p = learn.profile(con, limit=5)
    return {"generated": now_iso()[:16].replace("T", " "), "nodes": nodes, "links": links,
            "counts": {"solid": p["solid"], "shaky": p["shaky"], "frontier": len(frontier & keep),
                       "fog": sum(1 for x in nodes if x["state"] == "fog"),
                       "due": sum(1 for x in nodes if x.get("due_now"))}}


def map_html(con, include_imported: bool = False) -> str:
    data = json.dumps(map_data(con, include_imported), ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("/*DATA*/null", data)


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Understanding map</title>
<style>
:root{--bg:#f7f6f2;--panel:#fff;--ink:#1d1d1b;--muted:#6b6a64;--line:#d9d6cc;--fog:#c9c6bd;--fogdot:#c4c0b6;
 --solid:#1d1d1b;--shaky:#9b978d;--front:#1d1d1b;--due:#d08a1c;--edge:#8f8b80}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#16171a;--panel:#1f2125;--ink:#e9e7e1;
 --muted:#9a988f;--line:#34363c;--fog:#3a3c42;--fogdot:#4a4c52;--solid:#ece9e2;--shaky:#8a877f;--front:#ece9e2;--due:#e6a33a;--edge:#77756e}}
:root[data-theme="dark"]{--bg:#16171a;--panel:#1f2125;--ink:#e9e7e1;--muted:#9a988f;--line:#34363c;--fog:#3a3c42;
 --fogdot:#4a4c52;--solid:#ece9e2;--shaky:#8a877f;--front:#ece9e2;--due:#e6a33a;--edge:#77756e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center}
h1{font-size:16px;margin:0 8px 0 0}.chip{display:inline-flex;align-items:center;gap:6px;color:var(--muted)}.chip b{color:var(--ink)}
.sw{width:11px;height:11px;border-radius:50%;display:inline-block;border:1.5px solid var(--muted)}label{color:var(--muted);cursor:pointer;user-select:none}
main{display:flex;min-height:calc(100vh - 60px)}#wrap{flex:1;overflow:auto;padding:12px 16px}
aside{width:340px;max-width:100%;border-left:1px solid var(--line);background:var(--panel);padding:16px;overflow:auto;position:sticky;top:60px;height:calc(100vh - 60px)}
@media (max-width:760px){main{flex-direction:column}aside{width:auto;position:static;height:auto;border-left:0;border-top:1px solid var(--line)}}
svg text{font-size:12.5px;fill:var(--ink);dominant-baseline:middle}svg .muted{fill:var(--muted)}.n{cursor:pointer}
.n:hover text{text-decoration:underline}.tree{stroke:var(--line);fill:none;stroke-width:1.2}.x{fill:none;stroke:var(--edge);stroke-width:1.2;opacity:.35;stroke-dasharray:4 3}
.x.hi{opacity:1;stroke-width:2;stroke:var(--ink)}.sel{stroke:var(--ink)!important;stroke-width:2.5!important}
aside h2{font-size:15px;margin:0 0 6px}aside .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-top:14px}
aside p{margin:6px 0;white-space:pre-wrap}.pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:12px;color:#fff}
aside a{color:var(--front);cursor:pointer;text-decoration:underline}
</style></head><body>
<header><h1>Understanding map</h1><span id="counts"></span>
<label><input type="checkbox" id="lift"> lift the fog</label><span class="chip" id="gen"></span></header>
<main><div id="wrap"><svg id="map"></svg></div><aside id="side"><h2>Click a node</h2>
<p style="color:var(--muted)">Filled dark = solid. Filled grey = shaky. Hollow outline = your frontier, learn next. Faint dot or "▸ N unexplored" = fog.
Amber is the only colour: an amber ring means due today. Circles are concepts, diamonds invariants, squares decisions.
Dashed lines are links across trees, quiet until you select a node.</p></aside></main>
<script>
const D=/*DATA*/null;
const byId=new Map(D.nodes.map(n=>[n.id,{...n,kids:[]}]));const roots=[];
for(const n of byId.values()){if(n.parent!=null&&byId.has(n.parent))byId.get(n.parent).kids.push(n);else roots.push(n)}
roots.sort((a,b)=>(a.kind!=="concept")-(b.kind!=="concept")||a.id-b.id);
const C=k=>getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const fill={solid:"--solid",shaky:"--shaky",frontier:"--bg",fog:"--fogdot",container:"--panel"};
const stroke={solid:"--solid",shaky:"--shaky",frontier:"--front",fog:"--fogdot",container:"--muted"};
let lifted=false,selected=null;const open=new Set();
const unexplored=n=>n.kids.every(k=>k.state==="fog"&&unexplored(k));
for(const n of byId.values())if(!(n.state==="frontier"||n.state==="fog")||!unexplored(n))open.add(n.id);
const countFog=n=>n.kids.reduce((s,k)=>s+1+countFog(k),0);
function txt(el,s){el.textContent=s;return el}
function sv(tag,attrs,parent){const e=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const k in attrs)e.setAttribute(k,attrs[k]);parent&&parent.appendChild(e);return e}
function draw(){
 const svg=document.getElementById("map");svg.textContent="";const pos=new Map();const rowH=26,colW=230;let y=16,maxX=0;
 function place(n,d){const x=16+d*colW;maxX=Math.max(maxX,x);const show=lifted||open.has(n.id);const kids=show?n.kids:[];
  if(!kids.length){pos.set(n.id,{x,y,stub:!show&&n.kids.length?countFog(n):0});y+=rowH;return}
  const ys=kids.map(k=>{place(k,d+1);return pos.get(k.id).y});pos.set(n.id,{x,y:(ys[0]+ys[ys.length-1])/2,stub:0})}
 for(const r of roots){place(r,0);y+=rowH*0.8}
 svg.setAttribute("width",maxX+colW+40);svg.setAttribute("height",y+10);
 const g1=sv("g",{},svg),g2=sv("g",{},svg),g3=sv("g",{},svg);
 for(const n of byId.values()){if(n.parent==null||!pos.has(n.id)||!pos.has(n.parent))continue;const a=pos.get(n.parent),b=pos.get(n.id);
  sv("path",{class:"tree",d:`M${a.x+8},${a.y} C${a.x+colW/2},${a.y} ${b.x-colW/2},${b.y} ${b.x-8},${b.y}`},g1)}
 for(const l of D.links){if(!pos.has(l.src)||!pos.has(l.dst))continue;const a=pos.get(l.src),b=pos.get(l.dst);
  const hi=selected&&(l.src===selected||l.dst===selected);const bend=Math.max(40,Math.abs(a.y-b.y)/3);
  sv("path",{class:"x"+(hi?" hi":""),d:`M${a.x},${a.y} C${a.x+bend},${a.y-bend} ${b.x+bend},${b.y+bend} ${b.x},${b.y}`},g2)}
 for(const n of byId.values()){const p=pos.get(n.id);if(!p)continue;const g=sv("g",{class:"n",transform:`translate(${p.x},${p.y})`},g3);
  const st=n.state,f=C(fill[st]),s=C(stroke[st]),sel=selected===n.id?" sel":"";let shape;
  if(n.kind==="invariant")shape=sv("rect",{x:-6,y:-6,width:12,height:12,transform:"rotate(45)",fill:f,stroke:s,"stroke-width":2,class:sel},g);
  else if(n.kind==="decision")shape=sv("rect",{x:-6,y:-6,width:12,height:12,rx:2,fill:f,stroke:s,"stroke-width":2,class:sel},g);
  else shape=sv("circle",{r:n.parent==null?8:6.5,fill:f,stroke:s,"stroke-width":2,class:sel},g);
  if(n.due_now)sv("circle",{r:11,fill:"none",stroke:C("--due"),"stroke-width":1.6},g);
  const fogged=st==="fog"&&!lifted;
  const t=txt(sv("text",{x:14,class:fogged?"muted":""},g),fogged?"?":(n.title.length>30?n.title.slice(0,29)+"…":n.title));
  if(n.parent==null)t.setAttribute("font-weight","600");
  if(p.stub)txt(sv("text",{x:14+Math.min(n.title.length,30)*7+8,class:"muted"},g),`▸ ${p.stub} unexplored`);
  g.addEventListener("click",()=>{if(n.kids.length&&!lifted){open.has(n.id)&&n.state!=="container"&&n.parent!=null&&selected===n.id?open.delete(n.id):open.add(n.id)}selected=n.id;draw();side(n)})}
}
function row(parent,k,v){if(v==null||v==="")return;txt(document.createElement("div"),k).className="k";parent.appendChild(parent.lastChild);
 const p=document.createElement("p");p.textContent=Array.isArray(v)?v.join("\n"):String(v);parent.appendChild(p)}
function side(n){const a=document.getElementById("side");a.textContent="";const fogged=n.state==="fog"&&!lifted;
 txt(a.appendChild(document.createElement("h2")),fogged?"Unexplored":n.title);
 const pill=a.appendChild(document.createElement("span"));pill.className="pill";
 const label={solid:"solid",shaky:n.due_now?"shaky · due today":"shaky",frontier:"frontier: learn next",fog:"fog",container:"container"}[n.state];
 pill.textContent=n.kind+" · "+label;pill.style.background=C(stroke[n.state]);
 if(fogged){const p=a.appendChild(document.createElement("p"));p.textContent="You have not reached this yet. Understand its parent first, or lift the fog to peek.";return}
 if(n.reviews){row(a,"Your memory",`level ${n.level} of 3 · recall ${(n.retrievability*100).toFixed(0)}% · ${n.reviews} reviews, ${n.lapses} misses · last ${n.last} · next due ${n.due}`)}
 row(a,"What the store says",n.body);
 const f=n.facts||{};row(a,"Enforced by",f.enforced_by);row(a,"Breaks if",f.breaks_if);row(a,"Evidence",f.evidence);
 if(f.decided_by||f.decided_in)row(a,"Decided",[f.decided_by,f.decided_at,f.decided_in].filter(Boolean).join(" · "));
 row(a,"Reference",f.reference);
 const rel=D.links.filter(l=>l.src===n.id||l.dst===n.id);if(rel.length){txt(a.appendChild(document.createElement("div")),"Linked").className="k";
  for(const l of rel){const o=byId.get(l.src===n.id?l.dst:l.src);const p=a.appendChild(document.createElement("p"));
   p.append(l.src===n.id?`${l.rel.replace("_"," ")} → `:`← ${l.rel.replace("_"," ")} `);const lk=txt(p.appendChild(document.createElement("a")),o.title);
   lk.onclick=()=>{selected=o.id;let q=o;while(q.parent!=null){open.add(q.parent);q=byId.get(q.parent)}draw();side(o)};if(l.note)p.append(` (${l.note})`)}}}
const c=D.counts;const ce=document.getElementById("counts");
for(const [k,v,col] of [["solid",c.solid,"--solid"],["shaky",c.shaky,"--shaky"],["due today",c.due,"--due"],["frontier",c.frontier,"--bg"],["fog",c.fog,"--fogdot"]]){
 const s=ce.appendChild(document.createElement("span"));s.className="chip";s.style.marginRight="14px";const d=s.appendChild(document.createElement("span"));
 d.className="sw";d.style.background=C(col);s.append(" ");txt(s.appendChild(document.createElement("b")),String(v));s.append(" "+k)}
txt(document.getElementById("gen"),"generated "+D.generated+" UTC");
document.getElementById("lift").onchange=e=>{lifted=e.target.checked;draw()};draw();
</script></body></html>
"""
