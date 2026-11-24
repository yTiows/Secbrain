# Development log

Status vocabulary: **Implemented** (code exists) · **Tested** (automated test exercises it) · **Partial** · **Simulated** (stubbed) · **Not implemented**.
All numbers below were measured in this build (`pytest`, `python -m benchmarks.run`, `python -m benchmarks.scale`, `node tests/e2e_ui.js`).

## Phase 1 — Architecture & audit
Repo was empty. Decisions + risks: `docs/ARCHITECTURE.md`. Environment constraints discovered: no model-hub access, no LLM key ⇒ deterministic offline
pipeline with pluggable LLM/embedding providers.

## Phase 2 — Knowledge foundation (Implemented, Tested)
SQLite (WAL, FTS5, FK). Content-addressed immutable sources; 16 object types (extensible via `extra`); full version snapshots; tags/aliases; claims separate
from sources (evidence sentence + source ref + confidence + verification); change log with inverse payloads; soft-delete + guarded purge; `reindex` rebuilds all
derived data. Tests: persistence across reopen, validation, versions/restore, no-op updates, delete safeguards, source idempotency, rollback atomicity, reindex, embedding cache vs status/rollback.

## Phase 3 — Knowledge graph (Implemented, Tested)
12 typed relationship kinds with origin/confidence/explanation/evidence/verification/history; symmetric canonicalization; user rejection = permanent tombstone
(AI cannot resurrect); AI suggestion upgraded when a human/import asserts it; bounded best-first traversal; components, label-propagation clusters, PageRank,
isolated nodes, paths, LOD overview (analytics cached per DB generation).

## Phase 4 — Ingestion (Implemented, Tested)
txt/md(frontmatter)/json/pdf/docx with explicit extraction failures (originals still preserved). Sectionizer, 16-type heuristic classifier, entity + alias extraction
(acronym definitions, “also known as”, wiki-links; weak single mentions are promoted only after a 2nd source mentions them), structured claims
(decisions, attributes, numeric limits with unit normalisation, dates, replacement, negation), dedupe decisions (`unchanged` / `update` / `evidence` / `new` /
`conflict_user_edit` — user edits are never overwritten), relationship discovery (IDF-damped shared entities, explicit mentions, cue phrases; evidence rule, not similarity),
contradiction + replacement detection (project-scoped, compared against the 3 most recent claims), injection flagging, dry-run and review-before-commit modes,
background worker with durable jobs + crash recovery, one atomic transaction per source.
Verified with real DOCX/PDF/JSON/MD files, idempotent re-import, changed-source versioning, atomic failure + retry without duplicates, prompt-injection document.

## Phase 5 — Retrieval (Implemented, Tested, Benchmarked)
Hybrid: FTS5/BM25 ∪ semantic ∪ entity-linked ∪ title → fusion (+RRF) → bounded hub-damped graph expansion → contextual scoring (project, importance, recency/history
intent, verification, supersession) → **grounding gate** → near-duplicate collapse → conflict surfacing → token-budgeted package (sources, per-item *why*, relationships,
uncertainty, untrusted-content marks). Benchmark = 40 real documents (md/txt/json + generated docx/pdf, 3 domains, confounders) × 45 labelled queries.

| mode | P@3 | R@3 | R@5 | MRR | Hit@1 | negatives empty | p50/p95 ms |
|---|---|---|---|---|---|---|---|
| FTS only | 0.317 | 0.754 | 0.762 | 0.750 | 0.714 | 3/3 | 1.4/2.3 |
| semantic only (hash-TF-IDF) | 0.317 | 0.754 | 0.762 | 0.718 | 0.667 | 3/3 | 1.3/2.3 |
| hybrid, no graph | 0.373 | 0.885 | 0.905 | 0.889 | 0.857 | 3/3 | 1.6/2.5 |
| **full (hybrid + graph)** | **0.381** | **0.909** | **0.940** | 0.873 | 0.810 | **3/3** | 1.8/3.0 |

P@3 is capped (most queries have 1–2 relevant documents). By category (full): direct R@5 1.0 · synonym 1.0 · research 1.0 · contradiction 1.0 · temporal 1.0 ·
irrelevant-similar 1.0 · multi-hop 0.75 · **paraphrase 0.83 (MRR 0.75)**.
* Package: mean 606 tokens (est.) at a 1500 budget vs 4,915 for the whole vault; **but 3.6× the raw text of the selected objects (170 tokens)** — per-item metadata
  (sources, why, ids) costs more than the text on this tiny vault; savings appear only against reading the vault/documents, not against reading the same snippets. Token counts are
  estimates (`max(chars/4, words*1.33)`; no tokenizer offline). Budget respected in 42/42 packages; source traceability 149/149 items; explanations 147/149; conflicts reported in 3/3 conflict queries.
* **Misses (honest):** `hard-relational`, `lesson-fix` (paraphrase with no shared vocabulary, no neural embeddings); `direct-pref` (an incidental-title match ranks
  “Settings” above the right note); `conf-timeout` (right documents retrieved but ranked 3rd); `sim-venv` leaks 1 snake document into top-3 for “Python virtual environment…”
  (distractor leakage 1 in 4 queries). Two retrieval parameters (grounding-agreement thresholds) were chosen by grid search on these same queries ⇒ results are **optimistic**; the
  corpus was written by the implementer. The pytest gate (`tests/test_retrieval.py`) sets floors below measured values.
* Ablation note: the grounding gate is shared by all modes, so “FTS only” is not a pure BM25 baseline.

