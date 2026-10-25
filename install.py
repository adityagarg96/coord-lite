#!/usr/bin/env python3
"""Generate config/opencode.json for THIS checkout: absolute paths, agents inlined from agents/*.md.

Nothing global is touched. Run OpenCode with the generated directory layered on top of your own config:

    OPENCODE_CONFIG_DIR=<repo>/config opencode

Your own ~/.config/opencode and each repo's AGENTS.md still load; this adds the store, the two plugins,
the master/worker agents (each carrying instructions/coordination.md). Re-run after editing agents/ or moving the repo.

    python3 install.py [--threshold 600000] [--db ~/coord-lite-data/store.db] [--scope coord-lite] [--lessons core|full|none]
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def agent(md: Path, shared: str) -> dict:
    text = md.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    front, body = (m.group(1), m.group(2)) if m else ("", text)
    meta = dict(re.findall(r"^(\w+):\s*\"?(.*?)\"?\s*$", front, re.M))
    return {"description": meta.get("description", md.stem), "mode": meta.get("mode", "primary"), "system": body.strip() + "\n\n" + shared}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--threshold", type=int, default=600_000, help="master's handoff context size, ~60-70%% of the window")
    p.add_argument("--db", default=str(Path.home() / "coord-lite-data" / "store.db"))
    p.add_argument("--scope", default="coord-lite", help="peers scope: only sessions with the same scope see each other")
    p.add_argument("--timezone", default="America/Los_Angeles")
    p.add_argument("--lessons", choices=("core", "full", "none"), default="core",
                   help="working rules inlined into both agents: core = lessons/standing-rules.md (~3k tokens), "
                        "full = also lessons/agent-rules.md (~7k more), none = neither")
    a = p.parse_args()

    lessons = {"none": [], "core": ["standing-rules.md"], "full": ["standing-rules.md", "agent-rules.md"]}[a.lessons]
    parts = [ROOT / "instructions" / "coordination.md", *(ROOT / "lessons" / f for f in lessons)]
    shared = "\n\n".join(f.read_text().strip() for f in parts)
    store = ["uv", "run", "--project", str(ROOT / "store"), "python", "-m", "coordlite"]
    env = {"COORD_DB": str(Path(a.db).expanduser()), "COORD_TIMEZONE": a.timezone}
    cfg = {
        "update": "disable",
        # the shared laws are inlined into each agent: an absolute `instructions` path from a config dir was not
        # loaded by 2.0.29 (probed), and only these agents take part, so a team's own agents stay untouched
        "agents": {md.stem: agent(md, shared) for md in sorted((ROOT / "agents").glob("*.md"))},
        "mcp": {"servers": {"store": {"type": "local", "command": [*store, "serve"], "environment": env,
                                       # direct tools, not codemode: in 2.0.29 codemode calls to this server returned null (probed)
                                       "codemode": False}}},
        "plugins": [
            {"package": str(ROOT / "plugins" / "handoff"),
             "options": {"agent": "master", "thresholdTokens": a.threshold, "store": store, "env": env}},
            {"package": str(ROOT / "vendor" / "opencode-session-peers"), "options": {"scope": a.scope}},
        ],
    }
    out = ROOT / "config" / "opencode.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"wrote {out}\nrun:  OPENCODE_CONFIG_DIR={out.parent} opencode")


if __name__ == "__main__":
    main()
