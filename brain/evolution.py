"""Knowledge evolution: consolidation, concept discovery, gaps, temporal history, learning feedback.

Nothing here fabricates knowledge: consolidations quote existing objects, gaps only report what is missing.
"""
from __future__ import annotations
import re
from collections import Counter, defaultdict

from .config import Settings
from .db import Database, jdump, jload, now
from .findings import add_finding
from .graph import GraphEngine
from .knowledge import KnowledgeStore, Invalid, NotFound
from .text import tokens, stem, truncate, key as name_key, STOPWORDS, words
from .ingest.enrich import GENERIC_WORDS


class Evolution:
    def __init__(self, db: Database, store: KnowledgeStore, graph: GraphEngine, settings: Settings):
        self.db, self.store, self.graph, self.s = db, store, graph, settings

    # ---------------------------------------------------------------- concepts
    def discover_concepts(self, min_objects: int = 3, min_sources: int = 2) -> list[dict]:
        """Recurring terms/bigrams across ≥3 objects from ≥2 sources that are not yet entities/concepts."""
        known = {name_key(r["title"]) for r in self.db.all("SELECT title FROM objects WHERE type IN ('entity','concept','project') AND status='active'")}
        known |= {r["alias_key"] for r in self.db.all("SELECT alias_key FROM aliases")}
        df: dict[str, set] = defaultdict(set)
        srcs: dict[str, set] = defaultdict(set)
        surface: dict[str, Counter] = defaultdict(Counter)
        objs = self.db.all("SELECT id,title,content,source_id FROM objects WHERE status='active' AND type NOT IN ('entity','concept','document','project')")
        for o in objs:
            ws = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", o["title"] + " " + o["content"])]
            ws = [w for w in ws if w not in STOPWORDS and w not in GENERIC_WORDS]
            seen = set()
            for i, w in enumerate(ws):
                for term in (w, f"{w} {ws[i + 1]}" if i + 1 < len(ws) else None):
                    if not term:
                        continue
                    k = " ".join(stem(x) for x in term.split())
                    if k in seen:
                        continue
                    seen.add(k)
                    df[k].add(o["id"]); srcs[k].add(o["source_id"]); surface[k][term] += 1
        n = max(1, len(objs))
        out = []
        for k, ids in df.items():
            if len(ids) < min_objects or len(srcs[k]) < min_sources or (n >= 20 and len(ids) / n > 0.3):
                continue
            disp = surface[k].most_common(1)[0][0]
            if name_key(disp) in known:
                continue
            bigram = " " in k
            score = len(ids) * (1.6 if bigram else 1.0)
            out.append({"term": disp, "key": k, "objects": sorted(ids), "sources": len(srcs[k]), "score": score})
        out.sort(key=lambda x: -x["score"])
        # prefer bigrams over their constituent unigrams
        picked, covered = [], set()
        for c in out:
            parts = set(c["key"].split())
            if len(parts) == 1 and any(parts <= set(p["key"].split()) and set(p["objects"]) >= set(c["objects"]) for p in picked):
                continue
            picked.append(c)
        res = []
        for c in picked[:15]:
            f = add_finding(self.db, kind="concept_candidate", level="conditional", severity="info", confidence=min(0.9, 0.4 + 0.1 * len(c["objects"])),
                            title=f"Recurring concept: “{c['term']}”",
                            explanation=f"“{c['term']}” appears in {len(c['objects'])} objects from {c['sources']} sources but is not a tracked concept. Creating it links them.",
                            targets=c["objects"][:12], payload={"term": c["term"]}, dedupe_key=f"concept:{c['key']}")
            if f:
                res.append(f)
        return res

    def apply_concept(self, f: dict, actor: str) -> str:
        term = f["payload"]["term"]
        cites = []
        for oid in f["targets"][:6]:
            o = self.store.get_object(oid)
            cites.append(f"- “{o['title']}”: {truncate(o['summary'] or o['content'], 140)}")
        c = self.store.create_object(type="concept", title=term.title() if term.islower() else term,
                                     content=f"Recurring concept “{term}” discovered across the vault. Where it appears:\n" + "\n".join(cites),
                                     summary=f"Concept “{term}” recurring in {len(f['targets'])} objects.", importance=0.5, confidence=0.55,
                                     verification="ai_extracted", origin="ai", origin_key=f"concept::{name_key(term)}", actor=actor, reason="concept discovery",
                                     extra={"discovered": True})
        for oid in f["targets"]:
            self.graph.add_edge(oid, c["id"], "references", origin="ai", confidence=0.6, explanation=f"Mentions the recurring concept “{term}”.",
                                actor=actor, log=False)
        return self.store.log_change(actor, "concept_created", c["id"], f"created concept “{term}”", {"op": "delete_object", "object_id": c["id"]})

    # ----------------------------------------------------------- consolidation
    def find_consolidations(self, min_fragments: int = 4) -> list[dict]:
        """Many small fragments about one entity from ≥2 sources, with no overview object → propose a consolidated note."""
        res = []
        rows = self.db.all("SELECT e.dst ent, e.src obj FROM edges e JOIN objects o ON o.id=e.src JOIN objects d ON d.id=e.dst "
                           "WHERE e.type='references' AND e.status='active' AND e.verification!='rejected' AND o.status='active' AND d.status='active' "
                           "AND d.type IN ('entity','concept') AND o.type NOT IN ('document','entity','concept')")
        by = defaultdict(set)
        for r in rows:
            by[r["ent"]].add(r["obj"])
        for ent, objs in by.items():
            if len(objs) < min_fragments:
                continue
            qm = ",".join("?" * len(objs))
            nsrc = self.db.one(f"SELECT COUNT(DISTINCT source_id) c FROM objects WHERE id IN ({qm})", list(objs))["c"]
            if nsrc < 2:
                continue
            if self.db.one("SELECT 1 FROM edges e JOIN objects o ON o.id=e.src WHERE e.dst=? AND e.type='derived_from' LIMIT 1", (ent,)):
                continue
            eo = self.store.get_object(ent)
            if self.db.one("SELECT 1 FROM objects WHERE origin_key=?", (f"consolidation::{ent}",)):
                continue
            f = add_finding(self.db, kind="consolidation", level="conditional", severity="info", confidence=0.6,
                            title=f"Consolidate {len(objs)} fragments about “{eo['title']}”",
                            explanation=f"{len(objs)} objects from {nsrc} sources discuss “{eo['title']}” with no overview. A consolidated note would quote them; originals stay untouched.",
                            targets=sorted(objs)[:15], payload={"entity": ent}, dedupe_key=f"consol:{ent}")
            if f:
                res.append(f)
        return res

    def apply_consolidation(self, f: dict, actor: str) -> str:
        ent = self.store.get_object(f["payload"]["entity"])
        parts, srcs = [], set()
        for oid in f["targets"]:
            o = self.store.get_object(oid)
            src = ", ".join(sorted({s["name"] for s in o["sources"] if s["role"] == "primary"})) or "unknown source"
            parts.append(f"- ({o['type']}) {truncate(o['summary'] or o['content'], 200)}  — [{o['title']}] ⟵ {src}")
        c = self.store.create_object(type="document", title=f"Overview: {ent['title']}", content=f"Consolidated view of what the vault records about {ent['title']} (quoted, not rewritten):\n" + "\n".join(parts),
                                     summary=f"Consolidation of {len(f['targets'])} fragments about {ent['title']}", importance=0.55, confidence=0.6, verification="ai_extracted",
                                     origin="ai", origin_key=f"consolidation::{ent['id']}", actor=actor, reason="consolidation", extra={"consolidation": True})
        for oid in f["targets"]:
            self.graph.add_edge(c["id"], oid, "derived_from", origin="ai", confidence=0.8, explanation="Quoted in this consolidated overview; original preserved.", actor=actor, log=False)
        self.graph.add_edge(c["id"], ent["id"], "references", origin="ai", confidence=0.8, explanation="Overview of this entity.", actor=actor, log=False)
        return self.store.log_change(actor, "consolidation_created", c["id"], f"consolidated {len(f['targets'])} fragments about {ent['title']}", {"op": "delete_object", "object_id": c["id"]})

    # -------------------------------------------------------------------- gaps
    def gaps(self) -> list[dict]:
        """Report what is missing. Never fabricates the missing knowledge."""
        out = []
        for o in self.db.all("SELECT id,title,project FROM objects WHERE type='question' AND status='active'"):
            answered = self.db.one("SELECT 1 FROM edges WHERE (dst=? OR src=?) AND type IN ('explains','supports','updates') AND status='active' AND verification!='rejected' LIMIT 1", (o["id"], o["id"]))
            if not answered:
                out.append({"kind": "unanswered_question", "target": o["id"], "title": o["title"], "project": o["project"],
                            "detail": f"No object explains or answers the question “{truncate(o['title'], 80)}”."})
        for o in self.db.all("SELECT id,title,project FROM objects WHERE type='hypothesis' AND status='active'"):
            ev = self.db.one("SELECT 1 FROM edges WHERE (dst=? OR src=?) AND type IN ('supports','contradicts') AND status='active' AND verification!='rejected' LIMIT 1", (o["id"], o["id"]))
            if not ev:
                out.append({"kind": "untested_hypothesis", "target": o["id"], "title": o["title"], "project": o["project"],
                            "detail": f"Hypothesis “{truncate(o['title'], 80)}” has no supporting or contradicting evidence recorded."})
        for o in self.db.all("SELECT id,title,project,content FROM objects WHERE type='decision' AND status='active'"):
            if not re.search(r"\b(because|rationale|reason|since|due to|trade-?off|compared|so that)\b", o["content"], re.I):
                out.append({"kind": "decision_without_rationale", "target": o["id"], "title": o["title"], "project": o["project"],
                            "detail": f"Decision “{truncate(o['title'], 80)}” records no rationale."})
        for p in self.db.all("SELECT id,title FROM objects WHERE type='project' AND status='active'"):
            n = self.db.one("SELECT COUNT(*) c FROM objects WHERE project=? AND status='active' AND type NOT IN ('entity','document')", (p["title"],))["c"]
            kinds = {r["type"] for r in self.db.all("SELECT DISTINCT type FROM objects WHERE project=? AND status='active'", (p["title"],))}
            if n < 3:
                out.append({"kind": "thin_project", "target": p["id"], "title": p["title"], "project": p["title"],
                            "detail": f"Project “{p['title']}” has only {n} knowledge objects."})
            elif "decision" not in kinds and "procedure" not in kinds:
                out.append({"kind": "project_missing_types", "target": p["id"], "title": p["title"], "project": p["title"],
                            "detail": f"Project “{p['title']}” records no decisions or procedures."})
        return out

    # ---------------------------------------------------------------- timeline
    def timeline(self, oid: str) -> dict:
        """Temporal evolution around an object: predecessors/successors via updates/replaces/contradicts + the object's own version history.
        History is preserved, never flattened into a single 'current' answer."""
        o = self.store.get_object(oid)
        seen, chain = {oid}, []
        frontier = [oid]
        while frontier:
            cur = frontier.pop()
            for e in self.graph.edges_of(cur, types=["updates", "replaces", "contradicts"]):
                other = e["other_id"]
                if other in seen:
                    continue
                seen.add(other)
                frontier.append(other)
        events = []
        for sid in seen:
            x = self.store.get_object(sid)
            events.append({"id": sid, "title": x["title"], "type": x["type"], "status": x["status"], "when": x.get("event_time") or x["created_at"],
                           "summary": truncate(x["summary"] or x["content"], 200), "source": ([s["name"] for s in x["sources"] if s["role"] == "primary"] or [None])[0]})
        events.sort(key=lambda e: e["when"])
        rels = []
        for sid in seen:
            for e in self.graph.edges_of(sid, "out", types=["updates", "replaces", "contradicts"]):
                if e["other_id"] in seen:
                    rels.append({"from": e["src"], "to": e["dst"], "type": e["type"], "why": truncate(e["explanation"], 200)})
        return {"object": oid, "events": events, "relations": rels,
                "versions": [{"version": v["version"], "when": v["created_at"], "actor": v["actor"], "reason": v["reason"], "kind": v["change_kind"]} for v in self.store.versions(oid)]}

    # ---------------------------------------------------------------- feedback
    def record_feedback(self, kind: str, target: str, value: str = "", query: str = "", actor: str = "user") -> dict:
        """Feedback is stored separately from verified facts. Effects are deliberately small and bounded:
        - useful/irrelevant: ranking prior of at most ±8%, only after ≥2 signals (see retrieval._feedback_prior)
        - classification/importance: applied to that object only, as a versioned user edit
        - edge: verify/reject that relationship (a rejection is a permanent tombstone for AI rediscovery)
        Core heuristics/parameters are never modified by feedback."""
        if kind not in ("useful", "irrelevant", "classification", "importance", "edge"):
            raise Invalid("kind must be useful|irrelevant|classification|importance|edge")
        with self.db.tx():
            self.db.exec("INSERT INTO feedback(ts,kind,target,value,query,actor) VALUES(?,?,?,?,?,?)", (now(), kind, target, value, query, actor))
            applied = None
            if kind == "classification":
                self.store.update_object(target, {"type": value, "verification": "user_verified"}, actor="user", reason="user classification feedback")
                applied = f"type set to {value}"
            elif kind == "importance":
                self.store.update_object(target, {"importance": float(value)}, actor="user", reason="user importance feedback")
                applied = f"importance set to {value}"
            elif kind == "edge":
                if value == "verify":
                    self.graph.verify_edge(target)
                elif value == "reject":
                    self.graph.reject_edge(target)
                else:
                    raise Invalid("edge feedback value must be verify|reject")
                applied = f"edge {value}"
        return {"recorded": True, "applied": applied}
