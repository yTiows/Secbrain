"""SQLite persistence layer: schema, connections, transactions.

Single writer (RLock) + thread-local readers (WAL). Inside a `tx()` block the
current thread reads through the write connection so it sees its own writes.
"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,            -- file | note | api | import
  name TEXT NOT NULL,
  uri TEXT NOT NULL,             -- logical identity (filename / relative path / note:<id>)
  mime TEXT,
  sha256 TEXT NOT NULL,
  size INTEGER,
  text TEXT,                     -- extracted text (derived, regenerable from file)
  meta TEXT DEFAULT '{}',
  file_path TEXT,                -- content-addressed original
  status TEXT DEFAULT 'active',  -- active | archived | deleted
  flags TEXT DEFAULT '[]',       -- security flags
  supersedes TEXT,
  created_at REAL NOT NULL,
  UNIQUE(uri, sha256)
);
CREATE INDEX IF NOT EXISTS ix_sources_uri ON sources(uri);

CREATE TABLE IF NOT EXISTS objects(
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  summary TEXT DEFAULT '',
  importance REAL DEFAULT 0.5,
  confidence REAL DEFAULT 0.5,
  verification TEXT DEFAULT 'ai_extracted',
  status TEXT DEFAULT 'active',  -- active | archived | deleted | superseded
  project TEXT,
  origin_key TEXT,
  origin TEXT DEFAULT 'user',    -- user | import | ai | external
  source_id TEXT REFERENCES sources(id),
  content_hash TEXT,
  version INTEGER DEFAULT 1,
  extra TEXT DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  event_time REAL                -- when the described event/decision happened, if known
);
CREATE INDEX IF NOT EXISTS ix_obj_type ON objects(type);
CREATE INDEX IF NOT EXISTS ix_obj_status ON objects(status);
CREATE INDEX IF NOT EXISTS ix_obj_project ON objects(project);
CREATE INDEX IF NOT EXISTS ix_obj_origin ON objects(origin_key);
CREATE INDEX IF NOT EXISTS ix_obj_source ON objects(source_id);

CREATE TABLE IF NOT EXISTS object_versions(
  object_id TEXT NOT NULL REFERENCES objects(id),
  version INTEGER NOT NULL,
  snapshot TEXT NOT NULL,
  change_kind TEXT,
  actor TEXT,
  reason TEXT,
  created_at REAL NOT NULL,
  PRIMARY KEY(object_id, version)
);

CREATE TABLE IF NOT EXISTS tags(
  object_id TEXT NOT NULL REFERENCES objects(id),
  tag TEXT NOT NULL,
  origin TEXT DEFAULT 'user',
  PRIMARY KEY(object_id, tag)
);
CREATE INDEX IF NOT EXISTS ix_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS aliases(
  object_id TEXT NOT NULL REFERENCES objects(id),
  alias TEXT NOT NULL,
  alias_key TEXT NOT NULL,
  origin TEXT DEFAULT 'ai',
  PRIMARY KEY(object_id, alias_key)
);
CREATE INDEX IF NOT EXISTS ix_alias_key ON aliases(alias_key);

CREATE TABLE IF NOT EXISTS object_sources(
  object_id TEXT NOT NULL REFERENCES objects(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  role TEXT DEFAULT 'primary',   -- primary | evidence
  note TEXT,
  created_at REAL NOT NULL,
  PRIMARY KEY(object_id, source_id)
);

CREATE TABLE IF NOT EXISTS claims(
  id TEXT PRIMARY KEY,
  object_id TEXT NOT NULL REFERENCES objects(id),
  source_id TEXT REFERENCES sources(id),
  text TEXT NOT NULL,            -- claim sentence as extracted
  subject TEXT, predicate TEXT, value TEXT,
  subj_key TEXT, pred_key TEXT, val_key TEXT,
  functional INTEGER DEFAULT 0,  -- predicate is single-valued (conflicts detectable)
  polarity INTEGER DEFAULT 1,    -- -1 for negated assertions
  evidence TEXT,                 -- supporting passage
  confidence REAL DEFAULT 0.5,
  verification TEXT DEFAULT 'ai_extracted',
  status TEXT DEFAULT 'active',
  extracted_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_claims_obj ON claims(object_id);
CREATE INDEX IF NOT EXISTS ix_claims_sp ON claims(subj_key, pred_key);

CREATE TABLE IF NOT EXISTS edges(
  id TEXT PRIMARY KEY,
  src TEXT NOT NULL REFERENCES objects(id),
  dst TEXT NOT NULL REFERENCES objects(id),
  type TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT 'ai',          -- user | import | ai
  confidence REAL DEFAULT 1.0,
  explanation TEXT DEFAULT '',
  evidence TEXT DEFAULT '{}',
  verification TEXT DEFAULT 'suggested',      -- suggested | verified | rejected
  status TEXT DEFAULT 'active',               -- active | deleted
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(src, dst, type)
);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst);

CREATE TABLE IF NOT EXISTS edge_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edge_id TEXT NOT NULL,
  ts REAL NOT NULL,
  actor TEXT,
  action TEXT,
  before TEXT,
  after TEXT
);

CREATE TABLE IF NOT EXISTS embeddings(
  object_id TEXT PRIMARY KEY REFERENCES objects(id),
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vec BLOB NOT NULL,
  text_hash TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  title, content, tags, aliases, object_id UNINDEXED,
  tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  source_id TEXT,
  kind TEXT DEFAULT 'ingest',
  status TEXT NOT NULL,          -- queued | running | done | failed | awaiting_review | unchanged
  stage TEXT,
  attempts INTEGER DEFAULT 0,
  error TEXT,
  options TEXT DEFAULT '{}',
  result TEXT DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_candidates(
  term_key TEXT NOT NULL,
  display TEXT NOT NULL,
  object_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  count INTEGER DEFAULT 1,
  PRIMARY KEY(term_key, object_id)
);
CREATE INDEX IF NOT EXISTS ix_cand_key ON entity_candidates(term_key);

CREATE TABLE IF NOT EXISTS findings(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  level TEXT NOT NULL,           -- safe | conditional | protected
  status TEXT NOT NULL DEFAULT 'open',  -- open | applied | auto_applied | dismissed
  confidence REAL DEFAULT 0.5,
  severity TEXT DEFAULT 'info',
  title TEXT,
  explanation TEXT,
  payload TEXT DEFAULT '{}',
  targets TEXT DEFAULT '[]',
  dedupe_key TEXT UNIQUE,
  created_at REAL NOT NULL,
  resolved_at REAL
);

CREATE TABLE IF NOT EXISTS changes(
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  actor TEXT,
  kind TEXT NOT NULL,
  target TEXT,
  summary TEXT,
  undo TEXT DEFAULT '{}',
  undone INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,            -- useful | irrelevant | classification | edge | importance
  target TEXT,
  value TEXT,
  query TEXT,
  actor TEXT
);

CREATE TABLE IF NOT EXISTS synonyms(
  term_key TEXT NOT NULL,
  alias_key TEXT NOT NULL,
  PRIMARY KEY(term_key, alias_key)
);

CREATE TABLE IF NOT EXISTS api_keys(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  scopes TEXT NOT NULL DEFAULT '["read"]',
  projects TEXT NOT NULL DEFAULT '[]',   -- [] = unrestricted
  types TEXT NOT NULL DEFAULT '[]',      -- [] = unrestricted
  created_at REAL NOT NULL,
  revoked_at REAL,
  last_used_at REAL
);

CREATE TABLE IF NOT EXISTS proposals(
  id TEXT PRIMARY KEY,
  key_id TEXT,
  kind TEXT NOT NULL,            -- update | relationship
  target TEXT,
  payload TEXT NOT NULL,
  status TEXT DEFAULT 'open',    -- open | applied | rejected
  note TEXT,
  created_at REAL NOT NULL,
  resolved_at REAL
);
"""


