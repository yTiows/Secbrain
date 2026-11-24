---
project: Orion
date: 2025-05-12
tags: [experiments, retrieval]
---
# Experiment log: hybrid retrieval

## Results
We evaluated three retrieval setups on 120 labelled queries. Semantic search alone reached precision at 5 of 0.64. Hybrid search reached precision at 5 of 0.71. The benchmark shows hybrid search improves recall for multi-word queries.

## Observation
Latency stayed under 40 ms for all three setups. We noticed that acronym queries failed unless aliases were expanded.
