"""Ingestion engine: validate → extract → preserve → structure → extract knowledge → dedupe → relate → validate → atomic commit.

Everything that mutates knowledge for one source happens inside ONE SQLite transaction, so a failure (or a dry run)
leaves no partial state. Job state lives outside that transaction so failures are recorded.
"""
from __future__ import annotations
import queue
import re
import threading
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..config import Settings
from ..db import Database, jdump, jload, now
from ..findings import add_finding
from ..graph import GraphEngine
from ..knowledge import KnowledgeStore, new_id, Invalid
from ..knowledge_types import OBJECT_TYPES
from ..llm import LLMProvider, NullLLM
from ..text import key as name_key, shingles, jaccard, sha256_text, truncate, tokens, normalize_unicode
from . import security
from .classify import classify
from .claims import extract_claims, key_sentences
from .enrich import summarize, keywords, inline_tags, find_date, parse_date, importance_for, GENERIC_TITLES
from .entities import extract_entities, find_known, Cand, STOP_CAPS
from .extractors import extract, ExtractionError, SUPPORTED
from .relationships import RelationshipDiscoverer
from .structure import sectionize, Section


class _DryRun(Exception):
    def __init__(self, report):
        self.report = report


class ValidationError(Exception):
    pass


@dataclass
class Unit:
    title: str
    body: str
    origin_key: str
    heading_path: list[str]
    level: int = 2
    type: str = "fact"
    type_conf: float = 0.4
    cues: list = field(default_factory=list)
    is_doc: bool = False
    oid: str | None = None
    decision: str = "new"
    section_keys: list = field(default_factory=list)


