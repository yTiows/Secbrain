# Secbrain — a second brain that maintains itself

A persistent, interconnected, continuously maintained knowledge system. It **acquires** documents and notes, **understands** them
(typed knowledge objects, claims with evidence, entities, relationships), **maintains** them (duplicates, contradictions, outdated
facts, missing links — all versioned and reversible) and **retrieves** exactly the relevant, explained, source-traceable
knowledge for people and for external AI agents.

Runs fully offline in one process: Python + SQLite (FTS5) + a static web UI. No LLM or model download is required
(an optional LLM provider hook exists, see *Status*).

## Quick start

```bash
pip install -e '.[dev]'
python -m brain.cli serve            # http://127.0.0.1:8765  (admin key written to data/admin.key, mode 600)
python -m brain.cli ingest ./my-notes --folders-as-projects     # import a folder / Obsidian vault
python -m brain.cli search "why did we choose postgres?" --context
python -m pytest                      # 92 tests
python -m benchmarks.run              # reproducible retrieval benchmark
```

Open the UI, paste the admin key. Views: **Graph** (zoom/pan/filter/cluster LOD, expand neighborhoods), **Explore**, **Search**
(ranked results or an AI context package), **Intel** (findings, contradictions, duplicates, gaps, changes with undo), **Vault**
(import, notes, review-before-commit, export Obsidian vault, backup, API keys).

## External API (versioned `/api/v1`, OpenAPI at `/api/v1/docs`)

```bash
curl -H "Authorization: Bearer $KEY" -X POST localhost:8765/api/v1/context \
  -d '{"query":"What is the access token lifetime?","token_budget":1000}' -H 'content-type: application/json'
```

| Operation | Endpoint | Scope |
|---|---|---|
| Search knowledge | `POST /search` | read |
| Contextual knowledge package (token-budgeted, sources, why, conflicts) | `POST /context` | read |
| Project context | `GET /projects/{name}/context` | read |
| Get object / relationships / history | `GET /objects/{id}[/relationships\|/history]` | read |
| Explore graph | `GET /graph/neighborhood/{id}`, `/graph/overview`, `/graph/path` | read |
| Submit information (stored as `external`, `ai_extracted`) | `POST /ingest/note`, `/ingest/file` | write |
| Propose an update (never applied without admin) | `POST /propose/update` | write |
| Maintenance, undo, vault, keys | `/maintenance/*`, `/changes/*`, `/vault/*`, `/keys` | admin |

Keys are random 256-bit tokens (only hashes stored). A key can be restricted to **projects** and/or **object types**; the
restriction is enforced inside retrieval/graph/object access, and out-of-scope objects behave as non-existent.

## Architecture in one screen

* **SQLite is the single authority** for objects, claims, versions, edges, jobs; originals are content-addressed immutable files.
  FTS index, embeddings and summaries are derived (`brain reindex`). Markdown vault = export projection (frontmatter `id` makes
  round-trips create *versions*, never duplicates). Details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
* **Ingestion**: extract → preserve original → sectionize → classify (16 types) → entities/aliases → claims (+evidence) → dedupe
  (unchanged / update / evidence-only / new / user-edit-protected) → relationship discovery → validate → **one atomic transaction**.
* **Relationships need evidence**: IDF-weighted rare shared entities, explicit mentions, cue phrases, claim conflicts — not mere
  similarity. Generic entities (“Python”) carry zero weight. User rejections are permanent tombstones.
* **Retrieval**: FTS5/BM25 ∪ semantic ∪ entity-linked → RRF-style fusion → bounded graph expansion (hub-damped) → grounding gate
  (similar-but-ungrounded results are dropped) → duplicate collapse → conflict surfacing → token-budgeted package.
* **Maintenance**: SAFE actions auto-apply (tags, summaries, suggested links, reindex, unsupported-claim flags); CONDITIONAL
  (merge, reclassify, supersede, resolve contradiction) need approval; PROTECTED (source integrity, security flags, purge) never
  automatic. Every change records an inverse; `undo` restores it.

## Status, honestly

See [`docs/DEVLOG.md`](docs/DEVLOG.md) for measured results and the full implemented/tested/limited table. Headlines:

* Default “semantic” search is **hashed TF-IDF**, not a neural model (no model downloads were possible). It captures lexical /
  morphological similarity; true synonymy relies on aliases (“also known as”, acronym definitions), user synonyms and graph links.
  Paraphrase queries with no shared vocabulary are the main retrieval weakness (benchmark: category `paraphrase`).
* Extraction is heuristic and English-centric. An `AnthropicLLM` provider is implemented behind `BRAIN_USE_LLM=1` but was **not
  exercised against the live API** in this build.
* No OCR (scanned PDFs fail explicitly). Single-user local deployment; SQLite single writer.
