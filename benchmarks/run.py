"""Reproducible retrieval benchmark.

    python -m benchmarks.run [--out benchmarks/results/latest.json]

Builds a fresh vault from benchmarks/corpus (md/txt/json checked in; docx/pdf generated), ingests everything through the
real pipeline, then evaluates labelled queries per retrieval mode (ablation) and reports precision@K, recall@K, MRR,
latency, token consumption and traceability.
"""
from __future__ import annotations
import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from brain.app import Brain
from brain.config import Settings
from brain.retrieval import RetrievalEngine
from brain.text import estimate_tokens
from .corpus_build import materialise

HERE = Path(__file__).parent
MODES = {
    "fts_only": dict(channels=frozenset({"lex"})),
    "semantic_only": dict(channels=frozenset({"sem"})),
    "hybrid_no_graph": dict(channels=frozenset({"lex", "sem", "ent"})),
    "full": dict(),
}


def load_corpus(brain: Brain, workdir: Path) -> dict:
    files = materialise(workdir / "corpus")
    t0 = time.perf_counter()
    failures = []
    for rel, opts in files:
        data = (workdir / "corpus" / rel).read_bytes()
        j = brain.ingest.ingest_bytes(Path(rel).name, data, uri=rel, **opts)
        if j["status"] != "done":
            failures.append((rel, j["status"], j.get("error")))
    brain.store.reindex()                     # recompute corpus IDF statistics over the final vault
    return {"files": len(files), "failures": failures, "ingest_seconds": round(time.perf_counter() - t0, 2)}


def primary_uri_map(brain: Brain) -> dict[str, str | None]:
    """object id -> primary source uri (entities/auto objects excluded: they are not document evidence)."""
    m = {}
    for r in brain.db.all("SELECT o.id, o.type, o.origin, s.uri FROM objects o LEFT JOIN sources s ON s.id=o.source_id WHERE o.status='active'"):
        m[r["id"]] = None if (r["type"] == "entity" or (r["type"] in ("concept", "project") and r["origin"] == "ai")) else r["uri"]
    return m


def rank_docs(items, urimap) -> list[str]:
    out = []
    for it in items:
        u = urimap.get(it["id"])
        if u and u not in out:
            out.append(u)
    return out