class IngestionEngine:
    def __init__(self, db: Database, store: KnowledgeStore, graph: GraphEngine, settings: Settings, llm: LLMProvider | None = None):
        self.db, self.store, self.graph, self.s = db, store, graph, settings
        self.llm = llm or NullLLM()
        self.rel = RelationshipDiscoverer(db, store, graph, settings)
        self._q: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ worker mgmt
    def start(self):
        """Start background worker; recover jobs interrupted by a crash."""
        if self._thread and self._thread.is_alive():
            return
        with self.db.tx():
            self.db.exec("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (now(),))
        for r in self.db.all("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at"):
            self._q.put(r["id"])
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ingest-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._q.put("")
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            jid = self._q.get()
            if not jid or self._stop.is_set():
                continue
            try:
                self.run_job(jid)
            except Exception:
                pass

    # ------------------------------------------------------------------- jobs
    def _job(self, r) -> dict:
        d = dict(r)
        d["options"] = jload(d["options"], {})
        d["result"] = jload(d["result"], {})
        return d

    def get_job(self, jid: str) -> dict:
        r = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        if not r:
            raise KeyError(jid)
        return self._job(r)

    def list_jobs(self, limit: int = 100, status: str | None = None) -> list[dict]:
        if status:
            rows = self.db.all("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = self._job(r)
            d["result"] = {k: d["result"].get(k) for k in ("summary", "error") if k in d["result"]}
            out.append(d)
        return out

    def _set_job(self, jid: str, **kw):
        with self.db.tx():
            cols, args = [], []
            for k, v in kw.items():
                cols.append(f"{k}=?")
                args.append(jdump(v) if k in ("result", "options") else v)
            cols.append("updated_at=?"); args.append(now())
            self.db.exec(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", (*args, jid))

    # --------------------------------------------------------------- submission
    def ingest_bytes(self, name: str, data: bytes, *, kind: str = "file", uri: str | None = None, mime: str | None = None,
                     project: str | None = None, tags=(), type: str | None = None, dry_run: bool = False, review: bool = False,
                     wait: bool = True, force: bool = False, origin: str = "import", actor: str = "ingest",
                     importance: float | None = None) -> dict:
        name = PurePosixPath(str(name).replace("\\", "/")).name or "untitled.txt"
        uri = uri or name
        if len(data) > self.s.max_upload_bytes:
            raise Invalid(f"file too large ({len(data)} bytes; limit {self.s.max_upload_bytes})")
        if type and type not in OBJECT_TYPES:
            raise Invalid(f"unknown type {type!r}")
        ext = Path(name).suffix.lower()
        if ext and ext not in SUPPORTED:
            raise Invalid(f"unsupported file type {ext!r}; supported: {sorted(SUPPORTED)}")
        options = {"project": project, "tags": list(tags), "type": type, "dry_run": dry_run, "review": review, "force": force,
                   "origin": origin, "actor": actor, "importance": importance}
        try:
            ex = extract(name, data, mime)
            if len(ex.text) > self.s.max_text_chars:
                raise ExtractionError(f"text too large ({len(ex.text)} chars; limit {self.s.max_text_chars})")
            err = None
        except ExtractionError as e:
            ex, err = None, str(e)
        flags = security.scan(ex.text) if ex else []
        src, status = self.store.add_source(kind=kind, name=name, uri=uri, data=data, text=ex.text if ex else None, mime=mime,
                                            meta={"format": ex.format, "front": ex.meta, "title": ex.title,
                                                  "warnings": ex.warnings} if ex else {"format": None}, flags=flags)
        jid = new_id("j")
        t = now()
        if err:
            with self.db.tx():
                self.db.exec("INSERT INTO jobs(id,source_id,status,stage,error,options,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (jid, src["id"], "failed", "extract", err, jdump(options), jdump({"error": err, "summary": f"extraction failed: {err}"}), t, t))
            return self.get_job(jid)
        if status == "unchanged" and not force and not dry_run:
            prev = self.db.one("SELECT id FROM jobs WHERE source_id=? AND status IN ('done','unchanged') ORDER BY created_at DESC LIMIT 1", (src["id"],))
            if prev:
                with self.db.tx():
                    self.db.exec("INSERT INTO jobs(id,source_id,status,stage,options,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                 (jid, src["id"], "unchanged", "done", jdump(options),
                                  jdump({"summary": "identical content already ingested; skipped", "source_id": src["id"], "previous_job": prev["id"]}), t, t))
                return self.get_job(jid)
        with self.db.tx():
            self.db.exec("INSERT INTO jobs(id,source_id,status,stage,options,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                         (jid, src["id"], "queued", "queued", jdump(options), t, t))
        if wait:
            return self.run_job(jid)
        self._q.put(jid)
        return self.get_job(jid)

    def ingest_note(self, content: str, *, title: str | None = None, type: str | None = None, project: str | None = None,
                    tags=(), importance: float | None = None, wait: bool = True, origin: str = "user", actor: str = "user",
                    dry_run: bool = False, review: bool = False) -> dict:
        if not content or not content.strip():
            raise Invalid("content required")
        body = content if not title else (f"# {title}\n\n{content}" if not content.lstrip().startswith("#") else content)
        nid = new_id("n")
        return self.ingest_bytes(f"{(title or 'note')[:60]}.md".replace("/", "-"), body.encode("utf-8"), kind="note", uri=f"note:{nid}",
                                 project=project, tags=tags, type=type, wait=wait, origin=origin, actor=actor, importance=importance,
                                 dry_run=dry_run, review=review)

    def retry(self, jid: str, wait: bool = True) -> dict:
        j = self.get_job(jid)
        if j["status"] not in ("failed", "awaiting_review", "unchanged", "done"):
            raise Invalid(f"job in status {j['status']} cannot be retried")
        self._set_job(jid, status="queued", stage="queued", error=None)
        if wait:
            return self.run_job(jid)
        self._q.put(jid)
        return self.get_job(jid)

    def approve(self, jid: str, wait: bool = True) -> dict:
        j = self.get_job(jid)
        if j["status"] != "awaiting_review":
            raise Invalid("job is not awaiting review")
        opts = j["options"]; opts["review"] = False; opts["dry_run"] = False
        self._set_job(jid, options=opts, status="queued", stage="queued")
        return self.run_job(jid) if wait else (self._q.put(jid) or self.get_job(jid))

    # -------------------------------------------------------------------- run
    def run_job(self, jid: str) -> dict:
        j = self.get_job(jid)
        opts = j["options"]
        self._set_job(jid, status="running", stage="processing", attempts=j["attempts"] + 1, error=None)
        t0 = time.time()
        try:
            src = self.store.get_source(j["source_id"])
            if src["text"] is None:
                raise ExtractionError("source has no extracted text")
            if opts.get("review") or opts.get("dry_run"):
                try:
                    self._process(src, opts, dry=True)
                except _DryRun as d:
                    report = d.report
                    finally_reload = self.store._load_stats()
                    if finally_reload:
                        self.store.embedder.load_stats(finally_reload)
                    report["elapsed_s"] = round(time.time() - t0, 3)
                    status = "awaiting_review" if opts.get("review") and not opts.get("dry_run") else "done"
                    report["summary"] = ("DRY RUN (nothing written): " if status == "done" else "AWAITING REVIEW (nothing written): ") + report["summary"]
                    self._set_job(jid, status=status, stage="done", result=report)
                    return self.get_job(jid)
            report = self._process(src, opts, dry=False)
            report["elapsed_s"] = round(time.time() - t0, 3)
            self._set_job(jid, status="done", stage="done", result=report)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self._set_job(jid, status="failed", stage="error", error=msg,
                          result={"error": msg, "summary": f"failed: {msg}", "trace": traceback.format_exc()[-1500:]})
            # in-memory stats may have drifted if a tx was rolled back
            blob = self.store._load_stats()
            if blob:
                self.store.embedder.load_stats(blob)
        return self.get_job(jid)

    # ----------------------------------------------------------------- process
    def _process(self, src: dict, opts: dict, dry: bool) -> dict:
        report: dict = {"source_id": src["id"], "source": src["name"], "objects": [], "entities_created": [], "entities_linked": 0,
                        "edges": [], "claims": 0, "contradictions": [], "replacements": [], "warnings": [], "flags": src["flags"],
                        "counts": defaultdict(int)}
        with self.db.tx():
            self._run(src, opts, report)
            self._validate(report)
            report["counts"] = dict(report["counts"])
            c = report["counts"]
            report["summary"] = (f"{c.get('new', 0)} new, {c.get('update', 0)} updated, {c.get('evidence', 0)} evidence-only, "
                                 f"{c.get('unchanged', 0)} unchanged objects; {len(report['entities_created'])} entities created; "
                                 f"{len(report['edges'])} relationships; {report['claims']} claims; {len(report['contradictions'])} contradictions")
            if dry:
                raise _DryRun(report)
        return report

    def _validate(self, report):
        for o in report["objects"]:
            if o["decision"] in ("new", "update"):
                ob = self.store.get_object(o["id"])
                if not ob["title"].strip() or not ob["content"].strip():
                    raise ValidationError(f"empty object produced: {o['id']}")
                if ob["type"] not in OBJECT_TYPES:
                    raise ValidationError(f"invalid type {ob['type']}")
        for e in report["edges"]:
            if e["src"] == e["dst"]:
                raise ValidationError("self edge")

    # ------------------------------------------------------------------ core
    def _run(self, src: dict, opts: dict, report: dict):
        st, db = self.store, self.db
        text, smeta = src["text"], src["meta"]
        front = smeta.get("front") or {}
        origin = opts.get("origin") or "import"
        actor = opts.get("actor") or "ingest"
        uri = src["uri"]
        base_name = Path(src["name"]).stem
        title_hint = front.get("title") if isinstance(front.get("title"), str) else (smeta.get("title") or None)
        doc_title, sections = sectionize(text, title_hint or "")
        if not title_hint and doc_title == "Untitled":
            doc_title = base_name
        if not sections:
            raise ValidationError("no content could be structured from the source")
        project = opts.get("project") or (front.get("project") if isinstance(front.get("project"), str) else None)
        user_tags = list(opts.get("tags") or []) + (front.get("tags") if isinstance(front.get("tags"), list) else
                                                    [front["tags"]] if isinstance(front.get("tags"), str) else [])
        forced_type = opts.get("type") or (front.get("type") if front.get("type") in OBJECT_TYPES else None)
        event_time = parse_date(front.get("date") or front.get("created")) or find_date(text)
        verification = "unverified" if origin in ("user", "import") else "ai_extracted"
        if src["flags"]:
            report["warnings"].append(f"possible prompt-injection content flagged: {src['flags']} (treated strictly as data)")
            add_finding(db, kind="security", level="protected", severity="warning", confidence=0.7,
                        title=f"Suspicious instructions inside source “{src['name']}”",
                        explanation=f"Flags: {', '.join(src['flags'])}. The text is stored as data only; no instruction in it was acted upon.",
                        targets=[], payload={"source_id": src["id"]}, dedupe_key=f"sec:{src['id']}")

        # ---- build units
        units: list[Unit] = []
        single = len(sections) == 1
        if single:
            s0 = sections[0]
            cl = classify(doc_title, s0.body, [doc_title])
            units.append(Unit(doc_title, s0.body, f"{uri}::doc", [doc_title], 1, cl["type"], cl["confidence"], cl["cues"], False))
        else:
            whole = classify(doc_title, text[:3000], [doc_title])
            dtype = forced_type or ("project" if whole["type"] == "project" and whole["confidence"] >= 0.6 else "document")
            outline = "\n".join(f"- {s.title}" for s in sections[:40])
            units.append(Unit(doc_title, f"{summarize(text, 400)}\n\nSections:\n{outline}", f"{uri}::doc", [doc_title], 1, dtype, whole["confidence"] if dtype != "document" else 0.9,
                              [], True))
            used = set()
            for s in sections:
                cl = classify(s.title, s.body, s.path)
                t = s.title if s.title.strip().lower() not in GENERIC_TITLES and s.title.strip() != doc_title.strip() else f"{doc_title} — {s.title if s.title.strip() != doc_title.strip() else 'overview'}"
                if t in used:
                    t = f"{t} ({s.index + 1})"
                used.add(t)
                units.append(Unit(t, s.body, f"{uri}::{s.key}", s.path, s.level, cl["type"], cl["confidence"], cl["cues"], False))
        if forced_type and single:
            units[0].type, units[0].type_conf = forced_type, 1.0
        for u in units:
            if self.llm.name != "none" and not u.is_doc and not (forced_type and single):
                r = self.llm.classify(u.title, u.body)
                if r and r["confidence"] > u.type_conf:
                    u.type, u.type_conf = r["type"], r["confidence"]
                    u.cues = ["llm"]
        if doc_title and any(u.type == "project" for u in units if u.is_doc):
            project = project or doc_title

        # ---- project hub
        project_oid = None
        if project:
            project_oid = self._ensure_project(project, src, report)

        known = self._known_map()
        doc_unit = units[0] if (not single) else None
        new_ids: list[str] = []
        doc_cands: dict[str, Cand] = {}
        unit_cands: dict[int, dict[str, Cand]] = {}
        # ---- extract + dedupe + persist units
        for idx, u in enumerate(units):
            full = f"{u.title}\n{u.body}"
            flags = security.scan(full)
            cands = {} if u.is_doc else extract_entities(full, front.get("entities") if isinstance(front.get("entities"), list) else None)
            unit_cands[idx] = cands
            for k, c in cands.items():
                d = doc_cands.get(k)
                if d is None:
                    doc_cands[k] = Cand(c.display, c.key, c.count, c.strong, c.kind, set(c.aliases), c.mid_count, list(c.contexts))
                else:
                    d.count += c.count; d.strong = d.strong or c.strong; d.aliases |= c.aliases; d.contexts += c.contexts[:1]
            decision, existing = self._decide(u, src, uri)
            u.decision = decision
            tags_auto = [] if u.is_doc else keywords(u.body, self._df_lookup, 3, u.title)
            tags = list(dict.fromkeys([*user_tags, *inline_tags(u.body), *([] if u.is_doc else [])]))
            summary = summarize(u.body if not u.is_doc else text)
            imp = importance_for(u.type, u.level, opts.get("importance"))
            conf = round(min(0.95, 0.45 + 0.45 * u.type_conf), 3)
            extra = {"classification": {"confidence": u.type_conf, "cues": u.cues}}
            if flags:
                extra["security_flags"] = flags
            if decision == "new":
                o = st.create_object(type=u.type, title=truncate(u.title, 200), content=u.body, summary=summary, importance=imp,
                                     confidence=conf, verification=verification if origin != "ai" else "ai_extracted",
                                     project=project, tags=tags, source_id=src["id"], origin_key=u.origin_key, origin=origin,
                                     extra=extra, event_time=event_time, actor=actor, reason=f"ingest {src['name']}")
                if tags_auto:
                    st.add_tags(o["id"], tags_auto, actor="ai", origin="ai", log=False)
                u.oid = o["id"]
                new_ids.append(o["id"])
            elif decision == "update":
                st.update_object(existing["id"], {"title": truncate(u.title, 200), "content": u.body, "summary": summary, "type": u.type,
                                                  "extra": {**existing["extra"], **extra}}, actor=actor,
                                 reason=f"source changed ({src['name']})", kind="reingest")
                u.oid = existing["id"]
                st.add_evidence(u.oid, src["id"], "updated from new source version")
                new_ids.append(u.oid)
                self.db.exec("UPDATE claims SET status='superseded' WHERE object_id=? AND status='active'", (u.oid,))
            elif decision == "evidence":
                u.oid = existing["id"]
                st.add_evidence(u.oid, src["id"], f"duplicate content in “{src['name']}”")
            elif decision in ("unchanged", "conflict_user_edit"):
                u.oid = existing["id"]
                st.add_evidence(u.oid, src["id"], "same content")
                if decision == "conflict_user_edit":
                    add_finding(db, kind="source_conflict", level="conditional", severity="info", confidence=0.9,
                                title=f"Source changed but user-edited object preserved: {truncate(u.title, 60)}",
                                explanation="The imported source differs from the stored object, but the object has user edits, which are never overwritten automatically.",
                                targets=[u.oid], payload={"source_id": src["id"]}, dedupe_key=f"srcconf:{u.oid}:{src['id']}")
                    report["warnings"].append(f"user-edited object preserved: {u.title}")
            report["counts"][decision if decision != "conflict_user_edit" else "unchanged"] += 1
            report["objects"].append({"id": u.oid, "title": u.title, "type": u.type, "decision": decision, "classification_confidence": u.type_conf,
                                      "origin_key": u.origin_key, "duplicate_of": existing["id"] if decision == "evidence" else None})
            # claims for new/updated objects
            if decision in ("new", "update") and not u.is_doc:
                report["claims"] += self._persist_claims(u, src)

        # ---- structure edges: section → document, object → project
        parent = units[0].oid if not single else None
        for u in units:
            if not u.oid:
                continue
            if parent and u.oid != parent and u.decision in ("new", "update", "unchanged"):
                self._edge(report, u.oid, parent, "part_of", 1.0, f"Section “{truncate(u.title, 60)}” of document “{truncate(units[0].title, 60)}”.",
                           {"structure": True}, origin="import")
            if project_oid and u.oid != project_oid and (u.oid == parent or single):
                self._edge(report, u.oid, project_oid, "part_of", 0.85, f"Belongs to project “{project}” (from metadata/location).",
                           {"project": project}, origin="import")

        # ---- entities
        self._link_entities(units, unit_cands, doc_cands, known, src, report, project_oid, units[0].title)

        # ---- claim-level relations, then discovery
        for oid in new_ids:
            r = self.rel.detect_claim_relations(oid)
            for e in r["contradictions"]:
                report["contradictions"].append({"edge": e["id"], "src": e["src"], "dst": e["dst"], "explanation": e["explanation"]})
                report["edges"].append(self._edge_rep(e))
            for e in r["replacements"]:
                report["replacements"].append(e["id"])
                report["edges"].append(self._edge_rep(e))
        for e in self.rel.discover(new_ids, report):
            report["edges"].append(self._edge_rep(e))

        # ---- sections that disappeared from a changed source
        if any(u.decision == "update" for u in units):
            current = {u.origin_key for u in units}
            prev = self.db.all("SELECT id,title,origin_key FROM objects WHERE origin_key LIKE ? AND status='active'", (uri.replace("%", "") + "::%",))
            for r in prev:
                if r["origin_key"] not in current:
                    add_finding(db, kind="source_section_removed", level="conditional", severity="info", confidence=0.6,
                                title=f"Section no longer in source: {truncate(r['title'], 60)}",
                                explanation="A newer version of the source no longer contains this section. The object was kept (nothing is deleted automatically).",
                                targets=[r["id"]], payload={"source_id": src["id"]}, dedupe_key=f"gone:{r['id']}:{src['id']}")
                    report["warnings"].append(f"section removed from source, object kept: {r['title']}")

    # -------------------------------------------------------------- helpers
    def _df_lookup(self, tok: str) -> float:
        emb = self.store.embedder
        idx, _ = emb._slot("u:" + tok)
        return emb._idf(idx)

    def _edge_rep(self, e: dict) -> dict:
        return {"id": e["id"], "src": e["src"], "dst": e["dst"], "type": e["type"], "confidence": e["confidence"], "explanation": e["explanation"],
                "origin": e["origin"]}

    def _known_map(self) -> dict[str, str]:
        known: dict[str, str] = {}
        for r in self.db.all("SELECT id,title,type FROM objects WHERE status='active' AND type IN ('entity','concept','project')"):
            known.setdefault(name_key(r["title"]), r["id"])
        for r in self.db.all("SELECT a.alias_key, a.object_id FROM aliases a JOIN objects o ON o.id=a.object_id WHERE o.status='active' AND o.type IN ('entity','concept','project')"):
            known.setdefault(r["alias_key"], r["object_id"])
        for u in ("", ):
            known.pop(u, None)
        for r in self.db.all("SELECT term_key, alias_key FROM synonyms"):
            if r["term_key"] in known:
                known.setdefault(r["alias_key"], known[r["term_key"]])
        return {k: v for k, v in known.items() if len(k) >= 2 and k not in {name_key(w) for w in STOP_CAPS}}

    def _ensure_project(self, project: str, src: dict, report: dict) -> str:
        k = name_key(project)
        for r in self.db.all("SELECT id,title FROM objects WHERE type='project' AND status IN ('active','archived')"):
            if name_key(r["title"]) == k:
                return r["id"]
        for r in self.db.all("SELECT a.object_id FROM aliases a JOIN objects o ON o.id=a.object_id WHERE a.alias_key=? AND o.type='project'", (k,)):
            return r["object_id"]
        o = self.store.create_object(type="project", title=project, content=f"Project “{project}”. Auto-created from import metadata; knowledge objects that belong to this project link here.",
                                     summary=f"Project {project}", importance=0.7, confidence=0.6, verification="ai_extracted", project=project,
                                     origin_key=f"project::{k}", origin="ai", extra={"auto": True}, actor="ingest", reason="project hub", source_id=src["id"])
        report["objects"].append({"id": o["id"], "title": o["title"], "type": "project", "decision": "new", "origin_key": o["origin_key"], "auto": True})
        report["counts"]["new"] += 1
        return o["id"]

    def _user_edited(self, oid: str) -> bool:
        return self.db.one("SELECT 1 FROM object_versions WHERE object_id=? AND actor='user' AND change_kind NOT IN ('create') LIMIT 1", (oid,)) is not None

    def _decide(self, u: Unit, src: dict, uri: str):
        st = self.store
        ex = st.find_by_origin(u.origin_key)
        h = sha256_text(u.body)
        if ex:
            if ex["status"] == "deleted":
                return "new", None
            if ex["content_hash"] == h and ex["title"] == truncate(u.title, 200):
                return "unchanged", ex
            if self._user_edited(ex["id"]):
                return "conflict_user_edit", ex
            return "update", ex
        if u.is_doc:
            return "new", None
        vec = st.embedder.embed(st._embed_text(u.title, u.body))
        mine = shingles(u.body)
        for cid, sim in st.similar(vec, k=5):
            c = st.db.one("SELECT * FROM objects WHERE id=?", (cid,))
            if not c or c["type"] in ("entity", "document") or c["status"] != "active" or c["origin_key"] == u.origin_key:
                continue
            jac = jaccard(mine, shingles(c["content"]))
            if jac >= self.s.dup_jaccard or (sim >= self.s.dup_cosine and jac >= 0.5):
                return "evidence", st._obj(c, full=False)
        return "new", None

    def _persist_claims(self, u: Unit, src: dict) -> int:
        n = 0
        for pc in extract_claims(u.body):
            self.store.add_claim(object_id=u.oid, source_id=src["id"], text=pc.text, subject=pc.subject, predicate=pc.predicate, value=pc.value,
                                 subj_key=pc.subj_key, pred_key=pc.pred_key, val_key=pc.val_key, functional=pc.functional, polarity=pc.polarity,
                                 evidence=pc.text, confidence=pc.confidence)
            n += 1
        if u.type in ("fact", "research", "decision", "observation", "experience", "concept") and not u.is_doc:
            for s in key_sentences(u.body, 2):
                self.store.add_claim(object_id=u.oid, source_id=src["id"], text=s, evidence=s, confidence=0.45)
                n += 1
        return n

    def _edge(self, report, src, dst, type_, conf, expl, ev, origin="ai"):
        e = self.graph.add_edge(src, dst, type_, origin=origin, confidence=conf, explanation=expl, evidence=ev, actor="ingest")
        if e:
            report["edges"].append(self._edge_rep(e))
        return e

    # ------------------------------------------------------------- entities
    def _link_entities(self, units, unit_cands, doc_cands, known, src, report, project_oid, doc_title):
        st = self.store
        title_keys = {name_key(doc_title)} | {name_key(u.title) for u in units}
        resolved: dict[str, str | None] = {}
        weak_seen: dict[str, str] = {}

        def register(oid: str, c: Cand):
            known[c.key] = oid
            for al in c.aliases:
                known.setdefault(name_key(al), oid)

        def create(c: Cand) -> str:
            ctx = (c.contexts[0].strip() if c.contexts else "")
            content = c.display + (f" (also: {', '.join(sorted(c.aliases))})" if c.aliases else "") + (f". Context: “{truncate(ctx, 160)}”" if ctx else f". Mentioned in “{truncate(doc_title, 60)}”.")
            typ = "concept" if c.kind == "concept-term" else "entity"
            o = st.create_object(type=typ, title=c.display, content=content, summary=truncate(content, 200), importance=0.45, confidence=0.6,
                                 verification="ai_extracted", aliases=sorted(c.aliases), origin_key=f"entity::{c.key}", origin="ai",
                                 source_id=src["id"], extra={"kind": c.kind}, actor="ingest", reason="entity extracted")
            register(o["id"], c)
            report["entities_created"].append({"id": o["id"], "title": o["title"], "type": typ, "aliases": sorted(c.aliases)})
            return o["id"]

        def resolve(k: str) -> str | None:
            if k in resolved:
                return resolved[k]
            c = doc_cands[k]
            oid = None
            if k in known:
                oid = known[k]
                for al in c.aliases:
                    if name_key(al) not in known:
                        st.add_alias(oid, al, "ai"); known[name_key(al)] = oid
            else:
                for al in c.aliases:
                    if name_key(al) in known:
                        oid = known[name_key(al)]
                        st.add_alias(oid, c.display, "ai"); known[k] = oid
                        break
            if oid is None:
                if k in title_keys or len(k) < 3 or k.isdigit() or c.display in STOP_CAPS:
                    resolved[k] = None
                    return None
                if c.strong or c.count >= 2:
                    oid = create(c)
                else:
                    # weak (single mention): promote only when a *different* source also mentions it
                    other = self.db.one("SELECT object_id FROM entity_candidates WHERE term_key=? AND source_id!=? LIMIT 1", (k, src["id"]))
                    if other:
                        oid = create(c)
                        for r in self.db.all("SELECT DISTINCT object_id, display, count FROM entity_candidates WHERE term_key=? AND source_id!=?", (k, src["id"])):
                            if r["object_id"] != oid and self.db.one("SELECT 1 FROM objects WHERE id=? AND status='active'", (r["object_id"],)):
                                self._edge(report, r["object_id"], oid, "references", 0.55, f"Mentions “{c.display}” (entity promoted after appearing in two sources).",
                                           {"promoted": True}, origin="ai")
                    else:
                        weak_seen[k] = c.display
            resolved[k] = oid
            return oid

        for idx, u in enumerate(units):
            if u.is_doc or u.decision not in ("new", "update") or not u.oid:
                continue
            text = f"{u.title}\n{u.body}"
            counts: dict[str, tuple[int, bool, str]] = {}
            for k, c in unit_cands[idx].items():
                oid = resolve(k) if k in doc_cands else None
                if oid and oid != u.oid:
                    counts[oid] = (c.count, c.strong, c.display, (c.contexts or [""])[0])
                elif oid is None and k in weak_seen:
                    self.db.exec("INSERT OR IGNORE INTO entity_candidates(term_key,display,object_id,source_id,count) VALUES(?,?,?,?,?)",
                                 (k, c.display, u.oid, src["id"], c.count))
            for oid, n in find_known(text, known).items():
                if oid != u.oid and oid not in counts:
                    t = self.db.one("SELECT title FROM objects WHERE id=?", (oid,))
                    counts[oid] = (n, False, t["title"] if t else "", "")
            for oid, (n, strong, disp, ctx) in counts.items():
                if oid == project_oid:
                    continue
                conf = round(min(0.9, 0.5 + 0.1 * min(n, 4) + (0.1 if strong else 0)), 3)
                self._edge(report, u.oid, oid, "references", conf, f"Mentions “{disp}” {n}×.", {"quote": ctx, "source_id": src["id"]}, origin="ai")
                report["entities_linked"] += 1
