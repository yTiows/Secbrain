"""Scale/performance probe on a synthetic vault (reproducible, seeded).

    python -m benchmarks.scale --docs 1500
"""
from __future__ import annotations
import argparse
import json
import random
import statistics
import tempfile
import time
from pathlib import Path

from brain.app import Brain
from brain.config import Settings

TECH = ["cache", "queue", "shard", "index", "replica", "gateway", "scheduler", "tokenizer", "ledger", "pipeline", "router", "balancer", "firewall", "cluster",
        "container", "registry", "snapshot", "migration", "rollback", "throttle", "retry", "timeout", "latency", "throughput", "checkpoint", "compaction"]
PROJ = [f"Project{c}" for c in "ABCDEFGHIJKLMNOPQRST"]
ENT = ["Redis", "Kafka", "PostgreSQL", "Kubernetes", "Terraform", "Grafana", "Prometheus", "RabbitMQ", "Nginx", "Docker", "Vault", "Consul", "Elastic", "Spark", "Airflow"]


def make_doc(rng: random.Random, i: int) -> tuple[str, str, str]:
    proj = rng.choice(PROJ)
    a, b, c = rng.sample(TECH, 3)
    e1, e2 = rng.sample(ENT, 2)
    n = rng.randint(2, 40)
    uniq = f"{rng.choice(['alpha','bravo','delta','sigma','omega','kappa'])}{i}"
    text = (f"---\nproject: {proj}\n---\n# {a.title()} {b} notes {uniq}\n\n"
            f"The {a} service for {proj} uses {e1} and relies on a {b}. We observed that {c} problems appear when load grows beyond {n} requests per second. "
            f"The team keeps the {a} configuration reviewed every quarter.\n\n## Decision\n\nWe decided to use {e2} for the {b} layer because it handles {c} better. "
            f"The default timeout is {rng.randint(5, 90)} seconds for {a} calls in {proj}.\n")
    return f"{proj}/{uniq}.md", text, f"{a} {b} {c} {e1}"


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--docs", type=int, default=1500); ap.add_argument("--out", default=str(Path(__file__).parent / "results" / "scale.json"))
    a = ap.parse_args()
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as td:
        b = Brain(Settings(data_dir=Path(td) / "data"))
        t0 = time.perf_counter(); lat_ing = []; queries = []
        for i in range(a.docs):
            uri, text, q = make_doc(rng, i)
            t1 = time.perf_counter()
            j = b.ingest.ingest_bytes(uri.split("/")[-1], text.encode(), uri=uri)
            lat_ing.append(time.perf_counter() - t1)
            assert j["status"] == "done", j
            if i % 15 == 0:
                queries.append(q)
        ingest_s = time.perf_counter() - t0
        t1 = time.perf_counter(); b.store.reindex(); reindex_s = time.perf_counter() - t1
        eng = b.retrieval
        ls, lc = [], []
        for q in queries:
            t1 = time.perf_counter(); eng.search(q, k=10); ls.append((time.perf_counter() - t1) * 1000)
            t1 = time.perf_counter(); eng.context(q, token_budget=1500); lc.append((time.perf_counter() - t1) * 1000)
        t1 = time.perf_counter(); ov = b.graph.overview(max_nodes=400); ov_ms = (time.perf_counter() - t1) * 1000
        t1 = time.perf_counter(); b.graph.clusters(); cl_ms = (time.perf_counter() - t1) * 1000
        t1 = time.perf_counter(); m = b.maintenance.run(["duplicates", "isolated", "contradictions"]); mt = time.perf_counter() - t1
        import os
        size = sum(f.stat().st_size for f in Path(td).rglob("*") if f.is_file()) / 1e6
        p = lambda xs, q: round(sorted(xs)[int(q * (len(xs) - 1))], 2)
        res = {"docs": a.docs, "objects": b.store.stats()["objects"], "edges": b.graph.stats()["edges"], "ingest_total_s": round(ingest_s, 1),
               "ingest_docs_per_s": round(a.docs / ingest_s, 1), "ingest_ms_p50_p95_last100": [round(statistics.median(lat_ing[-100:]) * 1000, 1), round(sorted(lat_ing[-100:])[94] * 1000, 1)],
               "reindex_s": round(reindex_s, 2), "search_ms": {"n": len(ls), "p50": p(ls, .5), "p95": p(ls, .95)}, "context_ms": {"p50": p(lc, .5), "p95": p(lc, .95)},
               "graph_overview_ms": round(ov_ms, 1), "clusters_ms": round(cl_ms, 1), "maintenance_subset_s": round(mt, 2), "disk_mb": round(size, 1)}
        b.close()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
