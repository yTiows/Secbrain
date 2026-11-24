# Secbrain — Architecture (Phase 1)

## 1. Starting point (repository audit)

- Repository: empty (`git log` has no commits, no files). Foundation established from scratch.
- Toolchain available: Python 3.13, Node 22, SQLite 3.53 **with FTS5**, numpy, pypdf, python-docx, FastAPI/uvicorn (installable from PyPI).
- Constraints discovered: no outbound access to model hubs (HuggingFace blocked), no LLM API key configured. Consequence: the product must be fully functional **offline and deterministic**; LLM/embedding backends are optional, pluggable providers, never required.

## 2. Key decisions

| Decision | Choice | Rejected | Why |
|---|---|---|---|
| Language/runtime | Python 3.13 + FastAPI | Node/TS | Best ecosystem for extraction (pypdf, python-docx), numpy; single process |
| Primary store | **One SQLite file** (WAL, FTS5, FK) | Postgres + vector DB + graph DB | One transactional store gives atomic ingestion, FTS, graph, versioning with zero infra. Vault scale (10³–10⁵ objects) is well within SQLite |
| Vector index | float32 BLOBs in SQLite + numpy brute-force cosine, in-memory matrix cache | FAISS/pgvector/Chroma | Exact search, no extra service; 10⁵×1024 matmul ≈ ms. Swap-in point exists (`EmbeddingProvider`) |
| Graph | Edge table in SQLite + in-memory algorithms (components, label-propagation clusters, PageRank, bounded BFS) | Neo4j | Graph is queried through bounded, relevance-aware traversal, not Cypher. Persisted independently of the UI |
| Embeddings | Default `hash-tfidf-v1` (signed feature hashing of stemmed unigrams/bigrams/char-trigrams, corpus IDF) | sentence-transformers | No model downloads possible. **Honest limit:** captures lexical/morphological similarity, *not* true synonymy. Synonymy is covered by alias/entity expansion + user synonyms. Provider interface allows a neural model |
| LLM | `LLMProvider` interface; default `NullLLM` (heuristic pipeline); optional `AnthropicLLM` | LLM required | Offline determinism, testability, cost. LLM only refines (classification, relationship judgement); never required |
| Frontend | Static SPA (vanilla JS + Canvas, no build step, no CDN) | React + bundler | Zero build/CDN dependency, served by the API process. Custom force-directed canvas renderer with LOD/clustering |
| Background work | In-process worker thread + persisted `jobs` table (crash-recoverable) | Celery/Redis | Single process; job state is durable, resumed on restart |
| Auth | API keys (hashed), scopes `read/write/admin`, project/type scoping | none | Spec requires scoped external access |

## 3. Sources of truth (no competing authorities)

| Data | Authority | Derived / regenerable |
|---|---|---|
| Original uploaded files | `data/sources/<sha256>.<ext>` (content-addressed, immutable) + `sources` row | — |
| Extracted text of a source | `sources.text` (derived from file; re-extractable) | yes |
| Knowledge objects, claims, versions | SQLite | — |
| Graph edges + provenance + verification | SQLite `edges` / `edge_history` | — |
| Full-text index | SQLite FTS5 | rebuilt by `reindex` |
| Embeddings, summaries, IDF stats | SQLite | rebuilt by `reindex` |
| Markdown vault (Obsidian-compatible) | **export projection** (`vault export`) | Round-trips: re-import with the frontmatter `id` creates a new *version* of the object, never a duplicate |

Rule: Markdown files are a portable, human-readable *projection*, not a second writer. Edits made in an external vault enter through the import path (versioned, attributed `actor=import`).

## 4. Logical components (single process, module boundaries)

