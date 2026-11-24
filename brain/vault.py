"""Obsidian-compatible Markdown vault: export (projection), import (through the same versioned paths), backups.

The database stays authoritative. Exported files carry the stable object `id` in frontmatter so a round trip
produces new *versions*, never duplicates. Generated sections (relationships/backlinks) are fenced by markers and ignored on import.
"""
from __future__ import annotations
import io
import re
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath

from .db import now
from .graph import GraphEngine
from .ingest.extractors import parse_frontmatter
from .ingest.pipeline import IngestionEngine
from .knowledge import KnowledgeStore, NotFound
from .knowledge_types import EDGE_TYPES, OBJECT_TYPES
from .text import slugify, truncate

START, END = "<!-- brain:generated:start -->", "<!-- brain:generated:end -->"
GEN_RX = re.compile(re.escape(START) + r".*?" + re.escape(END) + r"\n?", re.S)


def _yaml_scalar(v) -> str:
    s = str(v)
    if re.search(r"[:#\[\]{},&*!|>'\"%@`\n]", s) or s != s.strip() or s == "":
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'
    return s


def _yaml_list(items) -> str:
    return "[" + ", ".join(_yaml_scalar(i) for i in items) + "]"


class VaultManager:
    def __init__(self, store: KnowledgeStore, graph: GraphEngine, ingest: IngestionEngine):
        self.store, self.graph, self.ingest = store, graph, ingest
        self.db = store.db

    # ----------------------------------------------------------------- export
    @staticmethod
    def filename(o: dict) -> str:
        return f"{slugify(o['title'], 60)} ~{o['id'][2:8]}"

    def render(self, o: dict, names: dict[str, str]) -> str:
        srcs = [s for s in o["sources"] if s["role"] == "primary"]
        fm = ["---", f"id: {o['id']}", f"type: {o['type']}", f"title: {_yaml_scalar(o['title'])}"]
        if o["tags"]:
            fm.append(f"tags: {_yaml_list(o['tags'])}")
        if o["aliases"]:
            fm.append(f"aliases: {_yaml_list(o['aliases'])}")
        if o["project"]:
            fm.append(f"project: {_yaml_scalar(o['project'])}")
        fm += [f"importance: {o['importance']}", f"confidence: {o['confidence']}", f"verification: {o['verification']}", f"status: {o['status']}",
               f"created: {time.strftime('%Y-%m-%d', time.gmtime(o['created_at']))}", f"updated: {time.strftime('%Y-%m-%d', time.gmtime(o['updated_at']))}",
               f"version: {o['version']}"]
        if o.get("event_time"):
            fm.append(f"date: {time.strftime('%Y-%m-%d', time.gmtime(o['event_time']))}")
        if srcs:
            fm.append(f"source: {_yaml_scalar(srcs[0]['name'])}")
            fm.append(f"source_file: {_yaml_scalar('attachments/' + srcs[0]['sha256'][:10] + '-' + srcs[0]['name'])}")
        fm.append("---")
        body = [f"# {o['title']}", "", o["content"].strip(), ""]
        gen = [START, "## Relationships"]
        rels = self.graph.edges_of(o["id"])
        out = [e for e in rels if e["direction"] == "out"]
        inc = [e for e in rels if e["direction"] == "in"]
        for e in out:
            gen.append(f"- {e['type'].replace('_', ' ')} [[{names.get(e['other_id'], e['other_title'])}|{e['other_title']}]]"
                       + (f" — {truncate(e['explanation'], 160)}" if e["explanation"] else "") + f" _({e['origin']}, {e['verification']}, {e['confidence']:.2f})_")
        if inc:
            gen.append("")
            gen.append("## Backlinks")
            for e in inc:
                gen.append(f"- [[{names.get(e['other_id'], e['other_title'])}|{e['other_title']}]] ({e['label'].replace('_', ' ')})")
        gen.append(END)
        return "\n".join(fm + [""] + body + gen) + "\n"

    def export_to_dir(self, dest: Path, include_deleted: bool = False) -> dict:
        dest = Path(dest)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        rows = self.db.all("SELECT id FROM objects WHERE status != 'deleted' ORDER BY type, title") if not include_deleted else self.db.all("SELECT id FROM objects")
        objs = [self.store.get_object(r["id"]) for r in rows]
        names = {o["id"]: self.filename(o) for o in objs}
        n = 0
        for o in objs:
            d = dest / ("entities" if o["type"] in ("entity",) else o["type"])
            d.mkdir(exist_ok=True)
            (d / (names[o["id"]] + ".md")).write_text(self.render(o, names), encoding="utf-8")
            n += 1
        att = dest / "attachments"
        att.mkdir(exist_ok=True)
        na = 0
        for s in self.store.list_sources(limit=100000):
            full = self.store.get_source(s["id"])
            p = Path(full["file_path"] or "")
            if p.exists():
                shutil.copyfile(p, att / f"{s['sha256'][:10]}-{s['name']}")
                na += 1
        (dest / "README.md").write_text(
            f"# Secbrain vault export\n\nExported {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC. {n} notes, {na} original attachments.\n\n"
            "Open this folder as an Obsidian vault. Frontmatter `id` keeps notes linked to the database: re-importing an edited file creates a new "
            "version of the same object. Text between the `brain:generated` markers is generated and ignored on import.\n", encoding="utf-8")
        return {"notes": n, "attachments": na}

    def export_zip(self) -> bytes:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            info = self.export_to_dir(Path(td) / "vault")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for p in sorted((Path(td) / "vault").rglob("*")):
                    if p.is_file():
                        z.write(p, p.relative_to(Path(td) / "vault").as_posix())
            return buf.getvalue()

    # ----------------------------------------------------------------- import
    def import_markdown(self, relpath: str, data: bytes, *, project: str | None = None, actor: str = "import") -> dict:
        """Import one Markdown file. Files exported by Secbrain (frontmatter id) update that object (new version);
        everything else flows through the normal ingestion pipeline."""
        text = data.decode("utf-8", "replace")
        fm, body = parse_frontmatter(text)
        oid = fm.get("id")
        if isinstance(oid, str) and oid.startswith("k_"):
            try:
                cur = self.store.get_object(oid)
            except NotFound:
                cur = None
            if cur:
                body = GEN_RX.sub("", body).strip()
                body = re.sub(r"^# .*\n+", "", body, count=1).strip()
                patch = {}
                if body and body != cur["content"].strip():
                    patch["content"] = body
                if isinstance(fm.get("title"), str) and fm["title"] != cur["title"]:
                    patch["title"] = fm["title"]
                if fm.get("type") in OBJECT_TYPES and fm["type"] != cur["type"]:
                    patch["type"] = fm["type"]
                tags = fm.get("tags")
                if isinstance(tags, list) and sorted(str(t).lower() for t in tags) != cur["tags"]:
                    patch["tags"] = [str(t) for t in tags]
                for k in ("importance", "confidence"):
                    if isinstance(fm.get(k), (int, float)) and abs(fm[k] - cur[k]) > 1e-9 and 0 <= fm[k] <= 1:
                        patch[k] = float(fm[k])
                if not patch:
                    return {"id": oid, "action": "unchanged"}
                self.store.update_object(oid, patch, actor=actor, reason=f"vault import {relpath}", kind="import")
                self._wikilinks(oid, body)
                return {"id": oid, "action": "updated", "fields": sorted(patch)}
        j = self.ingest.ingest_bytes(PurePosixPath(relpath).name, data, uri=relpath, project=project, wait=True, origin="import")
        ids = [o["id"] for o in (j.get("result") or {}).get("objects", []) if o.get("id") and o.get("decision") in ("new", "update")]
        for oid2 in ids:
            self._wikilinks(oid2, self.store.get_object(oid2)["content"])
        return {"job": j["id"], "action": j["status"], "objects": len(ids), "error": j.get("error")}

    def _wikilinks(self, oid: str, body: str):
        """Explicit [[links]] are user-authored: they become verified `references` edges when the target exists."""
        for m in re.finditer(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]+)?\]\]", body):
            target = m.group(1).strip()
            tid = None
            mm = re.search(r"~(\w{6})$", target)
            if mm:
                r = self.db.one("SELECT id FROM objects WHERE id=? AND status!='deleted'", ("k_" + mm.group(1),))
                tid = r["id"] if r else None
            if not tid:
                r = self.db.one("SELECT id FROM objects WHERE LOWER(title)=LOWER(?) AND status='active' ORDER BY type='entity' DESC LIMIT 1", (target,))
                tid = r["id"] if r else None
            if tid and tid != oid and not self.graph.is_rejected(oid, tid):
                self.graph.add_edge(oid, tid, "references", origin="import", confidence=1.0, verification="verified", explanation="Explicit wiki-link in the note.", actor="import", log=False)

    def import_directory(self, root: Path, *, folders_as_projects: bool = False) -> dict:
        root = Path(root)
        res = {"files": 0, "created_or_updated": 0, "unchanged": 0, "failed": []}
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in (".md", ".markdown", ".txt", ".json", ".pdf", ".docx"):
                continue
            rel = p.relative_to(root).as_posix()
            if rel.startswith((".obsidian/", "attachments/")) or rel == "README.md" and "Secbrain vault export" in p.read_text("utf-8", "ignore")[:80]:
                continue
            proj = rel.split("/")[0] if folders_as_projects and "/" in rel else None
            try:
                if p.suffix.lower() in (".md", ".markdown"):
                    r = self.import_markdown(rel, p.read_bytes(), project=proj)
                else:
                    j = self.ingest.ingest_bytes(p.name, p.read_bytes(), uri=rel, project=proj, origin="import")
                    r = {"action": j["status"], "error": j.get("error")}
                res["files"] += 1
                if r.get("error") or r.get("action") == "failed":
                    res["failed"].append({"file": rel, "error": r.get("error")})
                elif r.get("action") in ("unchanged",):
                    res["unchanged"] += 1
                else:
                    res["created_or_updated"] += 1
            except Exception as e:
                res["failed"].append({"file": rel, "error": f"{type(e).__name__}: {e}"})
        return res

    def import_zip(self, data: bytes, **kw) -> dict:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if sum(i.file_size for i in z.infolist()) > 200 * 1024 * 1024 or len(z.infolist()) > 20000:
                    raise ValueError("archive too large")
                for info in z.infolist():
                    target = (root / info.filename).resolve()
                    if root not in target.parents and target != root:      # zip-slip guard
                        raise ValueError(f"unsafe path in archive: {info.filename}")
                z.extractall(root)
            return self.import_directory(root, **kw)

    # ------------------------------------------------------------------ backup
    def backup(self, settings) -> str:
        dest = settings.backups_dir / f"brain-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
        self.db.backup(dest)
        return str(dest)
