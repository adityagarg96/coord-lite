# coord-lite

One **master** OpenCode session coordinates a set of requirements; a handful of **worker** sessions build them.
They share a SQLite graph (the **store**) and message each other through **session-peers**. The master hands
off to a fresh context when it fills, instead of compacting.

| piece | what it is | where |
|---|---|---|
| store | nodes (single parent = containment), typed edges, append-only receipts, briefs, an attributed change log, FTS5 search. Exposed as the `store` MCP server (17 tools). | `store/` (Python, `uv`) |
| handoff | at a context threshold the master writes a brief; the plugin then compacts the session and **replaces** OpenCode's summary with a boot packet from the store (brief + live plan + lint + recent writes). Same session id and title, so peers keep reaching it. | `plugins/handoff/` |
| peers | `peers` tool: list / send / ask / reply / handover between live sessions on this machine. Third-party, MIT, pinned to the reviewed commit `112c25c`. | `vendor/opencode-session-peers` (submodule) |
| agents | `master` and `worker` prompts, each carrying the shared laws. | `agents/`, `instructions/coordination.md` |

## Setup

Needs OpenCode **2.0.x** (built and tested against 2.0.29), `uv`, Python ≥ 3.11, git.

```sh
git clone --recursive <this repo> ~/coord-lite && cd ~/coord-lite
python3 install.py --threshold 600000          # writes config/opencode.json (absolute paths, nothing global)
cd <your work repo>
OPENCODE_CONFIG_DIR=~/coord-lite/config opencode
```

`OPENCODE_CONFIG_DIR` layers this config on top of yours: your `~/.config/opencode` and each repo's own
`AGENTS.md` still load. Re-run `install.py` after editing `agents/` or moving the checkout.

- `--threshold`: the master's handoff point in tokens, ~60–70% of the model's context window.
- `--db`: the store file (default `~/coord-lite-data/store.db`); every session must use the same one.
- `--scope`: peers visibility; only sessions with the same scope see each other.

## Daily use

1. Open the **master**: `Tab` to the `master` agent, title the session `master`. Paste the requirements; it
   loads them as one `plan` root with a `requirement` per item, each with an exit test and an owner.
2. Open up to five **worker** sessions (agent `worker`, titles `w1` … `w5`), in whichever repos they work in.
   Each finds its requirements by `attrs.owner`.
3. Sessions tell each other what changed with `peers send` ("R7 #42 is BLOCKED on #51"); the receiver runs
   `changes_since` and reads the store. Subagents that must stay reachable are spawned with `background: true`.
4. `map_export` (or `uv run --project store python -m coordlite export --root <id> --out MIND_MAP.md`) renders
   the `concept` tree as a plain `[N]` mind-map file for people without the store.

`/compact` on the master is a manual handoff. Workers compact the normal way.

## Tests

```sh
cd store && uv run pytest -q            # 15 tests, tmp SQLite only (incl. 5 concurrent writers, a real stdio MCP round trip)
cd plugins/handoff && node --test logic.test.ts   # 10 tests, the handoff state machine against a fake host
```

## Verified live (OpenCode 2.0.29, free `opencode/big-pickle`, isolated XDG dirs)

- Config, both plugins and the `store` MCP server load from a different repo via `OPENCODE_CONFIG_DIR`, next to
  that repo's own AGENTS.md; writes land in the store attributed to the actor; `peers status` connects.
- A full handoff: threshold crossed → brief written by the model → compaction replaced by the boot packet →
  the successor says it started from the packet and continues the brief's `next`.

Found by those runs and fixed: MCP `codemode` calls to the store returned `null`, so the store runs with
`codemode: false`; a compaction `result` without `tokens` stalled the next turn, so the plugin estimates them;
relative paths in the config resolve against the session's directory, so `install.py` writes absolute ones;
a config-dir `instructions` entry was not loaded, so the shared laws are inlined into each agent.

Not verified live: two sessions messaging each other through peers (the plugin's author tests that on 2.0.29;
here only `status` was exercised), and a handoff at a real-size threshold.

Caveat for testing: free models can claim tool results they never got. One run reported creating nodes while
every call had failed. Check the store, not the reply.

## Lessons

`lessons/standing-rules.md`: 35 rules generalized from incidents in real agent sessions (delete before
building, scale process to blast radius, long-job hygiene, provenance and measurement, safety, orchestration).
`lessons/agent-rules.md`: the longer corpus-derived set, mined from ~1,400 coded correction and bug events,
each rule naming the moment to apply it. `install.py --lessons core` (default) inlines the first into both
agents, `full` adds the second, `none` leaves both out. Both are plain Markdown and can be used with any agent.
