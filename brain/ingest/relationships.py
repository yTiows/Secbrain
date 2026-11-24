"""Relationship discovery & conflict detection.

Principle: similarity alone never creates an edge between unrelated notes. A relationship needs *evidence*:
rare shared entities, an explicit mention, a recognised cue phrase, a claim-level link, or very high semantic similarity.
Entities shared by a large fraction of the vault (e.g. "Python") are down-weighted to zero (IDF damping).
"""
from __future__ import annotations
import math
import re
from collections import defaultdict

from ..config import Settings
from ..db import Database
from ..findings import add_finding
from ..graph import GraphEngine
from ..knowledge import KnowledgeStore
from ..text import key as name_key, tokens, sentences, truncate

DEPENDS = re.compile(r"\b(depends? on|depending on|requires?|relies on|built (?:on|upon|with)|based on|needs?|uses?)\b", re.I)
IMPLEMENTS = re.compile(r"\b(implements?|implementation of|realizes?|puts into practice)\b", re.I)
SUPPORTS = re.compile(r"\b(supports?|confirms?|validates?|evidence for|consistent with|backs up|corroborates?)\b", re.I)
EXPLAINS = re.compile(r"\b(explains?|explanation of|clarifies|describes why)\b", re.I)
UPDATE_CUES = re.compile(r"\b(now|updated?|changed|as of|instead|no longer|new default|revised|supersed\w+|replac\w+)\b", re.I)

EVIDENCE_TYPES = {"research", "observation", "experience", "fact"}
CLAIMED_TYPES = {"hypothesis", "decision", "idea"}
SKIP_CANDIDATE = {"entity", "document"}


