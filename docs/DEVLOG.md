# Development log

Status vocabulary: **Implemented** (code exists) · **Tested** (automated test exercises it) · **Partial** · **Simulated** (stubbed) · **Not implemented**.

## Phase 1 — Architecture & audit (done)
- Repo was empty. Architecture and decisions in `docs/ARCHITECTURE.md`.
- Environment constraints discovered: no model hub access, no LLM key → deterministic offline pipeline, pluggable LLM/embeddings.

## Phase 2 — Knowledge foundation (done, tested)
- SQLite schema (WAL, FTS5, FKs); content-addressed immutable sources; typed objects with full version snapshots; tags/aliases;
  claims separate from sources; change log with inverse payloads; soft delete / guarded purge; reindex of all derived data.
- Tests: persistence across reopen, validation, versions + restore, no-op updates, delete safeguards, source idempotency, transaction rollback, reindex.

## Phase 3 — Knowledge graph (done, tested)
- Typed directed edges (12 types) with origin, confidence, explanation, evidence, verification, history; symmetric canonicalization;
  user rejection = permanent tombstone; bounded best-first traversal; components, label-propagation clusters, PageRank, isolated nodes, paths, LOD overview.
- Tests: provenance/validation, tombstones, merge-on-rediscovery, traversal bounds, analytics.

## Phase 4 — Ingestion (done, tested)
- Extractors: txt/md(frontmatter)/json/pdf/docx with explicit failures (no OCR). Sectionizer, heuristic classifier (16 types), entity+alias extraction,
  structured claims (decisions, attributes, numeric limits, replacement, negation), dedupe (unchanged/update/evidence/new/user-edit-protected),
  relationship discovery with IDF-damped entity evidence, conflict + replacement detection, injection flagging, dry-run/review mode,
  background worker + durable jobs + crash recovery, atomic per-source transaction.
- Honest limits: heuristic extraction (no LLM), English-centric patterns, entities need 2 mentions or 2 sources unless strongly signalled.