def now() -> float:
    return time.time()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._wlock = threading.RLock()
        self._local = threading.local()
        self._wconn = self._connect()
        self._wconn.executescript(SCHEMA)
        self._wconn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        self._wconn.commit()
        self.generation = 0  # bumped on every committed write; caches key off it

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    @property
    def c(self) -> sqlite3.Connection:
        if getattr(self._local, "depth", 0) > 0:
            return self._wconn
        conn = getattr(self._local, "rconn", None)
        if conn is None:
            conn = self._connect()
            self._local.rconn = conn
        return conn

    @contextmanager
    def tx(self):
        """Atomic write transaction. Nested use becomes a savepoint."""
        self._wlock.acquire()
        depth = getattr(self._local, "depth", 0)
        try:
            if depth == 0:
                self._wconn.execute("BEGIN IMMEDIATE")
            else:
                self._wconn.execute(f"SAVEPOINT sp{depth}")
            self._local.depth = depth + 1
            try:
                yield self._wconn
            except BaseException:
                if depth == 0:
                    self._wconn.execute("ROLLBACK")
                else:
                    self._wconn.execute(f"ROLLBACK TO sp{depth}")
                    self._wconn.execute(f"RELEASE sp{depth}")
                raise
            else:
                if depth == 0:
                    self._wconn.execute("COMMIT")
                    self.generation += 1
                else:
                    self._wconn.execute(f"RELEASE sp{depth}")
            finally:
                self._local.depth = depth
        finally:
            self._wlock.release()

    # convenience
    def one(self, sql: str, args=()):
        return self.c.execute(sql, args).fetchone()

    def all(self, sql: str, args=()):
        return self.c.execute(sql, args).fetchall()

    def exec(self, sql: str, args=()):
        return self.c.execute(sql, args)

    def get_meta(self, key: str, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str):
        self.c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def backup(self, dest: str | Path) -> None:
        with self._wlock:
            dst = sqlite3.connect(str(dest))
            try:
                self._wconn.backup(dst)
            finally:
                dst.close()

    def close(self):
        try:
            self._wconn.close()
        except Exception:
            pass


def jdump(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def jload(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except Exception:
        return default
