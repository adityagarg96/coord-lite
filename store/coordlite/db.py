"""The store. One SQLite file shared by every session; each session's MCP server opens its own
connection (WAL + busy_timeout), and every multi-statement write runs inside BEGIN IMMEDIATE.

Laws encoded here, not in prompts:
- parent_id is single-valued (containment, rollup). edge is many-to-many, no uniqueness.
- touched_at is written ONLY by add_receipt. Editing a node is not progress.
- receipt rows are append-only (triggers refuse UPDATE/DELETE).
- source_key makes every import idempotent ('brief:master', 'import:<id>').
"""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import timing

DB_PATH = Path(os.environ.get("COORD_DB", Path.home() / "coord-lite-data" / "store.db")).expanduser()

KINDS = {"goal", "project", "requirement", "concept", "idea", "task", "plan", "decision", "checkpoint", "lesson", "floor",
         "note", "mission", "capital", "code", "capture",
         "segment", "segment_instance", "day_template", "day_instance", "e3d"}
STATES = {"raw", "seed", "active", "parked", "done", "dropped"}
RELS = {"relates_to", "blocks", "derived_from", "supersedes", "evidences", "informs", "duplicates",
        "instance_of", "serves", "targets"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS node(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ({",".join(repr(k) for k in sorted(KINDS))})),
  title TEXT NOT NULL,
  body TEXT,
  parent_id INTEGER REFERENCES node(id),
  horizon TEXT,
  capital TEXT,
  mission TEXT,
  state TEXT NOT NULL CHECK (state IN ({",".join(repr(s) for s in sorted(STATES))})),
  status TEXT,
  attrs TEXT NOT NULL DEFAULT '{{}}',
  source_key TEXT UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state_changed_at TEXT,
  next_review_at TEXT,
  review_interval_days INTEGER,
  review_count INTEGER NOT NULL DEFAULT 0,
  snooze_count INTEGER NOT NULL DEFAULT 0,
  touched_at TEXT,
  why_dropped TEXT,
  due_at TEXT,
  starts_at TEXT,
  ends_at TEXT,
  anchor_kind TEXT,
  moveable BOOLEAN,
  CHECK (state != 'dropped' OR why_dropped IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS node_parent ON node(parent_id);
CREATE INDEX IF NOT EXISTS node_kind_state ON node(kind, state);
CREATE INDEX IF NOT EXISTS node_review ON node(next_review_at);
CREATE INDEX IF NOT EXISTS node_updated ON node(updated_at);

CREATE TABLE IF NOT EXISTS edge(
  id INTEGER PRIMARY KEY,
  src INTEGER NOT NULL REFERENCES node(id),
  dst INTEGER NOT NULL REFERENCES node(id),
  rel TEXT NOT NULL CHECK (rel IN ({",".join(repr(r) for r in sorted(RELS))})),
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS edge_src ON edge(src);
CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst);

CREATE TABLE IF NOT EXISTS receipt(
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  ts TEXT NOT NULL,
  node_id INTEGER REFERENCES node(id),
  payload TEXT NOT NULL DEFAULT '{{}}',
  dedupe_key TEXT UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS receipt_no_update BEFORE UPDATE ON receipt
  BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS receipt_no_delete BEFORE DELETE ON receipt
  BEGIN SELECT RAISE(ABORT, 'receipts are append-only'); END;

-- One row per master (or other scoped) session life; a handoff ends one and opens the next.
CREATE TABLE IF NOT EXISTS session(
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,                 -- 'master' | 'plan:<node_id>' | a worker's name
  opencode_session_id TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  ended_why TEXT,                      -- handoff | forced | error
  tokens_at_end INTEGER,
  brief_node_id INTEGER REFERENCES node(id)
);
CREATE INDEX IF NOT EXISTS session_scope_open ON session(scope, ended_at);

-- Every write, attributed: the coordination log 'changes_since' reads. Edge removals live here too.
CREATE TABLE IF NOT EXISTS change(
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  node_id INTEGER,
  what TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS change_at ON change(at);

CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(title, body, content='node', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS node_fts_ai AFTER INSERT ON node BEGIN
  INSERT INTO node_fts(rowid, title, body) VALUES (new.id, new.title, COALESCE(new.body, ''));
END;
CREATE TRIGGER IF NOT EXISTS node_fts_ad AFTER DELETE ON node BEGIN
  INSERT INTO node_fts(node_fts, rowid, title, body) VALUES ('delete', old.id, old.title, COALESCE(old.body, ''));
END;
CREATE TRIGGER IF NOT EXISTS node_fts_au AFTER UPDATE OF title, body ON node BEGIN
  INSERT INTO node_fts(node_fts, rowid, title, body) VALUES ('delete', old.id, old.title, COALESCE(old.body, ''));
  INSERT INTO node_fts(rowid, title, body) VALUES (new.id, new.title, COALESCE(new.body, ''));
END;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    migrate_checks(con)
    con.executescript(SCHEMA)
    return con


@contextmanager
def tx(con):
    """One write transaction. BEGIN IMMEDIATE takes the write lock up front, so a read-then-write
    (node_edit's history, subtree_create) cannot interleave with another session's write.
    Nested use joins the outer transaction."""
    if con.in_transaction:
        yield
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _in_list(values) -> str:
    return ",".join(repr(v) for v in sorted(values))


def migrate_checks(con):
    """Widen the CHECK lists on node.kind / edge.rel when KINDS or RELS grow. SQLite cannot ALTER a CHECK,
    so the table is rebuilt from its OWN stored CREATE statement with only the lists rewritten.
    One transaction, idempotent: a no-op once the lists match."""
    tables = {r["name"]: r["sql"] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ('node','edge')")}
    plans = []
    if "node" in tables:
        sql = tables["node"]
        new = re.sub(r"kind IN \([^)]*\)\)", f"kind IN ({_in_list(KINDS)}))", sql, count=1)
        if new != sql:
            plans.append(("node", new))
    if "edge" in tables:
        sql = tables["edge"]
        new = re.sub(r"rel IN \([^)]*\)", f"rel IN ({_in_list(RELS)})", sql, count=1)
        if new != sql:
            plans.append(("edge", new))
    if not plans:
        return False
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("BEGIN IMMEDIATE")
    try:
        for name, create in plans:
            tmp = f"{name}__widen"
            con.execute(re.sub(rf"^CREATE TABLE (IF NOT EXISTS )?\"?{name}\"?\s*\(", f"CREATE TABLE {tmp}(", create, count=1))
            con.execute(f"INSERT INTO {tmp} SELECT * FROM {name}")
            con.execute(f"DROP TABLE {name}")
            con.execute(f"ALTER TABLE {tmp} RENAME TO {name}")
        bad = con.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise sqlite3.IntegrityError(f"foreign key check failed after widening: {len(bad)} rows")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA foreign_keys=ON")
    # the rebuilt node table lost its triggers and the FTS index points at old rowids: recreate and rebuild
    con.executescript(SCHEMA)
    con.execute("INSERT INTO node_fts(node_fts) VALUES ('rebuild')")
    return True


# ---------------------------------------------------------------- nodes

NODE_COLS = ("kind", "title", "body", "parent_id", "horizon", "capital", "mission", "state", "status",
             "source_key", "created_at", "next_review_at", "review_interval_days", "why_dropped", *timing.TIME_FIELDS)


def insert_node(con, *, attrs: dict | None = None, **f) -> int:
    f.update(timing.validate(f, {k: f[k] for k in timing.TIME_FIELDS if k in f}))
    ts = now_iso()
    f.setdefault("created_at", ts)
    cols = [c for c in NODE_COLS if c in f]
    vals = [f[c] for c in cols]
    cur = con.execute(
        f"INSERT INTO node ({','.join(cols)}, attrs, updated_at, state_changed_at) "
        f"VALUES ({','.join('?' * len(cols))}, ?, ?, ?)",
        (*vals, json.dumps(attrs or {}, ensure_ascii=False), ts, ts))
    return cur.lastrowid


def update_node(con, node_id: int, *, attrs_patch: dict | None = None, **f) -> None:
    current = con.execute("SELECT * FROM node WHERE id=?", (node_id,)).fetchone()
    if current is None:
        raise ValueError("no such node")
    f.update(timing.validate(dict(current), {k: v for k, v in f.items() if k in timing.TIME_FIELDS}))
    sets, vals = [], []
    for k, v in f.items():
        if k not in NODE_COLS and k not in ("review_count", "snooze_count"):
            raise ValueError(f"not an editable column: {k}")
        sets.append(f"{k} = ?")
        vals.append(v)
    if "state" in f:
        sets.append("state_changed_at = ?")
        vals.append(now_iso())
    if attrs_patch:
        a = json.loads(current["attrs"] or "{}")
        a.update(attrs_patch)
        sets.append("attrs = ?")
        vals.append(json.dumps(a, ensure_ascii=False))
    sets.append("updated_at = ?")
    vals.append(now_iso())
    con.execute(f"UPDATE node SET {', '.join(sets)} WHERE id = ?", (*vals, node_id))


def add_edge(con, src: int, dst: int, rel: str, note: str | None = None) -> int:
    return con.execute("INSERT INTO edge (src, dst, rel, note, created_at) VALUES (?,?,?,?,?)",
                       (src, dst, rel, note, now_iso())).lastrowid


def add_receipt(con, source: str, ts: str, node_id: int | None, payload: dict | None = None,
                dedupe_key: str | None = None) -> int | None:
    """The ONLY writer of touched_at."""
    try:
        rid = con.execute(
            "INSERT INTO receipt (source, ts, node_id, payload, dedupe_key, created_at) VALUES (?,?,?,?,?,?)",
            (source, ts, node_id, json.dumps(payload or {}, ensure_ascii=False), dedupe_key, now_iso())).lastrowid
    except sqlite3.IntegrityError:
        return None
    if node_id is not None:
        con.execute("UPDATE node SET touched_at = ? WHERE id = ? AND (touched_at IS NULL OR touched_at < ?)",
                    (ts, node_id, ts))
    return rid


def log_change(con, actor: str, node_id: int | None, what: str) -> None:
    con.execute("INSERT INTO change (at, actor, node_id, what) VALUES (?,?,?,?)",
                (now_iso(), actor or "unknown", node_id, what[:300]))


def node_dict(con, row: sqlite3.Row) -> dict:
    a = json.loads(row["attrs"] or "{}")
    parent_title = None
    if row["parent_id"]:
        p = con.execute("SELECT title FROM node WHERE id = ?", (row["parent_id"],)).fetchone()
        parent_title = p["title"] if p else None
    ref = parse_iso(row["touched_at"]) or parse_iso(row["created_at"])
    untouched = (datetime.now(timezone.utc) - ref).days if ref else None
    child_count = con.execute("SELECT COUNT(*) FROM node WHERE parent_id = ?", (row["id"],)).fetchone()[0]
    return {
        "id": row["id"], "kind": row["kind"], "title": row["title"], "body": row["body"],
        "summary": a.get("summary"),
        "parent_id": row["parent_id"], "parent_title": parent_title,
        "horizon": row["horizon"], "capital": row["capital"], "mission": row["mission"],
        "state": row["state"], "status": row["status"],
        "created_at": row["created_at"], "updated_at": row["updated_at"], "touched_at": row["touched_at"],
        "why_dropped": row["why_dropped"], "untouched_days": untouched, "child_count": child_count,
        **{k: (bool(row[k]) if k == "moveable" and row[k] is not None else row[k]) for k in timing.TIME_FIELDS},
        "time": timing.derived(dict(row)),
        "attrs": a,
    }


def get_node(con, node_id: int) -> dict | None:
    row = con.execute("SELECT * FROM node WHERE id = ?", (node_id,)).fetchone()
    return node_dict(con, row) if row else None
