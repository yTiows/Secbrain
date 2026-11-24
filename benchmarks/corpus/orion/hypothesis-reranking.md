---
project: Orion
date: 2025-05-20
tags: [hypothesis]
---
# Hypothesis: reranking

We suspect that a cross-encoder reranker would lift precision at 5 above 0.75 for hybrid retrieval, because most remaining errors are near-miss documents from the same topic. This might not hold for very short queries.
