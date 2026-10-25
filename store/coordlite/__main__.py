"""CLI: `python -m coordlite <command>`.

  init                                   create the store (idempotent) and print its path
  serve                                  run the `store` MCP server over stdio (what opencode.json launches)
  boot    [--scope master] [--since ISO] print the boot packet a fresh session of that scope starts from
  handoff --scope master --session ID [--tokens N] [--since ISO]
                                         record the handoff (end this session row, open the next), then print boot
"""
import argparse
import json
import sys

from . import views
from .db import DB_PATH, connect

BOOT_CAP = 40_000   # characters of plan text in a boot packet; the session pulls the rest with plan_render


def boot_packet(con, scope: str = "master", since: str | None = None) -> str:
    parts = [f"# HANDOFF: you are a fresh session continuing scope '{scope}'",
             "Your previous conversation was replaced by this packet. Everything durable is in the store; "
             "trust the store over anything you think you remember."]
    b = views.brief_node(con, scope)
    if b is None:
        parts.append("## Brief from your predecessor\n(none: you are the first session of this scope)")
    else:
        stale = since and b["updated_at"] < since
        parts.append(f"## Brief from your predecessor (written {b['updated_at']})"
                     + ("\nWARNING: this brief predates the handoff request, so the last stretch of work is not in it. "
                        "Read `changes_since` below and the plan before acting." if stale else "")
                     + "\n" + b["body"])
    roots = con.execute("SELECT id FROM node WHERE parent_id IS NULL AND state NOT IN ('done','dropped') "
                        "AND (source_key IS NULL OR source_key NOT LIKE 'brief:%') ORDER BY id").fetchall()
    plan, used = [], 0
    for r in roots:
        doc = views.plan_render(con, r["id"], depth=3) or ""
        if used + len(doc) > BOOT_CAP:
            plan.append(f"(… #{r['id']} and later roots omitted for size: plan_render them)")
            break
        plan.append(doc)
        used += len(doc)
    parts.append("## The plan as it stands (plan_render, depth 3)\n" + ("\n\n".join(plan) or "(the store is empty)"))
    findings = views.store_lint(con)
    if findings:
        counts = {}
        for f in findings:
            counts[f["check"]] = counts.get(f["check"], 0) + 1
        parts.append(f"## Store hygiene ({len(findings)} findings: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                     + "). Clear these before adding new structure.\n" + "\n".join(f"- {f['message']}" for f in findings[:40]))
    recent = views.changes_since(con, None, limit=30)["changes"]
    if recent:
        parts.append("## Last 30 writes (changes_since)\n" + "\n".join(f"- {c['at'][:19]} {c['actor']}: {c['what']}" for c in recent))
    js = views.journal_stats(con)
    if js["pending"]:
        parts.append(f"## Journal\n{js['pending']} uncompiled journal entries (oldest {js['oldest_pending'][:10]}). "
                     "Compile them with the knowledge-compile skill (in a background subagent) when you have a quiet moment.")
    parts.append("## Knowledge index\n" + views.knowledge_index(con, max_chars=12_000))
    parts.append("## Continue\nTell the user in one line that you resumed from a handoff and what you will do next, "
                 "then carry on with the brief's `next`. Use `peers list` to see the live sessions.")
    return "\n\n".join(parts)


def main(argv=None):
    p = argparse.ArgumentParser(prog="coordlite")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("serve")
    b = sub.add_parser("boot")
    b.add_argument("--scope", default="master")
    b.add_argument("--since")
    h = sub.add_parser("handoff")
    h.add_argument("--scope", default="master")
    h.add_argument("--session", required=True)
    h.add_argument("--tokens", type=int)
    h.add_argument("--since")
    h.add_argument("--why", default="handoff")
    j = sub.add_parser("journal", help="append a journal entry; body on stdin")
    j.add_argument("--actor", required=True)
    j.add_argument("--session")
    j.add_argument("--agent")
    j.add_argument("--kind", default="flush", choices=("flush", "precompact", "manual"))
    j.add_argument("--tokens", type=int)
    sub.add_parser("index")
    sub.add_parser("journal-stats")
    x = sub.add_parser("export")
    x.add_argument("--root", type=int, required=True)
    x.add_argument("--out", help="file to write (default: stdout)")
    a = p.parse_args(argv)

    if a.cmd == "serve":
        from .server import main as serve
        return serve()
    con = connect(DB_PATH)
    if a.cmd == "init":
        print(DB_PATH)
    elif a.cmd == "boot":
        sys.stdout.write(boot_packet(con, a.scope, a.since))
    elif a.cmd == "handoff":
        views.session_handoff(con, a.scope, a.session, a.why, a.tokens)
        sys.stdout.write(boot_packet(con, a.scope, a.since))
    elif a.cmd == "journal":
        print(json.dumps(views.journal_write(con, a.actor, sys.stdin.read(), a.session, a.agent, a.kind, a.tokens)))
    elif a.cmd == "index":
        sys.stdout.write(views.knowledge_index(con))
    elif a.cmd == "journal-stats":
        print(json.dumps(views.journal_stats(con)))
    elif a.cmd == "export":
        doc = views.map_export(con, a.root)
        if doc is None:
            sys.exit(f"no node #{a.root}")
        if a.out:
            with open(a.out, "w") as f:
                f.write(doc)
        else:
            sys.stdout.write(doc)


if __name__ == "__main__":
    main()
