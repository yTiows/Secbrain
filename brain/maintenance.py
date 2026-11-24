"""Autonomous knowledge maintenance: inspect → explain → decide autonomy → apply (versioned, reversible) → record.

Autonomy policy
  SAFE         auto-applied: tags, summaries, reindex, suggested connections, unsupported-claim flags, duplicate *identification*
  CONDITIONAL  needs approval (or `auto_conditional` + confidence ≥ threshold): merge, reclassify, supersede, contradiction resolution
  PROTECTED    never automatic: source integrity problems, security flags, deletion of sources/large amounts of knowledge
"""
from __future__ import annotations
import threading
import time
from collections import defaultdict

import numpy as np

from .changes import ChangeManager
from .config import Settings
from .db import Database, jdump, jload, now
from .findings import add_finding, get_finding, list_findings
from .graph import GraphEngine
from .ingest.classify import classify
from .ingest.enrich import summarize, keywords
from .ingest.relationships import RelationshipDiscoverer
from .knowledge import KnowledgeStore, Invalid, NotFound
from .text import jaccard, shingles, key as name_key, truncate, sha256_text

SKIP_TYPES = ("entity", "document")


class MaintenanceEngine:
    def __init__(self, db: Database, store: KnowledgeStore, graph: GraphEngine, settings: Settings, rel: RelationshipDiscoverer):
        self.db, self.store, self.graph, self.s, self.rel = db, store, graph, settings, rel
        self.changes = ChangeManager(db, store, graph)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ driver
    def run(self, only: list[str] | None = None) -> dict:
        checks = {
            "reindex": self.check_reindex, "summaries_tags": self.check_incomplete, "relationships": self.check_missing_relationships,
            "duplicates": self.check_duplicates, "contradictions": self.check_contradictions, "outdated": self.check_outdated,
            "classification": self.check_classification, "claims": self.check_unsupported_claims, "isolated": self.check_isolated,
            "sources": self.check_sources, "stale": self.check_stale,
        }
        t0 = time.time()
        summary: dict = {}
        for name, fn in checks.items():
            if only and name not in only:
                continue
            try:
                summary[name] = fn()
            except Exception as e:                      # one failing inspection must not stop the others
                summary[name] = {"error": f"{type(e).__name__}: {e}"}
        if self.s.auto_conditional:
            for f in list_findings(self.db, "open"):
                if f["level"] == "conditional" and f["confidence"] >= self.s.conditional_min_confidence and f["kind"] in ("duplicate", "outdated"):
                    try:
                        self.apply(f["id"], actor="maintenance-auto")
                        summary.setdefault("auto_conditional_applied", []).append(f["id"])
                    except Exception:
                        pass
        summary["elapsed_s"] = round(time.time() - t0, 3)
        self.db.set_meta("last_maintenance", jdump({"at": now(), "summary": summary}))
        return summary

    def start_scheduler(self, interval_s: int = 3600):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval_s):
                try:
                    self.run()
                except Exception:
                    pass
        self._thread = threading.Thread(target=loop, name="maintenance", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---------------------------------------------------------------- findings
    def list(self, status="open", kind=None):
        return list_findings(self.db, status, kind)

    def dismiss(self, fid: str, actor="user") -> dict:
        with self.db.tx():
            self.db.exec("UPDATE findings SET status='dismissed', resolved_at=? WHERE id=?", (now(), fid))
            self.store.log_change(actor, "finding_dismissed", fid, "dismissed finding", {"op": "none"})
        return get_finding(self.db, fid)

    def apply(self, fid: str, actor: str = "user", action: str | None = None, confirm: str | None = None) -> dict:
        f = get_finding(self.db, fid)
        if f["status"] not in ("open",):
            raise Invalid(f"finding is {f['status']}")
        if f["level"] == "protected":
            raise Invalid("PROTECTED finding: requires manual handling outside automated apply")
        handlers = {"duplicate": self._apply_merge, "outdated": self._apply_supersede, "classification": self._apply_reclassify,
                    "contradiction": self._apply_resolve, "concept_candidate": self._apply_concept, "consolidation": self._apply_consolidation}
        h = handlers.get(f["kind"])
        if not h:
            raise Invalid(f"finding kind {f['kind']!r} has no apply action")
        with self.db.tx():
            ch = h(f, actor, action)
            self.db.exec("UPDATE findings SET status=?, resolved_at=? WHERE id=?", ("auto_applied" if actor.startswith("maintenance") else "applied", now(), fid))
        return {"finding": get_finding(self.db, fid), "change_id": ch}

    # ----------------------------------------------------------------- checks
    def _active(self, skip=SKIP_TYPES):
        q = ",".join("?" * len(skip))
        return [self.store._obj(r, False) for r in self.db.all(f"SELECT * FROM objects WHERE status='active' AND type NOT IN ({q})", skip)]

    def check_reindex(self) -> dict:
        n = self.db.one("SELECT COUNT(*) c FROM objects WHERE status!='deleted'")["c"]
        seen = self.store.embedder.n_docs
        if n and abs(n - seen) / n > 0.15:
            r = self.store.reindex()
            self.store.log_change("maintenance", "reindex", "all", f"rebuilt derived indexes ({r['objects']} objects)", {"op": "none"})
            return {"reindexed": r["objects"]}
        return {"reindexed": 0}

    def check_incomplete(self) -> dict:
        """SAFE: generate missing summaries and keyword tags."""
        fixed_s = fixed_t = flagged = 0
        for o in self._active():
            if not (o["summary"] or "").strip() and len(o["content"]) > 40:
                before = self.store._snapshot(o["id"])
                self.store.update_object(o["id"], {"summary": summarize(o["content"])}, actor="maintenance", reason="generate summary", kind="maintenance")
                fixed_s += 1
            tags = [t["tag"] for t in self.db.all("SELECT tag FROM tags WHERE object_id=?", (o["id"],))]
            if not tags and len(o["content"]) > 80:
                ks = keywords(o["content"], None, 3, o["title"])
                if ks:
                    self.store.add_tags(o["id"], ks, actor="maintenance", origin="ai")
                    fixed_t += 1
            if len(o["content"].strip()) < 20:
                if add_finding(self.db, kind="incomplete", level="safe", severity="info", confidence=0.6, title=f"Very short object: {truncate(o['title'], 60)}",
                               explanation="Content is under 20 characters; it may lack enough information to be useful.", targets=[o["id"]],
                               dedupe_key=f"incomplete:{o['id']}"):
                    flagged += 1
        return {"summaries_added": fixed_s, "tags_added": fixed_t, "flagged_incomplete": flagged}

    def check_missing_relationships(self) -> dict:
        before = self.db.one("SELECT COUNT(*) c FROM edges WHERE status='active'")["c"]
        ids = [o["id"] for o in self._active()]
        created = self.rel.discover(ids)
        after = self.db.one("SELECT COUNT(*) c FROM edges WHERE status='active'")["c"]
        return {"suggested_edges_added": after - before, "scanned": len(ids)}

    def check_duplicates(self) -> dict:
        objs = self._active()
        if len(objs) < 2:
            return {"candidates": 0}
        ids, mat = self.store.embedding_matrix()
        pos = {i: n for n, i in enumerate(ids)}
        by_id = {o["id"]: o for o in objs}
        found = 0
        cand_ids = [i for i in ids if i in by_id]
        for a in cand_ids:
            sims = mat @ mat[pos[a]]
            for j in np.argsort(-sims)[:6]:
                b = ids[j]
                if b == a or b not in by_id or a > b:
                    continue
                oa, ob = by_id[a], by_id[b]
                if oa["source_id"] and oa["source_id"] == ob["source_id"]:
                    continue
                same_title = name_key(oa["title"]) == name_key(ob["title"])
                jac = jaccard(shingles(oa["content"]), shingles(ob["content"]))
                sim = float(sims[j])
                if (sim >= 0.85 and jac >= 0.6) or (same_title and sim >= 0.6 and jac >= 0.3) or jac >= 0.75:
                    if self.graph.is_rejected(a, b):
                        continue
                    conf = round(min(0.98, 0.5 * sim + 0.5 * max(jac, 0.9 if same_title else 0)) * (1.0 if oa["type"] == ob["type"] else 0.9), 3)
                    if add_finding(self.db, kind="duplicate", level="conditional", severity="info", confidence=conf,
                                   title=f"Possible duplicates: {truncate(oa['title'], 40)} / {truncate(ob['title'], 40)}",
                                   explanation=(f"Same type ({oa['type']})" if oa["type"] == ob["type"] else f"Types differ ({oa['type']}/{ob['type']})") + f", cosine {sim:.2f}, text overlap {jac:.2f}" + (", identical titles" if same_title else "") +
                                               ". Merging keeps both histories and sources.",
                                   targets=[a, b], payload={"cosine": sim, "jaccard": jac}, dedupe_key=f"dup:{a}:{b}"):
                        found += 1
        return {"candidates": found}

    def check_contradictions(self) -> dict:
        n = 0
        for o in self._active():
            r = self.rel.detect_claim_relations(o["id"])
            n += len(r["contradictions"]) + len(r["replacements"])
        return {"new_relations": n}

    def check_outdated(self) -> dict:
        n = 0
        for r in self.db.all("SELECT e.id eid, e.src, e.dst, e.type, e.confidence, e.explanation FROM edges e JOIN objects a ON a.id=e.src JOIN objects b ON b.id=e.dst "
                             "WHERE e.type IN ('replaces','updates') AND e.status='active' AND e.verification!='rejected' AND a.status='active' AND b.status='active'"):
            if add_finding(self.db, kind="outdated", level="conditional", severity="warning", confidence=float(r["confidence"]),
                           title="Possibly outdated knowledge", explanation=r["explanation"] or "A newer object replaces/updates this one.",
                           targets=[r["dst"], r["src"]], payload={"edge_id": r["eid"], "supersede": r["dst"]}, dedupe_key=f"outdated:{r['dst']}:{r['src']}"):
                n += 1
        return {"new_findings": n}

    def check_classification(self) -> dict:
        n = 0
        for o in self._active():
            if o["verification"] == "user_verified" or o["origin"] == "user":
                continue
            if self.db.one("SELECT 1 FROM object_versions WHERE object_id=? AND actor='user' AND change_kind!='create' LIMIT 1", (o["id"],)):
                continue
            if (o["origin_key"] or "").startswith(("project::", "entity::")) or len(o["content"]) < 80:
                continue
            c = classify(o["title"], o["content"], [o["title"]])
            if c["type"] != o["type"] and c["confidence"] >= 0.75 and c["type"] not in ("fact", "observation"):
                if add_finding(self.db, kind="classification", level="conditional", severity="info", confidence=c["confidence"],
                               title=f"Reclassify “{truncate(o['title'], 50)}”: {o['type']} → {c['type']}",
                               explanation=f"Re-classification with current rules gives {c['type']} (cues: {', '.join(c['cues'][:3])}).",
                               targets=[o["id"]], payload={"from": o["type"], "to": c["type"]}, dedupe_key=f"cls:{o['id']}:{c['type']}"):
                    n += 1
        return {"new_findings": n}

    def check_unsupported_claims(self) -> dict:
        """SAFE: a claim whose evidence is not found in its source text cannot be traced; mark it so it is not presented as supported."""
        n = 0
        for c in self.db.all("SELECT c.id, c.evidence, c.object_id, s.text, c.verification FROM claims c JOIN sources s ON s.id=c.source_id WHERE c.status='active' AND c.evidence!=''"):
            norm = lambda t: " ".join((t or "").split()).lower()
            ev = norm(c["evidence"])
            if c["verification"] == "ai_extracted" and ev and ev.replace("**", "").replace("`", "") not in norm(c["text"]).replace("**", "").replace("`", ""):
                with self.db.tx():
                    self.db.exec("UPDATE claims SET status='unsupported' WHERE id=?", (c["id"],))
                    self.store.log_change("maintenance", "claim_unsupported", c["id"], "claim evidence not found in source", {"op": "claim_status", "claim_id": c["id"], "status": "active"})
                add_finding(self.db, kind="unsupported_claim", level="safe", severity="warning", confidence=0.8,
                            title="Claim not supported by its source text", explanation=f"Evidence “{truncate(c['evidence'], 80)}” is not present in the source.",
                            targets=[c["object_id"]], dedupe_key=f"unsup:{c['id']}")
                n += 1
        return {"flagged": n}

    def check_isolated(self) -> dict:
        iso = [i for i in self.graph.isolated() if self.db.one("SELECT type FROM objects WHERE id=?", (i,))["type"] not in ("entity",)]
        n = 0
        for i in iso:
            o = self.store.get_object(i)
            if add_finding(self.db, kind="isolated", level="safe", severity="info", confidence=0.7, title=f"Isolated knowledge: {truncate(o['title'], 60)}",
                           explanation="This object has no relationships; it can only be found by direct search.", targets=[i],
                           dedupe_key=f"iso:{i}"):
                n += 1
        comps = self.graph.components()
        small = [c for c in comps[1:] if 2 <= len(c) <= 3]
        for c in small:
            add_finding(self.db, kind="isolated_cluster", level="safe", severity="info", confidence=0.6, title=f"Small disconnected cluster ({len(c)} objects)",
                        explanation="These objects connect to each other but not to the rest of the vault.", targets=c, dedupe_key="cluster:" + ":".join(sorted(c)))
        return {"isolated": len(iso), "new_findings": n, "small_clusters": len(small)}

    def check_sources(self) -> dict:
        from pathlib import Path
        import hashlib
        bad = 0
        for s in self.db.all("SELECT id,name,sha256,file_path FROM sources"):
            p = Path(s["file_path"] or "")
            problem = None
            if not p.exists():
                problem = "original file is missing"
            elif hashlib.sha256(p.read_bytes()).hexdigest() != s["sha256"]:
                problem = "original file content no longer matches its recorded hash"
            if problem and add_finding(self.db, kind="broken_source", level="protected", severity="critical", confidence=1.0,
                                       title=f"Source integrity problem: {s['name']}", explanation=problem + ". Restore from backup.", targets=[],
                                       payload={"source_id": s["id"]}, dedupe_key=f"src:{s['id']}:{problem[:10]}"):
                bad += 1
        return {"broken": bad}

    def check_stale(self) -> dict:
        cutoff = now() - self.s.stale_days * 86400
        n = 0
        for o in self.db.all("SELECT id,title,updated_at FROM objects WHERE status='active' AND type='project' AND updated_at<?", (cutoff,)):
            if add_finding(self.db, kind="stale", level="safe", severity="info", confidence=0.4, title=f"Project not updated for {self.s.stale_days}+ days: {o['title']}",
                           explanation="Project information may be outdated.", targets=[o["id"]], dedupe_key=f"stale:{o['id']}"):
                n += 1
        return {"new_findings": n}

    # --------------------------------------------------------------- appliers
    def merge(self, survivor: str, loser: str, actor: str = "user") -> str:
        """Merge `loser` into `survivor`: nothing is deleted. Loser is archived with a pointer; edges, claims, sources, tags, aliases move.
        Fully reversible (see ChangeManager.unmerge)."""
        st = self.store
        with self.db.tx():
            S, L = st.get_object(survivor), st.get_object(loser)
            snapshot = st._snapshot(loser)
            moved_edges = {}
            for e in self.db.all("SELECT * FROM edges WHERE (src=? OR dst=?) AND status='active'", (loser, loser)):
                other = e["dst"] if e["src"] == loser else e["src"]
                if other == survivor:
                    moved_edges[e["id"]] = {"src": e["src"], "dst": e["dst"], "status": e["status"]}
                    self.db.exec("UPDATE edges SET status='deleted', updated_at=? WHERE id=?", (now(), e["id"]))
                    continue
                ns, nd = (survivor, e["dst"]) if e["src"] == loser else (e["src"], survivor)
                dup = self.db.one("SELECT 1 FROM edges WHERE src=? AND dst=? AND type=?", (ns, nd, e["type"]))
                moved_edges[e["id"]] = {"src": e["src"], "dst": e["dst"], "status": e["status"]}
                if dup:
                    self.db.exec("UPDATE edges SET status='deleted', updated_at=? WHERE id=?", (now(), e["id"]))
                else:
                    self.db.exec("UPDATE edges SET src=?, dst=?, updated_at=? WHERE id=?", (ns, nd, now(), e["id"]))
            claims = [r["id"] for r in self.db.all("SELECT id FROM claims WHERE object_id=?", (loser,))]
            self.db.exec("UPDATE claims SET object_id=? WHERE object_id=?", (survivor, loser))
            added_sources = []
            for r in self.db.all("SELECT source_id FROM object_sources WHERE object_id=?", (loser,)):
                if st.add_evidence(survivor, r["source_id"], f"merged from “{L['title']}”"):
                    added_sources.append(r["source_id"])
            added_tags = [t for t in L["tags"] if t not in S["tags"]]
            if added_tags:
                for t in added_tags:
                    self.db.exec("INSERT OR IGNORE INTO tags(object_id,tag,origin) VALUES(?,?,?)", (survivor, t, "merge"))
            added_aliases = []
            for a in L["aliases"] + [L["title"]]:
                if name_key(a) != name_key(S["title"]) and st._add_alias_nolock(survivor, a, "merge"):
                    added_aliases.append(name_key(a))
            st.update_object(loser, {"status": "archived", "extra": {**L["extra"], "merged_into": survivor}}, actor=actor, reason=f"merged into {survivor}", kind="merge", log_change=False)
            st._index(survivor, observe=False)
            st.update_object(survivor, {"importance": max(S["importance"], L["importance"])}, actor=actor, reason=f"absorbed {loser}", kind="merge", log_change=False)
            return st.log_change(actor, "merge", survivor, f"merged “{L['title']}” into “{S['title']}”",
                                 {"op": "unmerge", "loser": loser, "survivor": survivor, "loser_snapshot": snapshot, "moved_edges": moved_edges,
                                  "moved_claims": claims, "added_sources": added_sources, "added_tags": added_tags, "added_aliases": added_aliases})

    def _apply_merge(self, f, actor, action):
        a, b = f["targets"][:2]
        A, B = self.store.get_object(a), self.store.get_object(b)
        order = sorted([A, B], key=lambda o: (o["verification"] != "user_verified", -o["importance"], o["created_at"]))
        return self.merge(order[0]["id"], order[1]["id"], actor)

    def _apply_supersede(self, f, actor, action):
        oid = f["payload"].get("supersede") or f["targets"][0]
        o = self.store.get_object(oid)
        snap = self.store._snapshot(oid)
        self.store.update_object(oid, {"status": "superseded"}, actor=actor, reason="superseded by newer knowledge", kind="supersede", log_change=False)
        return self.store.log_change(actor, "supersede", oid, f"marked “{o['title']}” superseded", {"op": "restore_snapshot", "object_id": oid, "snapshot": snap})

    def _apply_reclassify(self, f, actor, action):
        oid = f["targets"][0]
        snap = self.store._snapshot(oid)
        self.store.update_object(oid, {"type": f["payload"]["to"]}, actor=actor, reason="reclassified by maintenance", kind="reclassify", log_change=False)
        return self.store.log_change(actor, "reclassify", oid, f"{f['payload']['from']}→{f['payload']['to']}", {"op": "restore_snapshot", "object_id": oid, "snapshot": snap})

    def _apply_resolve(self, f, actor, action):
        """Resolution: the newer object wins; older is marked superseded, a `replaces` edge records why, contradiction edge is closed.
        `action` may be 'keep:<object_id>' to choose explicitly."""
        a, b = f["targets"][:2]
        A, B = self.store.get_object(a), self.store.get_object(b)
        if action and action.startswith("keep:"):
            win = action.split(":", 1)[1]
            if win not in (a, b):
                raise Invalid("keep:<id> must name one of the conflicting objects")
        else:
            win = a if (A.get("event_time") or A["created_at"]) >= (B.get("event_time") or B["created_at"]) else b
        lose = b if win == a else a
        ops = []
        snap = self.store._snapshot(lose)
        self.store.update_object(lose, {"status": "superseded"}, actor=actor, reason="contradiction resolved", kind="resolve", log_change=False)
        ops.append({"op": "restore_snapshot", "object_id": lose, "snapshot": snap})
        e = self.graph.add_edge(win, lose, "replaces", origin="user" if actor == "user" else "ai", confidence=0.9, verification="verified",
                                explanation="Resolved contradiction: this object is the accepted version.", actor=actor, log=False)
        if e:
            ops.append({"op": "edge_status", "edge_id": e["id"], "status": "deleted"})
        eid = f["payload"].get("edge_id")
        if eid:
            cur = self.graph.get_edge(eid)
            self.graph.update_edge(eid, {"status": "deleted"}, actor=actor, log=False)
            ops.append({"op": "edge_restore", "edge": {k: cur[k] for k in ("id", "src", "dst", "type", "verification", "explanation", "confidence", "status")}})
        return self.store.log_change(actor, "resolve_contradiction", win, f"kept “{self.store.get_object(win)['title']}”, superseded “{self.store.get_object(lose)['title']}”",
                                     {"op": "composite", "ops": ops})

    def _apply_concept(self, f, actor, action):
        from .evolution import Evolution
        return Evolution(self.db, self.store, self.graph, self.s).apply_concept(f, actor)

    def _apply_consolidation(self, f, actor, action):
        from .evolution import Evolution
        return Evolution(self.db, self.store, self.graph, self.s).apply_consolidation(f, actor)
