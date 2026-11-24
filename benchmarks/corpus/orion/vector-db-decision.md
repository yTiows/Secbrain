---
project: Orion
date: 2025-06-18
tags: [decision, vector]
---
# Vector storage decision

We decided to use pgvector for vector search in Orion. The extension runs inside PostgreSQL, so we avoid operating a separate vector database for the prototype.

## Trade-offs
pgvector is slower than dedicated vector databases at very large scale, but the catalogue sample has fewer than one million vectors.
