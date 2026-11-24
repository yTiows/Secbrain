"""Composition root: wires the engines together."""
from __future__ import annotations
from .config import Settings
from .db import Database
from .embeddings import HashTfidfEmbedder
from .knowledge import KnowledgeStore
from .graph import GraphEngine
from .llm import make_llm
from .ingest.pipeline import IngestionEngine


class Brain:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.settings.ensure()
        self.db = Database(self.settings.db_path)
        self.embedder = HashTfidfEmbedder(self.settings.embedding_dim)
        self.store = KnowledgeStore(self.db, self.settings, self.embedder)
        self.graph = GraphEngine(self.db, self.store)
        self.llm = make_llm()
        self.ingest = IngestionEngine(self.db, self.store, self.graph, self.settings, self.llm)

    def close(self):
        self.ingest.stop()
        self.db.close()