```
brain/
  config.py, db.py            H  Persistence & storage layer
  text.py, embeddings.py      B  Representation primitives (tokenize, stem, embed)
  knowledge.py                B  Knowledge objects, claims, versions, sources, changes
  graph.py                    C  Graph engine (edges, traversal, analytics)
  ingest/                     A  Ingestion engine
     extractors.py               file → text + metadata (txt/md/json/pdf/docx)
     security.py                 untrusted-input hygiene, injection flags
     structure.py                sectioning
     classify.py, entities.py, claims.py   knowledge extraction
     dedupe.py                   new / evidence / update / related / contradiction decision
     relationships.py        D  Relationship discovery
     pipeline.py                 orchestration, jobs, atomic commit
  retrieval.py                F  Hybrid retrieval + knowledge packaging
  maintenance.py              E  Findings, autonomy policy, rollback
  evolution.py                G  Consolidation, concept discovery, gaps, timeline, feedback
  vault.py                    Markdown import/export
  auth.py, api/app.py         J  External API (versioned /api/v1) + static UI host
web/                          I  UI
benchmarks/                   Retrieval benchmark (corpus + labelled queries + runner)
```

## 5. Data flow

**Ingest**: input → validate → extract text → store original (content-addressed) → sectionize → classify + entities + claims → dedupe vs. vault (new / evidence / update / related / contradiction) → relationship discovery (IDF-weighted shared entities, semantic sim, cue phrases) → validate → **single SQLite transaction** commit (objects, versions, claims, edges, FTS, embeddings) → job result recorded. Failure ⇒ rollback + `failed` job with explicit error; retry is idempotent (source identity = `(uri, sha256)`; object identity inside a source = `origin_key`).

**Retrieve**: query → analysis (entities, project, recency/type intent, alias expansion) → candidates (FTS5 BM25 ∪ cosine ∪ entity-linked objects) → fusion → bounded graph expansion (≤2 hops, typed/confidence-weighted, per-hop decay, node cap) → contextual scoring → relevance gate (rejects similar-but-ungrounded results) → near-duplicate collapse → conflict surfacing → token-budgeted package with per-item *why* + sources.

**Maintain**: scheduled/triggered inspections → `findings` (kind, level SAFE/CONDITIONAL/PROTECTED, confidence, explanation) → policy engine → auto-apply SAFE, queue CONDITIONAL for approval, never auto-apply PROTECTED → every applied change logged in `changes` with an inverse payload → `undo`.

## 6. Core data model

- `objects` (typed: fact, concept, idea, project, decision, experience, procedure, instruction, preference, research, reference, document, observation, hypothesis, question, entity; extensible via `extra`), `object_versions` (full snapshots), `tags`, `aliases`, `object_sources` (primary + evidence).
- `sources` (original bytes, sha256, uri, extracted text, security flags) are separate from `claims` (subject/predicate/value + evidence sentence + source ref + confidence + verification). AI interpretations are never presented as established facts: `verification ∈ {ai_extracted, unverified, user_verified, disputed}`.
- `edges` (typed, directed; origin user/import/ai; confidence; explanation; evidence; verification suggested/verified/rejected). Rejected edges persist as tombstones so discovery never resurrects them.

## 7. Risk assessment

| Risk | Mitigation |
|---|---|
| Heuristic extraction is weaker than an LLM | Pluggable LLM refinement; every AI output carries confidence + `ai_extracted`; benchmark and tests quantify actual quality; limitations documented |
| No neural embeddings → weak synonymy | Alias/acronym extraction, user synonyms, entity-linked candidate generation, graph expansion; neural provider swap-in; benchmark reports synonym-query results honestly |
| Over-connection (everything similar to everything) | IDF-weighted entity evidence, generic-entity damping, evidence required (not just similarity), rejection tombstones, relevance gate in retrieval |
| Prompt injection via documents | Documents are data; heuristic pipeline never interprets instructions; injection patterns flagged on source and objects; LLM provider receives content only inside data delimiters with a fixed system prompt |
| Partial writes | One transaction per ingestion; crash-recovered job table |
| SQLite single writer | Single write lock; reads concurrent under WAL; fine for personal vault |
| Auth leakage via API | Hashed keys, scopes, project/type scoping applied inside the retrieval/graph layer, not just routes |

## 8. Development sequence

1 Audit/architecture → 2 Knowledge foundation → 3 Graph → 4 Ingestion → 5 Retrieval + benchmark → 6 Maintenance → 7 UI + API → 8 QA. Each phase ends with passing tests and an entry in `docs/DEVLOG.md`.