## Phase 6 — Maintenance & evolution (Implemented, Tested)
Inspections: duplicates, contradictions, outdated/replaced, missing relationships (suggested edges), classification review (skips user-corrected objects), incomplete
objects (auto summaries/tags), unsupported claims (evidence not found in source → flagged, not trusted), isolated nodes/clusters, source integrity (hash/missing file), stale projects,
index drift (auto reindex). Autonomy: SAFE auto; CONDITIONAL on approval (or `auto_conditional` ≥ 0.9 confidence for duplicate/outdated, default off); PROTECTED never applicable.
Merge is non-destructive (loser archived + pointer; edges/claims/sources/tags move) and fully reversible; every applied change has an inverse (`undo`, no double undo).
Evolution: concept discovery (recurring bigrams ≥3 objects ≥2 sources), consolidation proposals (same entity + same project; quotes, never rewrites), gap reports (unanswered
questions, untested hypotheses, rationale-less decisions, thin projects — never fabricated), timelines (updates/replaces/contradicts chains + version history), bounded feedback (±8 %, ≥2 signals).
**Not implemented:** automatic evaluation-before-adoption of ranking changes (the benchmark is the manual gate); feedback-driven classifier tuning; scheduled background maintenance is
implemented (`start_scheduler`, hourly in `serve`) but only exercised manually in tests.

## Phase 7 — Interface & integration (Implemented; UI verified in a real browser)
Static SPA (no build/CDN). Graph: canvas force layout with spatial hashing, cluster gravity, LOD (labels/edges/cluster captions by zoom), label collision avoidance, pan/zoom/pinch,
type filters, strength filter, node cap, expand neighborhood, search highlight. Explorer, detail (content, metadata, sources + original download, relationships with confirm/reject/add,
claims with evidence, timeline, version history + restore, edit/verify/archive/delete), Search (ranked or AI-context package with raw JSON), Intelligence center (findings with apply/dismiss,
gaps, changes with undo, jobs), Vault (drag-drop import, review mode, notes, jobs retry/approve, Markdown export/import, backup, reindex, API keys). DOM built via `textContent` (no `innerHTML`; CSP `script-src 'self'`).
Bugs found by looking at screenshots and fixed: graph off-centre after layout/resize, overlapping labels/cluster captions, filter bar eating the mobile screen, detail sheet covering other mobile views, truncated entity context.

## Phase 8 — QA (measured)
* `pytest`: **92 tests pass** (foundation, graph, ingestion, maintenance/evolution, vault, API, retrieval + benchmark gate, security).
* Real-browser E2E (`node tests/e2e_ui.js`): 12/12 checks — wrong key rejected, upload → job done, note auto-classified, context package with source, edit saves v2 + history kept, maintenance, vault zip, zero console errors. Desktop (1366×820) and phone (390×800) screenshots reviewed; no horizontal overflow.
* Scale probe (`benchmarks/scale.py`, synthetic, seeded): 1,500 docs → 4,534 objects / 26,763 edges; ingest 25 docs/s (p50 60 ms at 4.5k objects); **search p50 25 ms / p95 34 ms; context p50 28 ms**;
  first graph overview 1.2 s (analytics then cached); full reindex 1.6 s; maintenance subset (duplicates+isolated+contradictions) 14 s; DB+originals 117 MB. Profiling found and fixed: O(N²) contradiction fan-out,
  full-matrix reload per write, un-indexed supersession query (2.9 s → 25 ms search). Dense synthetic overlap is a stress case, not a realistic vault.
* Security tests: path traversal in filenames, FTS/SQL injection strings, stored HTML is data, CSP, zip-slip/zip-bomb guards, binary upload rejection, scoped-key escalation attempts, secret non-disclosure, admin key file mode 600, prompt-injection document stays inert and is marked untrusted in packages.
* Error recovery: atomic rollback + retry w/o duplicates, crash recovery of `running` jobs, backups restorable.

## Definition of done (spec §15)
| # | Requirement | Status |
|---|---|---|
| 1-2 | Import multiple related/unrelated docs; structured extraction | Tested (`test_external_agent_end_to_end_definition_of_done`, E2E) |
| 3-4 | Meaningful relationships; avoid duplicates | Tested (unrelated pasta note not linked; duplicate → evidence) |
| 5 | Originals preserved | Tested (byte-identical download) |
| 6-7 | Graph reflects relationships; explore/inspect | Tested (API + browser) |
| 8-11 | NL question → relevant incl. indirect, explained, traceable | Tested; indirect: PostgreSQL links Atlas↔Orion; paraphrase-only queries still miss |
| 12-14 | Corrections persist, versions kept | Tested (incl. restart, undo) |
| 15 | Another app retrieves via API | Tested (scoped read key) |
| — | Irrelevant knowledge not indiscriminately retrieved | Tested/measured: 3/3 negatives empty; 1 leakage case remains |

## Known limitations / next steps
1. Neural embeddings (provider interface ready) — biggest retrieval upgrade; paraphrase recall is the weak spot.
2. LLM extraction/classification: `AnthropicLLM` exists but is untested live; claims/entities are regex/heuristic and English-only.
3. Corpus for benchmark is small and self-authored; add third-party documents and held-out queries before trusting the numbers.
4. OCR for scanned PDFs; PDF layout/tables; docx tables are flattened.
5. Auto-evaluation gate for feedback-driven ranking changes; multi-user concurrency (SQLite single writer).
6. Package metadata overhead (~70 tokens/item) could be trimmed further (id-only references, shared source table).
