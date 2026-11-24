"""Knowledge representation layer: sources, objects, versions, tags, aliases, claims, changes, embeddings."""
from __future__ import annotations
import hashlib
import json
import secrets
import threading
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import Settings
from .db import Database, jdump, jload, now
from .embeddings import HashTfidfEmbedder
from .knowledge_types import OBJECT_TYPES, VERIFICATION
from .text import key as name_key, sha256_text, truncate, sentences


class NotFound(KeyError):
    pass


class Invalid(ValueError):
    pass


def new_id(prefix: str = "k") -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


SNAPSHOT_FIELDS = ("type", "title", "content", "summary", "importance", "confidence", "verification",
                   "status", "project", "extra", "event_time")


class KnowledgeStore:
    def __init__(self, db: Database, settings: Settings, embedder: HashTfidfEmbedder | None = None):
        self.db = db
        self.s = settings
        self.s.ensure()
        self.embedder = embedder or HashTfidfEmbedder(settings.embedding_dim)
        blob = self._load_stats()
        if blob:
            self.embedder.load_stats(blob)
        self._mat_lock = threading.Lock()
        self._mat_gen = -1
        self._mat_ids: list[str] = []
        self._mat: np.ndarray | None = None

    # ------------------------------------------------------------------ stats
    def _load_stats(self) -> bytes | None:
        r = self.db.one("SELECT value FROM meta WHERE key='emb_stats'")
        return bytes.fromhex(r["value"]) if r else None

    def _save_stats(self):
        self.db.set_meta("emb_stats", self.embedder.dump_stats().hex())

    # ---------------------------------------------------------------- sources
    def add_source(self, *, kind: str, name: str, uri: str | None = None, data: bytes | None = None,
                   text: str | None = None, mime: str | None = None, meta: dict | None = None,
                   flags: list | None = None) -> tuple[dict, str]:
        """Persist an original. Returns (source, status) with status new | unchanged | new_version.
        Original bytes are content-addressed and never modified."""
        uri = uri or name
        if data is None:
            data = (text or "").encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        with self.db.tx():
            ex = self.db.one("SELECT * FROM sources WHERE uri=? AND sha256=?", (uri, sha))
            if ex:
                return self._src(ex), "unchanged"
            prev = self.db.one("SELECT id FROM sources WHERE uri=? ORDER BY created_at DESC LIMIT 1", (uri,))
            ext = Path(name).suffix[:10] if Path(name).suffix.isascii() else ""
            fp = self.s.sources_dir / f"{sha}{ext}"
            if not fp.exists():
                tmp = fp.with_suffix(fp.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(fp)
            sid = new_id("s")
            self.db.exec(
                "INSERT INTO sources(id,kind,name,uri,mime,sha256,size,text,meta,file_path,flags,supersedes,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, kind, name, uri, mime, sha, len(data), text, jdump(meta or {}), str(fp), jdump(flags or []),
                 prev["id"] if prev else None, now()))
            return self._src(self.db.one("SELECT * FROM sources WHERE id=?", (sid,))), ("new_version" if prev else "new")

    def set_source_text(self, sid: str, text: str, meta: dict | None = None, flags: list | None = None):
        with self.db.tx():
            r = self.db.one("SELECT meta, flags FROM sources WHERE id=?", (sid,))
            m = jload(r["meta"], {})
            m.update(meta or {})
            fl = sorted(set(jload(r["flags"], []) + (flags or [])))
            self.db.exec("UPDATE sources SET text=?, meta=?, flags=? WHERE id=?", (text, jdump(m), jdump(fl), sid))

    def _src(self, r) -> dict:
        d = dict(r)
        d["meta"] = jload(d.get("meta"), {})
        d["flags"] = jload(d.get("flags"), [])
        return d

    def get_source(self, sid: str) -> dict:
        r = self.db.one("SELECT * FROM sources WHERE id=?", (sid,))
        if not r:
            raise NotFound(sid)
        return self._src(r)

    def list_sources(self, limit=200, offset=0) -> list[dict]:
        rows = self.db.all("SELECT id,kind,name,uri,mime,sha256,size,status,flags,created_at,supersedes, "
                           "(SELECT COUNT(*) FROM objects o WHERE o.source_id=sources.id) AS n_objects "
                           "FROM sources ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = jload(d["flags"], [])
            out.append(d)
        return out

    def read_source_bytes(self, sid: str) -> bytes:
        s = self.get_source(sid)
        p = Path(s["file_path"])
        if not p.exists():
            raise NotFound(f"source file missing: {p.name}")
        return p.read_bytes()

    # ---------------------------------------------------------------- objects
    def _obj(self, r, full: bool = True) -> dict:
        d = dict(r)
        d["extra"] = jload(d.get("extra"), {})
        if full:
            oid = d["id"]
            d["tags"] = [t["tag"] for t in self.db.all("SELECT tag FROM tags WHERE object_id=? ORDER BY tag", (oid,))]
            d["aliases"] = [a["alias"] for a in self.db.all("SELECT alias FROM aliases WHERE object_id=? ORDER BY alias", (oid,))]
        return d

    def get_object(self, oid: str, include_deleted: bool = True) -> dict:
        r = self.db.one("SELECT * FROM objects WHERE id=?", (oid,))
        if not r or (r["status"] == "deleted" and not include_deleted):
            raise NotFound(oid)
        d = self._obj(r)
        d["sources"] = [dict(x) for x in self.db.all(
            "SELECT os.source_id, os.role, os.note, s.name, s.uri, s.sha256 FROM object_sources os "
            "JOIN sources s ON s.id=os.source_id WHERE os.object_id=? ORDER BY os.role, os.created_at", (oid,))]
        d["claims"] = self.list_claims(oid)
        return d

    def find_by_origin(self, origin_key: str) -> dict | None:
        r = self.db.one("SELECT * FROM objects WHERE origin_key=? AND status!='deleted' ORDER BY created_at LIMIT 1", (origin_key,))
        return self._obj(r) if r else None

    def find_by_title_key(self, title: str, type_: str | None = None) -> list[dict]:
        k = name_key(title)
        rows = self.db.all("SELECT * FROM objects WHERE status IN ('active','archived') " + ("AND type=? " if type_ else ""),
                           (type_,) if type_ else ())
        return [self._obj(r, False) for r in rows if name_key(r["title"]) == k]

    def list_objects(self, *, type: str | None = None, status: str | None = "active", project: str | None = None,
                     tag: str | None = None, q: str | None = None, source_id: str | None = None,
                     limit: int = 100, offset: int = 0, order: str = "updated_at DESC") -> list[dict]:
        sql, args = "SELECT o.* FROM objects o", []
        where = []
        if tag:
            sql += " JOIN tags t ON t.object_id=o.id"
            where.append("t.tag=?"); args.append(tag)
        if type:
            where.append("o.type=?"); args.append(type)
        if status:
            where.append("o.status=?"); args.append(status)
        if project:
            where.append("o.project=?"); args.append(project)
        if source_id:
            where.append("o.source_id=?"); args.append(source_id)
        if q:
            where.append("(o.title LIKE ? OR o.content LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
        if where:
            sql += " WHERE " + " AND ".join(where)
        assert order in ("updated_at DESC", "created_at DESC", "importance DESC", "title ASC")
        sql += f" ORDER BY o.{order} LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [self._obj(r) for r in self.db.all(sql, args)]

    def count_objects(self, status: str | None = "active") -> int:
        if status:
            return self.db.one("SELECT COUNT(*) c FROM objects WHERE status=?", (status,))["c"]
        return self.db.one("SELECT COUNT(*) c FROM objects")["c"]

    def _snapshot(self, oid: str) -> dict:
        o = self.get_object(oid)
        snap = {k: o[k] for k in SNAPSHOT_FIELDS}
        snap["tags"] = o["tags"]
        snap["aliases"] = o["aliases"]
        return snap

    def _embed_text(self, title: str, content: str, tags: Iterable[str] = ()) -> str:
        return f"{title}\n{title}\n{' '.join(tags)}\n{content[:6000]}"

    def _index(self, oid: str, *, observe: bool):
        """Refresh FTS + embedding for an object (derived data)."""
        o = self._obj(self.db.one("SELECT * FROM objects WHERE id=?", (oid,)))
        self.db.exec("DELETE FROM fts WHERE object_id=?", (oid,))
        if o["status"] in ("active", "archived", "superseded"):
            self.db.exec("INSERT INTO fts(title,content,tags,aliases,object_id) VALUES(?,?,?,?,?)",
                         (o["title"], o["content"], " ".join(o["tags"]), " ".join(o["aliases"]), oid))
        text = self._embed_text(o["title"], o["content"], o["tags"] + o["aliases"])
        th = sha256_text(text)
        ex = self.db.one("SELECT text_hash FROM embeddings WHERE object_id=?", (oid,))
        if ex and ex["text_hash"] == th:
            return
        if observe and not ex:
            self.embedder.observe(text)
            self._save_stats()
        v = self.embedder.embed(text)
        self.db.exec("INSERT INTO embeddings(object_id,model,dim,vec,text_hash) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(object_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, vec=excluded.vec, text_hash=excluded.text_hash",
                     (oid, self.embedder.name, self.embedder.dim, v.astype(np.float32).tobytes(), th))

    def create_object(self, *, type: str, title: str, content: str = "", summary: str = "", importance: float = 0.5,
                      confidence: float = 0.5, verification: str = "ai_extracted", project: str | None = None,
                      tags: Iterable[str] = (), aliases: Iterable[str] = (), source_id: str | None = None,
                      origin_key: str | None = None, origin: str = "user", extra: dict | None = None,
                      event_time: float | None = None, actor: str = "user", reason: str = "create",
                      tag_origin: str | None = None, oid: str | None = None) -> dict:
        if type not in OBJECT_TYPES:
            raise Invalid(f"unknown type {type!r}")
        if verification not in VERIFICATION:
            raise Invalid(f"unknown verification {verification!r}")
        title = (title or "").strip()
        if not title:
            raise Invalid("title required")
        for nm, v in (("importance", importance), ("confidence", confidence)):
            if not 0 <= float(v) <= 1:
                raise Invalid(f"{nm} must be within [0,1]")
        oid = oid or new_id("k")
        t = now()
        with self.db.tx():
            self.db.exec(
                "INSERT INTO objects(id,type,title,content,summary,importance,confidence,verification,project,origin_key,origin,"
                "source_id,content_hash,version,extra,created_at,updated_at,event_time) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, type, title, content, summary, importance, confidence, verification, project, origin_key, origin,
                 source_id, sha256_text(content), 1, jdump(extra or {}), t, t, event_time))
            for tg in dict.fromkeys(self._norm_tag(x) for x in tags if x):
                self.db.exec("INSERT OR IGNORE INTO tags(object_id,tag,origin) VALUES(?,?,?)", (oid, tg, tag_origin or origin))
            for al in aliases:
                self._add_alias_nolock(oid, al, origin)
            if source_id:
                self.db.exec("INSERT OR IGNORE INTO object_sources(object_id,source_id,role,created_at) VALUES(?,?,?,?)",
                             (oid, source_id, "primary", t))
            self._index(oid, observe=True)
            self._version(oid, "create", actor, reason)
            return self.get_object(oid)

    @staticmethod
    def _norm_tag(t: str) -> str:
        t = t.strip().lstrip("#").lower().replace(" ", "-")
        return t[:60]

    def _version(self, oid: str, kind: str, actor: str, reason: str):
        o = self.db.one("SELECT version FROM objects WHERE id=?", (oid,))
        self.db.exec("INSERT OR REPLACE INTO object_versions(object_id,version,snapshot,change_kind,actor,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                     (oid, o["version"], jdump(self._snapshot(oid)), kind, actor, reason, now()))

    def update_object(self, oid: str, patch: dict, *, actor: str = "user", reason: str = "edit",
                      kind: str = "update", log_change: bool = True) -> dict:
        """Versioned update. Previous state is never lost (object_versions holds full snapshots)."""
        allowed = set(SNAPSHOT_FIELDS) | {"tags", "aliases", "project", "extra"}
        bad = set(patch) - allowed
        if bad:
            raise Invalid(f"unknown fields: {sorted(bad)}")
        with self.db.tx():
            cur = self.get_object(oid)
            if cur["status"] == "deleted" and patch.get("status") not in ("active", "archived"):
                raise Invalid("object is deleted; restore it first")
            before = self._snapshot(oid)
            sets, args = [], []
            for f in SNAPSHOT_FIELDS:
                if f in patch and patch[f] != cur[f]:
                    v = patch[f]
                    if f == "type" and v not in OBJECT_TYPES:
                        raise Invalid(f"unknown type {v!r}")
                    if f == "verification" and v not in VERIFICATION:
                        raise Invalid(f"unknown verification {v!r}")
                    if f in ("importance", "confidence") and not 0 <= float(v) <= 1:
                        raise Invalid(f"{f} must be within [0,1]")
                    if f == "title" and not str(v).strip():
                        raise Invalid("title required")
                    if f == "extra":
                        v = jdump(v)
                    sets.append(f"{f}=?"); args.append(v)
            tags_changed = "tags" in patch and sorted(self._norm_tag(t) for t in patch["tags"]) != cur["tags"]
            al_changed = "aliases" in patch and sorted(patch["aliases"]) != cur["aliases"]
            if not sets and not tags_changed and not al_changed:
                return cur
            if "content" in patch and patch["content"] != cur["content"]:
                sets.append("content_hash=?"); args.append(sha256_text(patch["content"]))
            sets += ["version=version+1", "updated_at=?"]; args.append(now())
            self.db.exec(f"UPDATE objects SET {', '.join(sets)} WHERE id=?", (*args, oid))
            if tags_changed:
                self.db.exec("DELETE FROM tags WHERE object_id=?", (oid,))
                for tg in dict.fromkeys(self._norm_tag(x) for x in patch["tags"] if x):
                    self.db.exec("INSERT OR IGNORE INTO tags(object_id,tag,origin) VALUES(?,?,?)", (oid, tg, actor))
            if al_changed:
                self.db.exec("DELETE FROM aliases WHERE object_id=?", (oid,))
                for al in patch["aliases"]:
                    self._add_alias_nolock(oid, al, actor)
            self._index(oid, observe=False)
            self._version(oid, kind, actor, reason)
            if log_change:
                self.log_change(actor, "object_update", oid, f"{reason}: {', '.join(sorted(set(patch)))}",
                                {"op": "restore_snapshot", "object_id": oid, "snapshot": before})
            return self.get_object(oid)

    def add_tags(self, oid: str, tags: Iterable[str], actor="ai", origin="ai", log=True) -> list[str]:
        added = []
        with self.db.tx():
            for tg in tags:
                tg = self._norm_tag(tg)
                if not tg:
                    continue
                cur = self.db.exec("INSERT OR IGNORE INTO tags(object_id,tag,origin) VALUES(?,?,?)", (oid, tg, origin))
                if cur.rowcount:
                    added.append(tg)
            if added:
                self.db.exec("UPDATE objects SET version=version+1, updated_at=? WHERE id=?", (now(), oid))
                self._index(oid, observe=False)
                self._version(oid, "tags", actor, "add tags")
                if log:
                    self.log_change(actor, "tags_added", oid, f"added tags {added}", {"op": "remove_tags", "object_id": oid, "tags": added})
        return added

    def _add_alias_nolock(self, oid: str, alias: str, origin: str = "ai") -> bool:
        alias = alias.strip()
        k = name_key(alias)
        if not k:
            return False
        cur = self.db.exec("INSERT OR IGNORE INTO aliases(object_id,alias,alias_key,origin) VALUES(?,?,?,?)", (oid, alias, k, origin))
        return bool(cur.rowcount)

    def add_alias(self, oid: str, alias: str, origin: str = "ai") -> bool:
        with self.db.tx():
            ok = self._add_alias_nolock(oid, alias, origin)
            if ok:
                self._index(oid, observe=False)
        return ok

    def set_status(self, oid: str, status: str, *, actor="user", reason="") -> dict:
        if status not in ("active", "archived", "deleted", "superseded"):
            raise Invalid(status)
        return self.update_object(oid, {"status": status}, actor=actor, reason=reason or f"status→{status}", kind="status")

    def purge_object(self, oid: str, *, confirm: str) -> None:
        """PROTECTED: irreversible. Requires confirm == object id."""
        if confirm != oid:
            raise Invalid("purge requires confirm=<object id>")
        with self.db.tx():
            o = self.get_object(oid)
            if o["status"] != "deleted":
                raise Invalid("only soft-deleted objects can be purged")
            for t in ("fts",):
                self.db.exec(f"DELETE FROM {t} WHERE object_id=?", (oid,))
            for t, col in (("embeddings", "object_id"), ("tags", "object_id"), ("aliases", "object_id"), ("claims", "object_id"),
                           ("object_sources", "object_id"), ("object_versions", "object_id"), ("entity_candidates", "object_id")):
                self.db.exec(f"DELETE FROM {t} WHERE {col}=?", (oid,))
            self.db.exec("DELETE FROM edges WHERE src=? OR dst=?", (oid, oid))
            self.db.exec("DELETE FROM objects WHERE id=?", (oid,))
            self.log_change("user", "object_purge", oid, f"purged '{o['title']}' (irreversible)", {"op": "none"})

    def versions(self, oid: str) -> list[dict]:
        rows = self.db.all("SELECT version, change_kind, actor, reason, created_at, snapshot FROM object_versions WHERE object_id=? ORDER BY version DESC", (oid,))
        return [{**{k: r[k] for k in ("version", "change_kind", "actor", "reason", "created_at")}, "snapshot": jload(r["snapshot"], {})} for r in rows]

    def restore_version(self, oid: str, version: int, actor="user") -> dict:
        r = self.db.one("SELECT snapshot FROM object_versions WHERE object_id=? AND version=?", (oid, version))
        if not r:
            raise NotFound(f"{oid}@{version}")
        snap = jload(r["snapshot"], {})
        return self.update_object(oid, snap, actor=actor, reason=f"restore version {version}", kind="restore")

    # ----------------------------------------------------------------- evidence
    def add_evidence(self, oid: str, source_id: str, note: str = "") -> bool:
        with self.db.tx():
            cur = self.db.exec("INSERT OR IGNORE INTO object_sources(object_id,source_id,role,note,created_at) VALUES(?,?,?,?,?)",
                               (oid, source_id, "evidence", note, now()))
            return bool(cur.rowcount)

    # ------------------------------------------------------------------- claims
    def add_claim(self, *, object_id: str, source_id: str | None, text: str, subject=None, predicate=None, value=None,
                  subj_key=None, pred_key=None, val_key=None, functional=False, polarity: int = 1, evidence: str = "",
                  confidence: float = 0.5, verification: str = "ai_extracted") -> str:
        cid = new_id("c")
        self.db.exec("INSERT INTO claims(id,object_id,source_id,text,subject,predicate,value,subj_key,pred_key,val_key,functional,polarity,evidence,confidence,verification,extracted_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, object_id, source_id, text, subject, predicate, value, subj_key, pred_key, val_key,
                      1 if functional else 0, polarity, evidence, confidence, verification, now()))
        return cid

    def list_claims(self, oid: str) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT * FROM claims WHERE object_id=? AND status='active' ORDER BY extracted_at", (oid,))]

    # ------------------------------------------------------------------ changes
    def log_change(self, actor: str, kind: str, target: str, summary: str, undo: dict) -> str:
        cid = new_id("ch")
        self.db.exec("INSERT INTO changes(id,ts,actor,kind,target,summary,undo) VALUES(?,?,?,?,?,?,?)",
                     (cid, now(), actor, kind, target, summary, jdump(undo)))
        return cid

    def list_changes(self, limit=100, offset=0) -> list[dict]:
        out = []
        for r in self.db.all("SELECT * FROM changes ORDER BY ts DESC, rowid DESC LIMIT ? OFFSET ?", (limit, offset)):
            d = dict(r)
            d["undo"] = jload(d["undo"], {})
            out.append(d)
        return out

    # --------------------------------------------------------------- embeddings
    def embedding_matrix(self) -> tuple[list[str], np.ndarray]:
        """In-memory cache of all vectors for active objects, invalidated by DB generation."""
        with self._mat_lock:
            gen = self.db.generation
            if self._mat is None or self._mat_gen != gen:
                rows = self.db.all("SELECT e.object_id, e.vec FROM embeddings e JOIN objects o ON o.id=e.object_id WHERE o.status IN ('active','superseded')")
                ids = [r["object_id"] for r in rows]
                if rows:
                    mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
                else:
                    mat = np.zeros((0, self.embedder.dim), dtype=np.float32)
                self._mat, self._mat_ids, self._mat_gen = mat, ids, gen
            return self._mat_ids, self._mat

    def embedding_of(self, oid: str) -> np.ndarray | None:
        r = self.db.one("SELECT vec FROM embeddings WHERE object_id=?", (oid,))
        return np.frombuffer(r["vec"], dtype=np.float32) if r else None

    def similar(self, vec: np.ndarray, k: int = 10, exclude: set[str] | None = None) -> list[tuple[str, float]]:
        ids, mat = self.embedding_matrix()
        if not len(ids):
            return []
        sims = mat @ vec
        order = np.argsort(-sims)[: k + len(exclude or ()) + 1]
        out = []
        for i in order:
            if exclude and ids[i] in exclude:
                continue
            out.append((ids[i], float(sims[i])))
            if len(out) >= k:
                break
        return out

    def reindex(self) -> dict:
        """Rebuild all derived data (FTS, IDF stats, embeddings) from authoritative tables."""
        with self.db.tx():
            self.db.exec("DELETE FROM fts")
            self.db.exec("DELETE FROM embeddings")
            self.embedder.reset_stats()
            rows = self.db.all("SELECT id FROM objects WHERE status!='deleted'")
            texts = {}
            for r in rows:
                o = self._obj(self.db.one("SELECT * FROM objects WHERE id=?", (r["id"],)))
                texts[r["id"]] = self._embed_text(o["title"], o["content"], o["tags"] + o["aliases"])
                self.embedder.observe(texts[r["id"]])
            self._save_stats()
            for oid, text in texts.items():
                o = self._obj(self.db.one("SELECT * FROM objects WHERE id=?", (oid,)))
                self.db.exec("INSERT INTO fts(title,content,tags,aliases,object_id) VALUES(?,?,?,?,?)",
                             (o["title"], o["content"], " ".join(o["tags"]), " ".join(o["aliases"]), oid))
                self.db.exec("INSERT INTO embeddings(object_id,model,dim,vec,text_hash) VALUES(?,?,?,?,?)",
                             (oid, self.embedder.name, self.embedder.dim, self.embedder.embed(text).astype(np.float32).tobytes(), sha256_text(text)))
        return {"objects": len(texts), "model": self.embedder.name}

    # -------------------------------------------------------------------- stats
    def stats(self) -> dict:
        q = lambda s, a=(): self.db.one(s, a)["c"]
        return {
            "objects": q("SELECT COUNT(*) c FROM objects WHERE status='active'"),
            "archived": q("SELECT COUNT(*) c FROM objects WHERE status='archived'"),
            "deleted": q("SELECT COUNT(*) c FROM objects WHERE status='deleted'"),
            "sources": q("SELECT COUNT(*) c FROM sources"),
            "claims": q("SELECT COUNT(*) c FROM claims WHERE status='active'"),
            "edges": q("SELECT COUNT(*) c FROM edges WHERE status='active' AND verification!='rejected'"),
            "by_type": {r["type"]: r["c"] for r in self.db.all("SELECT type, COUNT(*) c FROM objects WHERE status='active' GROUP BY type")},
        }