class RelationshipDiscoverer:
    def __init__(self, db: Database, store: KnowledgeStore, graph: GraphEngine, settings: Settings):
        self.db, self.store, self.graph, self.s = db, store, graph, settings

    # ---------------------------------------------------------------- helpers
    def _entity_stats(self) -> tuple[dict[str, int], int]:
        links = {r["dst"]: r["c"] for r in self.db.all(
            "SELECT e.dst, COUNT(*) c FROM edges e JOIN objects o ON o.id=e.dst WHERE e.type='references' AND e.status='active' "
            "AND e.verification!='rejected' AND o.type IN ('entity','concept') GROUP BY e.dst")}
        n = self.db.one("SELECT COUNT(*) c FROM objects WHERE status='active' AND type NOT IN ('entity','document')")["c"]
        return links, max(n, 1)

    def entity_weight(self, links: int, n_obj: int) -> float:
        if links >= 6 and links / n_obj > 0.25:
            return 0.0                                   # generic entity: appears across a large part of the vault
        return max(0.0, math.log((n_obj + 1) / (links + 1)))

    def _ents_of(self, oid: str) -> dict[str, str]:
        return {r["dst"]: r["title"] for r in self.db.all(
            "SELECT e.dst, o.title FROM edges e JOIN objects o ON o.id=e.dst WHERE e.src=? AND e.type='references' "
            "AND e.status='active' AND e.verification!='rejected' AND o.type IN ('entity','concept')", (oid,))}

    def _fts_candidates(self, text: str, k: int = 6, exclude: str = "") -> list[str]:
        toks = list(dict.fromkeys(t for t in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", text.lower())))[:12]
        if not toks:
            return []
        q = " OR ".join(f'"{t}"' for t in toks)
        try:
            rows = self.db.all("SELECT object_id FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, 4.0, 1.0, 2.0, 2.0) LIMIT ?", (q, k + 1))
        except Exception:
            return []
        return [r["object_id"] for r in rows if r["object_id"] != exclude][:k]

    # -------------------------------------------------------------- discovery
    def discover(self, new_ids: list[str], report: dict | None = None) -> list[dict]:
        created: list[dict] = []
        ent_links, n_obj = self._entity_stats()
        for oid in new_ids:
            try:
                n = self.store.get_object(oid)
            except KeyError:
                continue
            if n["status"] != "active" or n["type"] in SKIP_CANDIDATE:
                continue
            created += self._discover_one(n, ent_links, n_obj)
        return created

    def _discover_one(self, n: dict, ent_links: dict[str, int], n_obj: int) -> list[dict]:
        oid = n["id"]
        n_ents = self._ents_of(oid)
        vec = self.store.embedding_of(oid)
        cand: dict[str, float] = {}
        if vec is not None:
            for cid, sim in self.store.similar(vec, k=12, exclude={oid}):
                cand[cid] = sim
        for eid in n_ents:
            for r in self.db.all("SELECT src FROM edges WHERE dst=? AND type='references' AND status='active' AND verification!='rejected' LIMIT 40", (eid,)):
                cand.setdefault(r["src"], 0.0)
        for cid in self._fts_candidates(n["title"] + " " + n["content"][:400], exclude=oid):
            cand.setdefault(cid, 0.0)
        cand.pop(oid, None)
        scored = []
        for cid, sim in cand.items():
            c = self.db.one("SELECT * FROM objects WHERE id=?", (cid,))
            if not c or c["status"] != "active" or c["type"] in SKIP_CANDIDATE:
                continue
            c = self.store._obj(c, full=False)
            if self.graph.is_rejected(oid, cid):
                continue
            if sim == 0.0:
                cv = self.store.embedding_of(cid)
                sim = float(vec @ cv) if vec is not None and cv is not None else 0.0
            r = self._evaluate(n, c, sim, n_ents, ent_links, n_obj)
            if r:
                scored.append(r)
        scored.sort(key=lambda r: -r["confidence"])
        out = []
        for r in scored[: self.s.rel_max_per_object]:
            e = self.graph.add_edge(r["src"], r["dst"], r["type"], origin="ai", confidence=r["confidence"],
                                    explanation=r["explanation"], evidence=r["evidence"], actor="ingest")
            if e:
                out.append(e)
        return out

    def _evaluate(self, n: dict, c: dict, sim: float, n_ents: dict, ent_links, n_obj) -> dict | None:
        c_ents = self._ents_of(c["id"])
        shared = set(n_ents) & set(c_ents)
        w = {e: self.entity_weight(ent_links.get(e, 0), n_obj) for e in shared}
        specific = {e: x for e, x in w.items() if x > 0}
        w_sum = sum(specific.values())
        ent = 1 - math.exp(-w_sum / 2.5)
        cosn = max(0.0, min(1.0, (sim - 0.10) / 0.5))
        proj = 1.0 if n["project"] and n["project"] == c["project"] else 0.0
        sibling = n["source_id"] and n["source_id"] == c["source_id"]
        cues, rtype = [], None
        # explicit mention of the other object's title
        ck = name_key(c["title"])
        ntext = n["title"] + "\n" + n["content"]
        mentions_title = len(c["title"]) >= 6 and ck and ck in name_key(ntext) and not sibling
        if mentions_title:
            cues.append("mentions-title")
            rtype = "references"
        # cue phrases in sentences that mention the shared entities or the other title
        ent_names = [n_ents[e] for e in specific] + ([c["title"]] if mentions_title else [])
        keys = [name_key(x) for x in ent_names if x]
        for s in sentences(n["content"]):
            sk = name_key(s)
            if keys and any(k and k in sk for k in keys):
                if IMPLEMENTS.search(s) and n["type"] in ("procedure", "instruction", "project", "experience", "decision"):
                    cues.append("implements-cue"); rtype = "implements"; break
                if DEPENDS.search(s) and re.search(r"\b(depends? on|depending on|requires?|relies on|built (?:on|upon|with)|based on)\b", s, re.I):
                    cues.append("depends-cue"); rtype = "depends_on"; break
                if SUPPORTS.search(s) and n["type"] in EVIDENCE_TYPES | {"experience"}:
                    cues.append("supports-cue"); rtype = "supports"; break
                if EXPLAINS.search(s) and n["type"] in ("concept", "research", "document", "fact"):
                    cues.append("explains-cue"); rtype = "explains"; break
        same_title = ck == name_key(n["title"]) and n["type"] == c["type"] and not sibling
        if same_title and sim >= 0.3:
            cues.append("same-title")
            rtype = rtype or "updates"
        # evidence rule: a relationship needs grounded evidence, not just topical similarity
        grounded = (ent >= 0.3 and sim >= 0.15) or (len(specific) >= 2 and sim >= 0.08) or sim >= 0.5 or bool(cues)
        if sibling and not (cues or sim >= 0.5):
            grounded = False
        if not grounded:
            return None
        conf = 0.55 * ent + 0.35 * cosn + 0.10 * proj + (0.18 if cues else 0.0)
        if sibling:
            conf *= 0.85
        conf = round(min(conf, 0.95), 3)
        if conf < self.s.rel_min_confidence:
            return None
        src, dst = n["id"], c["id"]
        if rtype is None:
            if n["type"] in EVIDENCE_TYPES and c["type"] in CLAIMED_TYPES and ent >= 0.3 and sim >= 0.2:
                rtype, conf = "supports", round(conf * 0.85, 3)
            elif ent < 0.3 and sim >= 0.5:
                rtype = "similar_to"
            else:
                rtype = "related_to"
        if rtype == "updates":
            # direction: newer updates older
            tn = n.get("event_time") or n["created_at"]; tc = c.get("event_time") or c["created_at"]
            if tc > tn:
                src, dst = dst, src
        if conf < self.s.rel_min_confidence:
            return None
        shared_names = [n_ents[e] for e in sorted(specific, key=lambda e: -specific[e])][:4]
        parts = []
        if shared_names:
            parts.append("both involve " + ", ".join(shared_names))
        if "mentions-title" in cues:
            parts.append(f"text explicitly mentions “{truncate(c['title'], 60)}”")
        for cu in cues:
            if cu.endswith("-cue"):
                parts.append(f"cue phrase suggests {rtype.replace('_', ' ')}")
        if "same-title" in cues:
            parts.append("same title and type in a different source (newer version updates older)")
        if proj:
            parts.append(f"same project “{n['project']}”")
        parts.append(f"semantic similarity {sim:.2f}")
        return {"src": src, "dst": dst, "type": rtype, "confidence": conf,
                "explanation": ("; ".join(parts)).capitalize() + ".",
                "evidence": {"shared_entities": shared_names, "cosine": round(sim, 3), "cues": cues,
                             "source_id": n["source_id"], "entity_weight": round(w_sum, 3)}}

    # ----------------------------------------------------- claim-level analysis
    def detect_claim_relations(self, oid: str) -> dict:
        """Contradictions, negations and replacements between claims of `oid` and the rest of the vault."""
        res = {"contradictions": [], "replacements": []}
        mine = self.db.all("SELECT * FROM claims WHERE object_id=? AND status='active'", (oid,))
        me = self.store.get_object(oid)
        my_t = me.get("event_time") or me["created_at"]
        for c in mine:
            if c["pred_key"] == "replaced" and c["val_key"]:
                res["replacements"] += self._replacement(me, c)
                continue
            if not c["subj_key"] or not c["pred_key"]:
                continue
            rows = self.db.all(
                "SELECT c.*, o.title otitle, o.status ostatus, o.created_at ocreated, o.event_time oevent FROM claims c JOIN objects o ON o.id=c.object_id "
                "WHERE c.subj_key=? AND c.pred_key=? AND c.object_id!=? AND c.status='active' AND o.status IN ('active','superseded')",
                (c["subj_key"], c["pred_key"], oid))
            for o in rows:
                conflict = False
                if c["functional"] and o["functional"] and c["val_key"] != o["val_key"] and c["polarity"] == o["polarity"] == 1:
                    conflict = True
                elif c["val_key"] == o["val_key"] and c["polarity"] != o["polarity"]:
                    conflict = True
                if not conflict or self.graph.is_rejected(oid, o["object_id"], "contradicts"):
                    continue
                numeric = bool(re.match(r"^[\d.]+", c["val_key"] or "")) and bool(re.match(r"^[\d.]+", o["val_key"] or ""))
                conf = 0.82 if numeric or c["polarity"] != o["polarity"] else 0.7
                o_t = o["oevent"] or o["ocreated"]
                newer = me["title"] if my_t > o_t else o["otitle"]
                expl = (f"Conflicting claims about “{c['subject']}” ({c['predicate']}): "
                        f"“{truncate(me['title'], 50)}” says “{truncate(c['value'] or '', 50)}” while “{truncate(o['otitle'], 50)}” says "
                        f"“{truncate(o['value'] or '', 50)}”; “{truncate(newer, 50)}” is newer."
                        + (" Possible update (cue words present)." if UPDATE_CUES.search(c["text"]) else ""))
                e = self.graph.add_edge(oid, o["object_id"], "contradicts", origin="ai", confidence=conf, explanation=expl,
                                        evidence={"claim_a": c["id"], "claim_b": o["id"], "quote_a": c["evidence"] or c["text"],
                                                  "quote_b": o["evidence"] or o["text"]}, actor="ingest")
                if e:
                    a, b = sorted([oid, o["object_id"]])
                    add_finding(self.db, kind="contradiction", level="conditional", severity="warning", confidence=conf,
                                title=f"Conflicting claims: {truncate(c['subject'] or '', 50)} — {c['predicate']}",
                                explanation=expl, targets=[a, b],
                                payload={"edge_id": e["id"], "claims": [c["id"], o["id"]], "newer": oid if my_t > o_t else o["object_id"]},
                                dedupe_key=f"contra:{a}:{b}:{c['subj_key']}:{c['pred_key']}")
                    res["contradictions"].append(e)
        return res

    def _replacement(self, me: dict, c) -> list[dict]:
        """`me` states NEW replaced OLD. Link to earlier objects that record OLD as the chosen/current value."""
        out = []
        old_key, new_name = c["val_key"], c["subject"]
        rows = self.db.all(
            "SELECT c.object_id, c.text, o.title, o.created_at, o.event_time FROM claims c JOIN objects o ON o.id=c.object_id "
            "WHERE c.val_key=? AND c.pred_key!='replaced' AND c.object_id!=? AND c.status='active' AND o.status='active' "
            "AND c.functional=1", (old_key, me["id"]))
        for r in rows:
            if self.graph.is_rejected(me["id"], r["object_id"], "replaces"):
                continue
            expl = (f"This object states that “{new_name}” replaced “{c['value']}”; “{truncate(r['title'], 60)}” records "
                    f"“{c['value']}” as the chosen/current value.")
            e = self.graph.add_edge(me["id"], r["object_id"], "replaces", origin="ai", confidence=0.78, explanation=expl,
                                    evidence={"claim": c["id"], "quote": c["evidence"] or c["text"], "older_quote": r["text"]}, actor="ingest")
            if e:
                out.append(e)
                add_finding(self.db, kind="outdated", level="conditional", severity="warning", confidence=0.78,
                            title=f"Possibly outdated: {truncate(r['title'], 60)}",
                            explanation=expl + " Consider marking the older object as superseded.",
                            targets=[r["object_id"], me["id"]], payload={"edge_id": e["id"], "supersede": r["object_id"]},
                            dedupe_key=f"outdated:{r['object_id']}:{me['id']}")
        return out