def evaluate(brain: Brain, queries: list[dict], mode: str, kw: dict, k_items: int = 10) -> dict:
    eng = RetrievalEngine(brain.db, brain.store, brain.graph, brain.settings)
    urimap = primary_uri_map(brain)
    rows, lat = [], []
    for q in queries:
        r = eng.retrieve(q["q"], k=k_items, **kw)
        lat.append(r["stats"]["latency_ms"])
        docs = rank_docs(r["items"], urimap)
        rel, ok, dis = set(q["rel"]), set(q.get("ok", [])), set(q.get("distract", []))
        row = {"id": q["id"], "cat": q["cat"], "docs": docs[:6], "n_items": len(r["items"])}
        if q.get("expect_empty"):
            row["correct_empty"] = len(r["items"]) == 0
        else:
            def p_at(k):
                return len([d for d in docs[:k] if d in rel]) / k
            def r_at(k):
                return len([d for d in docs[:k] if d in rel]) / len(rel) if rel else 0.0
            first = next((i for i, d in enumerate(docs) if d in rel), None)
            row.update({"p3": p_at(3), "r3": r_at(3), "r5": r_at(5), "mrr": (1 / (first + 1)) if first is not None else 0.0,
                        "hit1": 1.0 if docs[:1] and docs[0] in rel else 0.0,
                        "p_returned5": (len([d for d in docs[:5] if d in rel | ok]) / len(docs[:5])) if docs[:5] else 0.0,
                        "leak3": len([d for d in docs[:3] if d in dis]) if dis else None})
            if q.get("top1"):
                row["top1_ok"] = bool(docs) and docs[0] == q["top1"]
        rows.append(row)
    scored = [r for r in rows if "p3" in r]

    def avg(key, subset=None):
        vals = [r[key] for r in (subset or scored) if r.get(key) is not None]
        return round(statistics.mean(vals), 3) if vals else None
    cats = {}
    for c in sorted({r["cat"] for r in scored}):
        sub = [r for r in scored if r["cat"] == c]
        cats[c] = {"n": len(sub), "r5": avg("r5", sub), "mrr": avg("mrr", sub), "hit1": avg("hit1", sub), "p_returned5": avg("p_returned5", sub)}
    neg = [r for r in rows if "correct_empty" in r]
    leak = [r for r in scored if r.get("leak3") is not None]
    return {"mode": mode, "n_queries": len(rows),
            "P@3": avg("p3"), "R@3": avg("r3"), "R@5": avg("r5"), "MRR": avg("mrr"), "Hit@1": avg("hit1"),
            "precision_of_returned_top5": avg("p_returned5"),
            "negative_queries_correctly_empty": f"{sum(r['correct_empty'] for r in neg)}/{len(neg)}",
            "distractor_leakage_top3": f"{sum(r['leak3'] for r in leak)} distractors in {len(leak)} queries",
            "latency_ms": {"p50": round(statistics.median(lat), 2), "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 2), "max": round(max(lat), 2)},
            "by_category": cats, "rows": rows}


def package_checks(brain: Brain, queries: list[dict], budget: int = 1500) -> dict:
    eng = RetrievalEngine(brain.db, brain.store, brain.graph, brain.settings)
    urimap = primary_uri_map(brain)
    trace_ok = trace_tot = 0
    conf_ok = conf_tot = 0
    tok, full_tok, vault_tok, over = [], [], [], 0
    explain_ok = explain_tot = 0
    for q in queries:
        if q.get("expect_empty"):
            continue
        p = eng.context(q["q"], token_budget=budget)
        for it in p["items"]:
            trace_tot += 1
            trace_ok += 1 if it["sources"] else 0
            explain_tot += 1
            explain_ok += 1 if it.get("why") else 0
        if q.get("expect_conflict"):
            conf_tot += 1
            conf_ok += 1 if p["conflicts"] else 0
        s = p["stats"]
        real = estimate_tokens(json.dumps({k_: v for k_, v in p.items() if k_ != "_full_tokens"}, ensure_ascii=False, separators=(",", ":")))
        tok.append(real); full_tok.append(p["_full_tokens"]); vault_tok.append(eng._vault_tokens())
        over += 1 if real > budget else 0
    return {"budget": budget, "queries": len(tok), "mean_package_tokens_est": round(statistics.mean(tok), 1),
            "mean_selected_full_content_tokens_est": round(statistics.mean(full_tok), 1),
            "entire_vault_tokens_est": vault_tok[-1] if vault_tok else 0,
            "over_budget": over, "source_traceability": f"{trace_ok}/{trace_tot}", "items_with_explanation": f"{explain_ok}/{explain_tot}",
            "conflicts_reported": f"{conf_ok}/{conf_tot}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "results" / "latest.json"))
    args = ap.parse_args()
    queries = json.loads((HERE / "queries.json").read_text())
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        brain = Brain(Settings(data_dir=td / "data"))
        info = load_corpus(brain, td)
        res = {"corpus": info, "graph": brain.graph.stats(), "store": brain.store.stats(), "modes": {}}
        for m, kw in MODES.items():
            res["modes"][m] = evaluate(brain, queries, m, kw)
        res["package"] = package_checks(brain, queries)
        brain.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, indent=1))
    print(f"corpus: {info}")
    print(f"graph: {res['graph']}")
    hdr = f"{'mode':18} {'P@3':>6} {'R@3':>6} {'R@5':>6} {'MRR':>6} {'Hit@1':>6} {'Prec(ret)':>9}  neg-empty  leakage  p50/p95 ms"
    print(hdr)
    for m, r in res["modes"].items():
        print(f"{m:18} {r['P@3']:>6} {r['R@3']:>6} {r['R@5']:>6} {r['MRR']:>6} {r['Hit@1']:>6} {r['precision_of_returned_top5']:>9}  {r['negative_queries_correctly_empty']:>9}  {r['distractor_leakage_top3']}  {r['latency_ms']['p50']}/{r['latency_ms']['p95']}")
    print("package:", res["package"])
    print("by category (full):")
    for c, v in res["modes"]["full"]["by_category"].items():
        print(f"  {c:20} {v}")
    return res


if __name__ == "__main__":
    main()
