---
project: Orion
date: 2025-03-05
tags: [rag, research]
---
# Retrieval-Augmented Generation notes

Retrieval-Augmented Generation (RAG) combines a retriever with a language model so answers are grounded in documents. The quality of RAG depends mostly on retrieval quality.

## Chunking
Small chunks improve precision but lose context. Chunks of 200 to 400 tokens with overlap were a good default in published benchmarks.

## Hybrid search
Combining BM25 keyword search with dense embeddings consistently outperformed either alone in the surveys we read. Reranking the merged candidates gives a further gain.
