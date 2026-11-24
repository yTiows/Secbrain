"""External API (versioned, /api/v1) + static UI host.

Auth: `Authorization: Bearer brk_…` or `X-API-Key`. Scopes read < write < admin; keys may be restricted to projects/types,
enforced inside the engines (retrieval/graph/objects), not only at the route.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..app import Brain
from ..db import jdump
from ..findings import get_finding
from ..graph import GraphEngine
from ..ingest.extractors import SUPPORTED
from ..knowledge import Invalid, NotFound
from ..knowledge_types import EDGE_TYPES, OBJECT_TYPES
from ..retrieval import Scope

API = "/api/v1"
WEB_DIR = Path(__file__).resolve().parents[2] / "web"


# ------------------------------------------------------------------ schemas --
class SearchReq(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(10, ge=1, le=50)
    project: Optional[str] = None
    types: Optional[list[str]] = None


class ContextReq(SearchReq):
    k: int = Field(8, ge=1, le=30)
    token_budget: int = Field(2000, ge=200, le=32000)
    verbose: bool = False


class NoteReq(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    title: Optional[str] = Field(None, max_length=200)
    type: Optional[str] = None
    project: Optional[str] = Field(None, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=30)
    importance: Optional[float] = Field(None, ge=0, le=1)
    review: bool = False
    dry_run: bool = False
    wait: bool = True


class ObjectPatch(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    content: Optional[str] = Field(None, max_length=500_000)
    summary: Optional[str] = None
    type: Optional[str] = None
    importance: Optional[float] = Field(None, ge=0, le=1)
    confidence: Optional[float] = Field(None, ge=0, le=1)
    verification: Optional[str] = None
    project: Optional[str] = None
    tags: Optional[list[str]] = None
    aliases: Optional[list[str]] = None
    status: Optional[str] = None
    reason: str = "edit"


class EdgeReq(BaseModel):
    src: str
    dst: str
    type: str
    explanation: str = ""
    confidence: float = Field(1.0, ge=0, le=1)


class EdgePatch(BaseModel):
    verification: Optional[str] = None
    explanation: Optional[str] = None
    type: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0, le=1)
    status: Optional[str] = None


class FeedbackReq(BaseModel):
    kind: str
    target: str
    value: str = ""
    query: str = ""


class KeyReq(BaseModel):
    name: str
    scopes: list[str] = ["read"]
    projects: list[str] = []
    types: list[str] = []


class ProposalReq(BaseModel):
    object_id: str
    patch: dict
    rationale: str = ""


def create_app(brain: Brain, *, start_workers: bool = True, maintenance_interval_s: int = 3600, serve_ui: bool = True) -> FastAPI:
    import mimetypes
    mimetypes.add_type("application/manifest+json", ".webmanifest")
    app = FastAPI(title="Secbrain API", version="1.0.0", docs_url=f"{API}/docs", openapi_url=f"{API}/openapi.json")
    app.state.brain = brain
    b = brain

    if start_workers:
        from ..autopilot import Autopilot
        pilot = Autopilot(b, maintenance_interval=maintenance_interval_s or 1e12)
        app.state.autopilot = pilot

        @app.on_event("startup")
        def _start():
            b.ingest.start()
            pilot.start()

        @app.on_event("shutdown")
        def _stop():
            b.ingest.stop()
            pilot.stop()

    # ---------------------------------------------------------------- errors
    @app.exception_handler(NotFound)
    async def _nf(_, e):
        return JSONResponse({"error": "not_found", "detail": str(e)}, status_code=404)

    @app.exception_handler(Invalid)
    async def _inv(_, e):
        return JSONResponse({"error": "invalid", "detail": str(e)}, status_code=422)

    @app.exception_handler(KeyError)
    async def _ke(_, e):
        return JSONResponse({"error": "not_found", "detail": str(e)}, status_code=404)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith(API):
            resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        else:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------------ auth
    def principal(request: Request) -> dict:
        tok = None
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            tok = h[7:].strip()
        tok = tok or request.headers.get("x-api-key")
        p = b.keys.verify(tok)
        if not p:
            raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
        b.keys.touch(p["id"])
        return p

    def need(scope: str):
        order = {"read": 0, "write": 1, "admin": 2}

        def dep(p: dict = Depends(principal)) -> dict:
            have = max((order[s] for s in p["scopes"] if s in order), default=-1)
            if have < order[scope] and scope not in p["scopes"]:
                raise HTTPException(403, f"requires '{scope}' scope")
            return p
        return dep

    def scope_of(p: dict) -> Scope:
        return Scope(projects=p["projects"], types=p["types"])

    def visible(o: dict, p: dict) -> bool:
        return scope_of(p).allows(o)

    def guard(o: dict, p: dict) -> dict:
        if not visible(o, p):
            raise NotFound(o["id"])           # out-of-scope objects look non-existent
        return o

    # ----------------------------------------------------------------- health
    @app.get(f"{API}/health")
    def health():
        return {"status": "ok", "version": "1.0.0", "api": "v1"}

    @app.get(f"{API}/me")
    def me(p: dict = Depends(principal)):
        return {k: p[k] for k in ("id", "name", "scopes", "projects", "types")}

    @app.get(f"{API}/autopilot")
    def autopilot_status(p: dict = Depends(need("admin"))):
        pl = getattr(app.state, "autopilot", None)
        return {"inbox": str(b.settings.data_dir / "inbox"), "log": pl.log if pl else [], "backups": sorted(f.name for f in b.settings.backups_dir.glob("brain-*.sqlite"))}

    @app.get(f"{API}/meta")
    def meta(p: dict = Depends(need("read"))):
        return {"object_types": OBJECT_TYPES, "edge_types": {k: {"symmetric": v["symmetric"], "inverse": v["inverse"]} for k, v in EDGE_TYPES.items()},
                "supported_files": sorted(SUPPORTED)}

    # -------------------------------------------------------------- retrieval
    @app.post(f"{API}/search")
    def search(req: SearchReq, p: dict = Depends(need("read"))):
        return b.retrieval.search(req.query, k=req.k, project=req.project, types=req.types, scope=scope_of(p))

    @app.get(f"{API}/search")
    def search_get(q: str = Query(min_length=1, max_length=2000), k: int = Query(10, ge=1, le=50), project: Optional[str] = None, p: dict = Depends(need("read"))):
        return b.retrieval.search(q, k=k, project=project, scope=scope_of(p))

    @app.post(f"{API}/context")
    def context(req: ContextReq, p: dict = Depends(need("read"))):
        pkg = b.retrieval.context(req.query, token_budget=req.token_budget, k=req.k, project=req.project, types=req.types,
                                  scope=scope_of(p), verbose=req.verbose)
        pkg.pop("_full_tokens", None)
        return pkg

    @app.get(f"{API}/projects/{{name}}/context")
    def project_context(name: str, query: str = "overview of this project", token_budget: int = 1500, p: dict = Depends(need("read"))):
        if p["projects"] and name not in p["projects"]:
            raise NotFound(name)
        pkg = b.retrieval.context(query, token_budget=token_budget, project=name, scope=scope_of(p))
        pkg.pop("_full_tokens", None)
        return pkg

    # ---------------------------------------------------------------- objects
    @app.get(f"{API}/objects")
    def list_objects(type: Optional[str] = None, status: Optional[str] = "active", project: Optional[str] = None, tag: Optional[str] = None,
                     q: Optional[str] = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), order: str = "updated_at DESC",
                     p: dict = Depends(need("read"))):
        if order not in ("updated_at DESC", "created_at DESC", "importance DESC", "title ASC"):
            raise Invalid("bad order")
        sc = scope_of(p)
        rows = b.store.list_objects(type=type, status=status, project=project, tag=tag, q=q, limit=limit * 3 if sc.restricted else limit, offset=offset, order=order)
        rows = [o for o in rows if sc.allows(o)][:limit]
        return {"objects": [{k: o[k] for k in ("id", "type", "title", "summary", "project", "tags", "importance", "confidence", "verification", "status",
                                               "created_at", "updated_at", "version")} for o in rows], "count": len(rows)}

    @app.get(f"{API}/objects/{{oid}}")
    def get_object(oid: str, p: dict = Depends(need("read"))):
        o = guard(b.store.get_object(oid), p)
        o["relationships"] = [e for e in b.graph.edges_of(oid) if scope_of(p).allows(_row(e["other_id"]))]
        return o

    def _row(oid):
        r = b.db.one("SELECT id,type,project FROM objects WHERE id=?", (oid,))
        return dict(r) if r else {"project": None, "type": None}

    @app.patch(f"{API}/objects/{{oid}}")
    def patch_object(oid: str, patch: ObjectPatch, p: dict = Depends(need("write"))):
        guard(b.store.get_object(oid), p)
        data = patch.model_dump(exclude_unset=True)
        reason = data.pop("reason", "edit")
        return b.store.update_object(oid, data, actor="user", reason=reason)

    @app.delete(f"{API}/objects/{{oid}}")
    def delete_object(oid: str, purge: bool = False, confirm: Optional[str] = None, p: dict = Depends(need("admin"))):
        if purge:
            b.store.purge_object(oid, confirm=confirm or "")
            return {"purged": oid}
        return b.store.set_status(oid, "deleted", actor="user", reason="deleted via API")

    @app.post(f"{API}/objects/{{oid}}/{{action}}")
    def object_action(oid: str, action: str, p: dict = Depends(need("write"))):
        guard(b.store.get_object(oid), p)
        m = {"archive": "archived", "restore": "active", "unarchive": "active"}
        if action not in m:
            raise Invalid("action must be archive|restore")
        return b.store.set_status(oid, m[action], actor="user")

    @app.get(f"{API}/objects/{{oid}}/history")
    def history(oid: str, p: dict = Depends(need("read"))):
        guard(b.store.get_object(oid), p)
        return {"versions": b.store.versions(oid), "timeline": b.evolution.timeline(oid)}

    @app.post(f"{API}/objects/{{oid}}/restore-version/{{version}}")
    def restore_version(oid: str, version: int, p: dict = Depends(need("write"))):
        guard(b.store.get_object(oid), p)
        return b.store.restore_version(oid, version)

    # ---------------------------------------------------------- relationships
    @app.get(f"{API}/objects/{{oid}}/relationships")
    def rels(oid: str, p: dict = Depends(need("read"))):
        guard(b.store.get_object(oid), p)
        sc = scope_of(p)
        return {"relationships": [e for e in b.graph.edges_of(oid, include_rejected=True) if sc.allows(_row(e["other_id"]))]}

    @app.post(f"{API}/relationships")
    def create_rel(req: EdgeReq, p: dict = Depends(need("write"))):
        for i in (req.src, req.dst):
            guard(b.store.get_object(i), p)
        return b.graph.add_edge(req.src, req.dst, req.type, origin="user", confidence=req.confidence, explanation=req.explanation, actor="user")

    @app.patch(f"{API}/relationships/{{eid}}")
    def patch_rel(eid: str, patch: EdgePatch, p: dict = Depends(need("write"))):
        e = b.graph.get_edge(eid)
        guard(b.store.get_object(e["src"]), p); guard(b.store.get_object(e["dst"]), p)
        return b.graph.update_edge(eid, patch.model_dump(exclude_unset=True), actor="user")

    @app.delete(f"{API}/relationships/{{eid}}")
    def del_rel(eid: str, p: dict = Depends(need("write"))):
        e = b.graph.get_edge(eid)
        guard(b.store.get_object(e["src"]), p); guard(b.store.get_object(e["dst"]), p)
        return b.graph.delete_edge(eid, actor="user")

    # ------------------------------------------------------------------ graph
    @app.get(f"{API}/graph/overview")
    def g_overview(max_nodes: int = Query(300, ge=10, le=2000), types: Optional[str] = None, project: Optional[str] = None,
                   min_strength: float = 0.0, p: dict = Depends(need("read"))):
        sc = scope_of(p)
        return b.graph.overview(max_nodes=max_nodes, types=types.split(",") if types else None, project=project, min_strength=min_strength,
                                allow=(lambda n: sc.allows(n)) if sc.restricted else None)

    @app.get(f"{API}/graph/neighborhood/{{oid}}")
    def g_neigh(oid: str, hops: int = Query(2, ge=1, le=4), limit: int = Query(60, ge=5, le=300), min_strength: float = 0.0, types: Optional[str] = None,
                p: dict = Depends(need("read"))):
        guard(b.store.get_object(oid), p)
        sc = scope_of(p)
        r = b.graph.neighborhood(oid, hops=hops, limit=limit, min_strength=min_strength, types=types.split(",") if types else None,
                                 allow=(lambda n: sc.allows(n)) if sc.restricted else None)
        return r

    @app.get(f"{API}/graph/stats")
    def g_stats(p: dict = Depends(need("read"))):
        if p["projects"] or p["types"]:
            raise HTTPException(403, "graph statistics are not available to scoped keys")
        s = b.graph.stats()
        s["important"] = [{"id": i, "title": b.graph.graph_view()[0][i]["title"], "rank": round(v, 4)} for i, v in
                          sorted(b.graph.centrality().items(), key=lambda kv: -kv[1])[:10]]
        return s

    @app.get(f"{API}/graph/path")
    def g_path(a: str, b_: str = Query(alias="b"), p: dict = Depends(need("read"))):
        guard(b.store.get_object(a), p); guard(b.store.get_object(b_), p)
        path = b.graph.path(a, b_)
        return {"path": path}

    # ----------------------------------------------------------------- ingest
    def _clamp_project(project: Optional[str], p: dict) -> Optional[str]:
        if p["projects"]:
            if project and project not in p["projects"]:
                raise HTTPException(403, "project outside key scope")
            return project or p["projects"][0]
        return project

    @app.post(f"{API}/ingest/note")
    def ingest_note(req: NoteReq, p: dict = Depends(need("write"))):
        external = "admin" not in p["scopes"] and p["name"] != "ui"
        return _job_view(b.ingest.ingest_note(req.content, title=req.title, type=req.type, project=_clamp_project(req.project, p), tags=req.tags,
                                              importance=req.importance, wait=req.wait, review=req.review, dry_run=req.dry_run,
                                              origin="external" if external else "user", actor=f"api:{p['name']}"), p)

    @app.post(f"{API}/ingest/file")
    async def ingest_file(file: UploadFile = File(...), project: Optional[str] = Form(None), tags: str = Form(""), type: Optional[str] = Form(None),
                          review: bool = Form(False), dry_run: bool = Form(False), wait: bool = Form(True), uri: Optional[str] = Form(None),
                          p: dict = Depends(need("write"))):
        data = await file.read(b.settings.max_upload_bytes + 1)
        if len(data) > b.settings.max_upload_bytes:
            raise HTTPException(413, "file too large")
        external = "admin" not in p["scopes"] and p["name"] != "ui"
        j = b.ingest.ingest_bytes(file.filename or "upload.txt", data, uri=uri, project=_clamp_project(project, p), tags=[t for t in tags.split(",") if t.strip()],
                                  type=type or None, review=review, dry_run=dry_run, wait=wait, origin="external" if external else "import",
                                  actor=f"api:{p['name']}")
        return _job_view(j, p)

    def _job_view(j: dict, p: dict) -> dict:
        r = dict(j)
        res = dict(r.get("result") or {})
        if "admin" not in p["scopes"]:
            res.pop("trace", None)
        r["result"] = res
        return r

    @app.get(f"{API}/jobs")
    def jobs(limit: int = 50, status: Optional[str] = None, p: dict = Depends(need("write"))):
        return {"jobs": b.ingest.list_jobs(limit, status)}

    @app.get(f"{API}/jobs/{{jid}}")
    def job(jid: str, p: dict = Depends(need("write"))):
        return _job_view(b.ingest.get_job(jid), p)

    @app.post(f"{API}/jobs/{{jid}}/retry")
    def job_retry(jid: str, p: dict = Depends(need("write"))):
        return _job_view(b.ingest.retry(jid), p)

    @app.post(f"{API}/jobs/{{jid}}/approve")
    def job_approve(jid: str, p: dict = Depends(need("write"))):
        return _job_view(b.ingest.approve(jid), p)

    @app.get(f"{API}/sources")
    def sources(limit: int = 200, offset: int = 0, p: dict = Depends(need("admin"))):
        return {"sources": b.store.list_sources(limit, offset)}

    @app.get(f"{API}/sources/{{sid}}")
    def source(sid: str, p: dict = Depends(need("admin"))):
        s = b.store.get_source(sid)
        s["text"] = (s["text"] or "")[:20000]
        s.pop("file_path", None)
        return s

    @app.get(f"{API}/sources/{{sid}}/raw")
    def source_raw(sid: str, p: dict = Depends(need("admin"))):
        s = b.store.get_source(sid)
        data = b.store.read_source_bytes(sid)
        return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{Path(s["name"]).name}"'})

    # ----------------------------------------------------- proposals (external)
    @app.post(f"{API}/propose/update")
    def propose(req: ProposalReq, p: dict = Depends(need("write"))):
        guard(b.store.get_object(req.object_id), p)
        from ..knowledge import new_id
        pid = new_id("p")
        with b.db.tx():
            b.db.exec("INSERT INTO proposals(id,key_id,kind,target,payload,note,created_at) VALUES(?,?,?,?,?,?,?)",
                      (pid, p["id"], "update", req.object_id, jdump(req.patch), req.rationale, time.time()))
        return {"id": pid, "status": "open"}

    @app.get(f"{API}/proposals")
    def proposals(p: dict = Depends(need("admin"))):
        return {"proposals": [dict(r) for r in b.db.all("SELECT * FROM proposals ORDER BY created_at DESC LIMIT 200")]}

    @app.post(f"{API}/proposals/{{pid}}/{{action}}")
    def proposal_action(pid: str, action: str, p: dict = Depends(need("admin"))):
        r = b.db.one("SELECT * FROM proposals WHERE id=?", (pid,))
        if not r:
            raise NotFound(pid)
        if r["status"] != "open" or action not in ("apply", "reject"):
            raise Invalid("proposal not open or bad action")
        with b.db.tx():
            if action == "apply":
                patch = json.loads(r["payload"])
                allowed = {"title", "content", "summary", "tags", "aliases", "importance", "confidence", "project"}
                b.store.update_object(r["target"], {k: v for k, v in patch.items() if k in allowed}, actor=f"proposal:{pid}", reason=r["note"] or "external proposal", kind="proposal")
            b.db.exec("UPDATE proposals SET status=?, resolved_at=? WHERE id=?", ("applied" if action == "apply" else "rejected", time.time(), pid))
        return {"id": pid, "status": "applied" if action == "apply" else "rejected"}

    # ---------------------------------------------------------- intelligence
    @app.get(f"{API}/intelligence/summary")
    def intel(p: dict = Depends(need("admin"))):
        open_ = b.maintenance.list("open")
        by = {}
        for f in open_:
            by[f["kind"]] = by.get(f["kind"], 0) + 1
        last = b.db.get_meta("last_maintenance")
        return {"stats": b.store.stats(), "graph": b.graph.stats(), "open_findings": by, "last_maintenance": json.loads(last) if last else None,
                "recent_changes": b.store.list_changes(15), "gaps": b.evolution.gaps()[:15], "embedding_model": b.embedder.name,
                "llm": b.llm.name}

    @app.get(f"{API}/maintenance/findings")
    def findings(status: Optional[str] = "open", kind: Optional[str] = None, p: dict = Depends(need("admin"))):
        return {"findings": b.maintenance.list(status, kind)}

    @app.post(f"{API}/maintenance/run")
    def maintenance_run(only: Optional[list[str]] = None, p: dict = Depends(need("admin"))):
        out = b.maintenance.run(only)
        b.evolution.discover_concepts(); b.evolution.find_consolidations()
        return out

    @app.post(f"{API}/maintenance/findings/{{fid}}/apply")
    def finding_apply(fid: str, action: Optional[str] = None, p: dict = Depends(need("admin"))):
        return b.maintenance.apply(fid, actor="user", action=action)

    @app.post(f"{API}/maintenance/findings/{{fid}}/dismiss")
    def finding_dismiss(fid: str, p: dict = Depends(need("admin"))):
        return b.maintenance.dismiss(fid)

    @app.get(f"{API}/changes")
    def changes(limit: int = 100, p: dict = Depends(need("admin"))):
        return {"changes": b.store.list_changes(limit)}

    @app.post(f"{API}/changes/{{cid}}/undo")
    def undo(cid: str, p: dict = Depends(need("admin"))):
        return b.maintenance.changes.undo(cid)

    @app.post(f"{API}/feedback")
    def feedback(req: FeedbackReq, p: dict = Depends(need("write"))):
        if req.kind not in ("useful", "irrelevant"):
            if "admin" not in p["scopes"]:
                raise HTTPException(403, "only usefulness feedback is available to non-admin keys")
        return b.evolution.record_feedback(req.kind, req.target, req.value, req.query, actor=p["name"])

    @app.get(f"{API}/synonyms")
    def synonyms(p: dict = Depends(need("admin"))):
        return {"synonyms": [dict(r) for r in b.db.all("SELECT * FROM synonyms")]}

    @app.post(f"{API}/synonyms")
    def add_synonym(term: str, alias: str, p: dict = Depends(need("admin"))):
        from ..text import key
        with b.db.tx():
            b.db.exec("INSERT OR IGNORE INTO synonyms(term_key,alias_key) VALUES(?,?)", (key(term), key(alias)))
        return {"ok": True}

    # ------------------------------------------------------------------ vault
    @app.get(f"{API}/vault/export")
    def vault_export(p: dict = Depends(need("admin"))):
        return Response(b.vault.export_zip(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="secbrain-vault.zip"'})

    @app.post(f"{API}/vault/import")
    async def vault_import(file: UploadFile = File(...), folders_as_projects: bool = Form(False), p: dict = Depends(need("admin"))):
        data = await file.read(200 * 1024 * 1024 + 1)
        name = (file.filename or "").lower()
        try:
            if name.endswith(".zip"):
                return b.vault.import_zip(data, folders_as_projects=folders_as_projects)
            return b.vault.import_markdown(file.filename or "note.md", data)
        except ValueError as e:
            raise Invalid(str(e))

    @app.post(f"{API}/vault/backup")
    def vault_backup(p: dict = Depends(need("admin"))):
        return {"backup": Path(b.vault.backup(b.settings)).name}

    @app.post(f"{API}/vault/reindex")
    def reindex(p: dict = Depends(need("admin"))):
        return b.store.reindex()

    # ------------------------------------------------------------------- keys
    @app.get(f"{API}/keys")
    def keys(p: dict = Depends(need("admin"))):
        return {"keys": b.keys.list()}

    @app.post(f"{API}/keys")
    def key_create(req: KeyReq, p: dict = Depends(need("admin"))):
        return b.keys.create(req.name, req.scopes, req.projects, req.types)

    @app.delete(f"{API}/keys/{{kid}}")
    def key_revoke(kid: str, p: dict = Depends(need("admin"))):
        return {"revoked": b.keys.revoke(kid)}

    # --------------------------------------------------------------------- UI
    if serve_ui and WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")
    return app
