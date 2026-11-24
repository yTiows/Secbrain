"""Persistent knowledge graph: typed edges with provenance + bounded traversal + analytics.

The graph lives in the `edges` table; nothing here depends on the UI.
"""
from __future__ import annotations
import heapq
import threading
from collections import defaultdict, deque
from typing import Iterable

from .db import Database, jdump, jload, now
from .knowledge import KnowledgeStore, NotFound, Invalid, new_id
from .knowledge_types import EDGE_TYPES

ACTIVE_NODE = ("active", "superseded")


def edge_strength(e: dict) -> float:
    """Relationship strength in [0,1]: type prior x confidence, boosted when the user verified it."""
    base = EDGE_TYPES.get(e["type"], {"weight": 0.5})["weight"] * float(e.get("confidence", 1.0))
    if e.get("verification") == "verified":
        base = min(1.0, base * 1.25 + 0.05)
    return round(base, 4)


class GraphEngine:
    def __init__(self, db: Database, store: KnowledgeStore):
        self.db = db
        self.store = store
        self._lock = threading.Lock()
        self._gen = -1
        self._adj: dict[str, list[dict]] = {}
        self._nodes: dict[str, dict] = {}

    # ------------------------------------------------------------------ edges
    def _row(self, r) -> dict:
        d = dict(r)
        d["evidence"] = jload(d.get("evidence"), {})
        d["strength"] = edge_strength(d)
        return d

    def _canon(self, src: str, dst: str, type_: str) -> tuple[str, str]:
        if EDGE_TYPES[type_]["symmetric"] and src > dst:
            return dst, src
        return src, dst

    def get_edge(self, eid: str) -> dict:
        r = self.db.one("SELECT * FROM edges WHERE id=?", (eid,))
        if not r:
            raise NotFound(eid)
        return self._row(r)

    def find_edge(self, src: str, dst: str, type_: str) -> dict | None:
        src, dst = self._canon(src, dst, type_)
        r = self.db.one("SELECT * FROM edges WHERE src=? AND dst=? AND type=?", (src, dst, type_))
        return self._row(r) if r else None

    def is_rejected(self, src: str, dst: str, type_: str | None = None) -> bool:
        """True if the user rejected this relationship (any type when type_ is None)."""
        a, b = (src, dst) if src <= dst else (dst, src)
        sql = "SELECT 1 FROM edges WHERE verification='rejected' AND ((src=? AND dst=?) OR (src=? AND dst=?))"
        args: list = [src, dst, dst, src]
        if type_:
            sql += " AND type=?"; args.append(type_)
        return self.db.one(sql, args) is not None

    def add_edge(self, src: str, dst: str, type_: str, *, origin: str = "ai", confidence: float = 1.0,
                 explanation: str = "", evidence: dict | None = None, verification: str | None = None,
                 actor: str | None = None, log: bool = True) -> dict | None:
        if type_ not in EDGE_TYPES:
            raise Invalid(f"unknown relationship type {type_!r}; allowed: {sorted(EDGE_TYPES)}")
        if src == dst:
            raise Invalid("self-relationships are not allowed")
        if origin not in ("user", "import", "ai"):
            raise Invalid("origin must be user|import|ai")
        if not 0 <= confidence <= 1:
            raise Invalid("confidence must be within [0,1]")
        actor = actor or origin
        verification = verification or ("verified" if origin == "user" else "suggested")
        src, dst = self._canon(src, dst, type_)
        with self.db.tx():
            for oid in (src, dst):
                if not self.db.one("SELECT 1 FROM objects WHERE id=? AND status!='deleted'", (oid,)):
                    raise NotFound(oid)
            ex = self.db.one("SELECT * FROM edges WHERE src=? AND dst=? AND type=?", (src, dst, type_))
            t = now()
            if ex:
                exd = self._row(ex)
                if exd["verification"] == "rejected" and origin != "user":
                    return None                              # user rejection is permanent for AI rediscovery
                if exd["status"] == "active" and exd["verification"] != "rejected":
                    if origin == "ai" and confidence <= exd["confidence"]:
                        return exd                           # nothing new
                    new = dict(confidence=max(confidence, exd["confidence"]) if origin == "ai" else confidence,
                               explanation=explanation or exd["explanation"],
                               evidence={**exd["evidence"], **(evidence or {})})
                    if origin != "ai" and exd["origin"] == "ai":                      # a human/import statement upgrades an AI suggestion
                        new["verification"] = verification
                        self.db.exec("UPDATE edges SET origin=? WHERE id=?", (origin, exd["id"]))
                    return self.update_edge(exd["id"], new, actor=actor, log=log)
                # reactivate (deleted by anyone or rejected-by-user then re-created by user)
                self.db.exec("UPDATE edges SET status='active', verification=?, origin=?, confidence=?, explanation=?, evidence=?, updated_at=? WHERE id=?",
                             (verification, origin, confidence, explanation, jdump(evidence or {}), t, exd["id"]))
                self._hist(exd["id"], actor, "reactivate", exd, self.get_edge(exd["id"]))
                return self.get_edge(exd["id"])
            eid = new_id("e")
            self.db.exec("INSERT INTO edges(id,src,dst,type,origin,confidence,explanation,evidence,verification,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (eid, src, dst, type_, origin, confidence, explanation, jdump(evidence or {}), verification, t, t))
            e = self.get_edge(eid)
            self._hist(eid, actor, "create", None, e)
            if log and origin == "user":
                self.store.log_change(actor, "edge_create", eid, f"{type_}: {src}→{dst}", {"op": "edge_status", "edge_id": eid, "status": "deleted"})
            return e

    def _hist(self, eid, actor, action, before, after):
        self.db.exec("INSERT INTO edge_history(edge_id,ts,actor,action,before,after) VALUES(?,?,?,?,?,?)",
                     (eid, now(), actor, action, jdump(before) if before else None, jdump(after) if after else None))

    def update_edge(self, eid: str, patch: dict, *, actor: str = "user", log: bool = True) -> dict:
        allowed = {"verification", "explanation", "confidence", "type", "evidence", "status"}
        bad = set(patch) - allowed
        if bad:
            raise Invalid(f"unknown fields {sorted(bad)}")
        with self.db.tx():
            cur = self.get_edge(eid)
            sets, args = [], []
            for f in ("verification", "explanation", "confidence", "status"):
                if f in patch and patch[f] != cur[f]:
                    if f == "verification" and patch[f] not in ("suggested", "verified", "rejected"):
                        raise Invalid("verification must be suggested|verified|rejected")
                    if f == "status" and patch[f] not in ("active", "deleted"):
                        raise Invalid("status must be active|deleted")
                    if f == "confidence" and not 0 <= float(patch[f]) <= 1:
                        raise Invalid("confidence must be within [0,1]")
                    sets.append(f"{f}=?"); args.append(patch[f])
            if "evidence" in patch:
                sets.append("evidence=?"); args.append(jdump(patch["evidence"]))
            if "type" in patch and patch["type"] != cur["type"]:
                nt = patch["type"]
                if nt not in EDGE_TYPES:
                    raise Invalid(f"unknown relationship type {nt!r}")
                s, d = self._canon(cur["src"], cur["dst"], nt)
                if self.db.one("SELECT 1 FROM edges WHERE src=? AND dst=? AND type=? AND id!=?", (s, d, nt, eid)):
                    raise Invalid("a relationship of that type already exists between these nodes")
                sets += ["type=?", "src=?", "dst=?"]; args += [nt, s, d]
            if not sets:
                return cur
            if actor == "user" and "verification" not in patch and "type" in patch:
                sets.append("origin=?"); args.append("user")
            sets.append("updated_at=?"); args.append(now())
            self.db.exec(f"UPDATE edges SET {', '.join(sets)} WHERE id=?", (*args, eid))
            new = self.get_edge(eid)
            self._hist(eid, actor, "update", cur, new)
            if log:
                self.store.log_change(actor, "edge_update", eid, f"{cur['type']} {cur['verification']}→{new['type']} {new['verification']}",
                                      {"op": "edge_restore", "edge": {k: cur[k] for k in ("id", "src", "dst", "type", "verification", "explanation", "confidence", "status", "origin")}})
            return new

    def verify_edge(self, eid, actor="user"):
        return self.update_edge(eid, {"verification": "verified"}, actor=actor)

    def reject_edge(self, eid, actor="user"):
        return self.update_edge(eid, {"verification": "rejected"}, actor=actor)

    def delete_edge(self, eid, actor="user"):
        return self.update_edge(eid, {"status": "deleted"}, actor=actor)

    def edge_history(self, eid: str) -> list[dict]:
        return [{**dict(r), "before": jload(r["before"]), "after": jload(r["after"])} for r in
                self.db.all("SELECT * FROM edge_history WHERE edge_id=? ORDER BY id", (eid,))]

    def edges_of(self, oid: str, direction: str = "both", types: Iterable[str] | None = None,
                 include_rejected: bool = False) -> list[dict]:
        """Relationships touching `oid`, annotated with neighbor info and direction relative to oid."""
        where = "e.status='active'" + ("" if include_rejected else " AND e.verification!='rejected'")
        out = []
        sql = ("SELECT e.*, o.title AS other_title, o.type AS other_type, o.status AS other_status FROM edges e "
               "JOIN objects o ON o.id = CASE WHEN e.src=? THEN e.dst ELSE e.src END "
               f"WHERE (e.src=? OR e.dst=?) AND {where} AND o.status!='deleted'")
        for r in self.db.all(sql, (oid, oid, oid)):
            d = self._row(r)
            sym = EDGE_TYPES[d["type"]]["symmetric"]
            d["direction"] = "out" if d["src"] == oid else "in"
            d["other_id"] = d["dst"] if d["src"] == oid else d["src"]
            d["label"] = d["type"] if (d["direction"] == "out" or sym) else EDGE_TYPES[d["type"]]["inverse"]
            if direction == "out" and d["direction"] != "out" and not sym:
                continue
            if direction == "in" and d["direction"] != "in" and not sym:
                continue
            if types and d["type"] not in types:
                continue
            out.append(d)
        out.sort(key=lambda x: -x["strength"])
        return out

    def backlinks(self, oid: str) -> list[dict]:
        return self.edges_of(oid, "in")

    # ----------------------------------------------------------- in-memory view
    def _load(self):
        with self._lock:
            if self._gen == self.db.generation and self._nodes:
                return
            nodes = {r["id"]: dict(r) for r in self.db.all(
                "SELECT id,type,title,project,importance,confidence,status,updated_at FROM objects WHERE status IN ('active','superseded')")}
            adj: dict[str, list[dict]] = defaultdict(list)
            for r in self.db.all("SELECT * FROM edges WHERE status='active' AND verification!='rejected'"):
                if r["src"] in nodes and r["dst"] in nodes:
                    d = self._row(r)
                    adj[d["src"]].append(d)
                    adj[d["dst"]].append(d)
            self._nodes, self._adj, self._gen = nodes, adj, self.db.generation

    def graph_view(self):
        self._load()
        return self._nodes, self._adj

    # ---------------------------------------------------------------- traversal
    def neighborhood(self, oid: str, hops: int = 2, limit: int = 60, min_strength: float = 0.0,
                     types: Iterable[str] | None = None, allow=None) -> dict:
        """Bounded best-first expansion: nodes are visited in order of path strength (product of edge strengths),
        capped by hop count and node budget — never an unrestricted traversal."""
        nodes, adj = self.graph_view()
        if oid not in nodes:
            raise NotFound(oid)
        types = set(types) if types else None
        best = {oid: 1.0}
        depth = {oid: 0}
        heap = [(-1.0, oid)]
        seen_edges: dict[str, dict] = {}
        visited = []
        while heap and len(visited) < limit:
            neg, cur = heapq.heappop(heap)
            if cur in visited or -neg < best.get(cur, 0) - 1e-12:
                continue
            visited.append(cur)
            if depth[cur] >= hops:
                continue
            for e in adj.get(cur, ()):
                if types and e["type"] not in types:
                    continue
                if e["strength"] < min_strength:
                    continue
                other = e["dst"] if e["src"] == cur else e["src"]
                if allow and not allow(nodes[other]):
                    continue
                s = best[cur] * e["strength"]
                if s > best.get(other, 0):
                    best[other] = s
                    depth[other] = depth[cur] + 1
                    heapq.heappush(heap, (-s, other))
        vset = set(visited)
        for v in visited:
            for e in adj.get(v, ()):
                if e["src"] in vset and e["dst"] in vset and (not types or e["type"] in types) and e["strength"] >= min_strength:
                    seen_edges[e["id"]] = e
        return {
            "center": oid,
            "nodes": [{**nodes[v], "hops": depth[v], "path_strength": round(best[v], 4)} for v in visited],
            "edges": list(seen_edges.values()),
            "truncated": bool(heap) and len(visited) >= limit,
        }

    def path(self, a: str, b: str, max_hops: int = 4) -> list[dict] | None:
        nodes, adj = self.graph_view()
        if a not in nodes or b not in nodes:
            raise NotFound(a if a not in nodes else b)
        prev = {a: None}
        dq = deque([(a, 0)])
        while dq:
            cur, d = dq.popleft()
            if cur == b:
                break
            if d >= max_hops:
                continue
            for e in sorted(adj.get(cur, ()), key=lambda x: -x["strength"]):
                o = e["dst"] if e["src"] == cur else e["src"]
                if o not in prev:
                    prev[o] = (cur, e)
                    dq.append((o, d + 1))
        if b not in prev:
            return None
        path, cur = [], b
        while prev[cur] is not None:
            p, e = prev[cur]
            path.append({"from": p, "to": cur, "edge": e})
            cur = p
        return list(reversed(path))

    # ---------------------------------------------------------------- analytics
    def components(self) -> list[list[str]]:
        nodes, adj = self.graph_view()
        parent = {n: n for n in nodes}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for n, es in adj.items():
            for e in es:
                ra, rb = find(e["src"]), find(e["dst"])
                if ra != rb:
                    parent[ra] = rb
        comps = defaultdict(list)
        for n in nodes:
            comps[find(n)].append(n)
        return sorted(comps.values(), key=len, reverse=True)

    def isolated(self) -> list[str]:
        nodes, adj = self.graph_view()
        return [n for n in nodes if not adj.get(n)]

    def clusters(self, iterations: int = 12) -> dict[str, int]:
        """Deterministic weighted label propagation. Returns node -> cluster id (0 = largest)."""
        nodes, adj = self.graph_view()
        label = {n: n for n in sorted(nodes)}
        order = sorted(nodes)
        for _ in range(iterations):
            changed = False
            for n in order:
                es = adj.get(n)
                if not es:
                    continue
                score = defaultdict(float)
                for e in es:
                    o = e["dst"] if e["src"] == n else e["src"]
                    score[label[o]] += e["strength"]
                score[label[n]] += 1e-6
                best = max(score.items(), key=lambda kv: (kv[1], kv[0]))[0]
                if best != label[n]:
                    label[n] = best
                    changed = True
            if not changed:
                break
        groups = defaultdict(list)
        for n, l in label.items():
            groups[l].append(n)
        ranked = sorted(groups.values(), key=lambda g: (-len(g), g[0]))
        out = {}
        for i, g in enumerate(ranked):
            for n in g:
                out[n] = i
        return out

    def centrality(self, damping: float = 0.85, iters: int = 30) -> dict[str, float]:
        """Weighted PageRank over the undirected graph. Used to identify important nodes."""
        nodes, adj = self.graph_view()
        n = len(nodes)
        if not n:
            return {}
        pr = {k: 1.0 / n for k in nodes}
        wsum = {k: sum(e["strength"] for e in adj.get(k, ())) for k in nodes}
        for _ in range(iters):
            new = {k: (1 - damping) / n for k in nodes}
            dangling = sum(pr[k] for k in nodes if wsum[k] == 0)
            for k in nodes:
                if wsum[k] == 0:
                    continue
                for e in adj[k]:
                    o = e["dst"] if e["src"] == k else e["src"]
                    new[o] += damping * pr[k] * e["strength"] / wsum[k]
            for k in nodes:
                new[k] += damping * dangling / n
            pr = new
        return pr

    def stats(self) -> dict:
        nodes, adj = self.graph_view()
        comps = self.components()
        n_edges = len({e["id"] for es in adj.values() for e in es})
        by_type = defaultdict(int)
        for es in adj.values():
            for e in es:
                by_type[e["type"]] += 0.5
        return {"nodes": len(nodes), "edges": n_edges, "components": len(comps),
                "largest_component": len(comps[0]) if comps else 0, "isolated": len(self.isolated()),
                "edges_by_type": {k: int(v) for k, v in by_type.items()}}

    def overview(self, max_nodes: int = 400, types: Iterable[str] | None = None, project: str | None = None,
                 min_strength: float = 0.0, allow=None) -> dict:
        """Level-of-detail graph for the UI: top nodes by importance*centrality, plus cluster summaries."""
        nodes, adj = self.graph_view()
        cl = self.clusters()
        pr = self.centrality()
        types = set(types) if types else None
        cand = [n for n in nodes.values()
                if (not types or n["type"] in types) and (not project or n["project"] == project) and (not allow or allow(n))]
        mx = max((pr.get(n["id"], 0) for n in cand), default=1) or 1
        for n in cand:
            n["_score"] = 0.6 * pr.get(n["id"], 0) / mx + 0.4 * n["importance"]
        cand.sort(key=lambda n: -n["_score"])
        keep = cand[:max_nodes]
        keep_ids = {n["id"] for n in keep}
        edges = {}
        for n in keep:
            for e in adj.get(n["id"], ()):
                if e["src"] in keep_ids and e["dst"] in keep_ids and e["strength"] >= min_strength:
                    edges[e["id"]] = {k: e[k] for k in ("id", "src", "dst", "type", "strength", "verification", "origin")}
        csize = defaultdict(int)
        for n in cand:
            csize[cl.get(n["id"], -1)] += 1
        clusters = []
        for cid, size in sorted(csize.items(), key=lambda kv: -kv[1])[:60]:
            members = sorted((n for n in cand if cl.get(n["id"], -1) == cid), key=lambda n: -n["_score"])
            clusters.append({"id": cid, "size": size, "label": members[0]["title"] if members else str(cid),
                             "top": [m["title"] for m in members[:3]]})
        return {
            "nodes": [{"id": n["id"], "title": n["title"], "type": n["type"], "project": n["project"],
                       "importance": n["importance"], "cluster": cl.get(n["id"], -1),
                       "degree": len(adj.get(n["id"], ())), "rank": round(n["_score"], 4)} for n in keep],
            "edges": list(edges.values()),
            "clusters": clusters,
            "total_nodes": len(cand), "shown": len(keep),
        }
