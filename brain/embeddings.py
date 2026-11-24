"""Embedding providers. Default: deterministic hashed TF-IDF vectors (no model download).

Honest scope: these capture lexical/morphological overlap weighted by corpus rarity.
They do not know that "car" ~ "automobile"; synonymy is handled via aliases/entities.
"""
from __future__ import annotations
import hashlib
import math
import re
from typing import Protocol

import numpy as np

from .text import tokens, words, stem, normalize_unicode, STOPWORDS


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, text: str) -> np.ndarray: ...
    def observe(self, text: str) -> None: ...      # update corpus statistics
    def reset_stats(self) -> None: ...


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "little")


class HashTfidfEmbedder:
    name = "hash-tfidf-v1"

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.df = np.zeros(dim, dtype=np.float64)
        self.n_docs = 0
        self._cache: dict[str, tuple[int, float]] = {}

    # --- feature extraction -------------------------------------------------
    def _slot(self, feat: str) -> tuple[int, float]:
        r = self._cache.get(feat)
        if r is None:
            h = _h(feat)
            r = (h % self.dim, 1.0 if (h >> 40) & 1 else -1.0)
            if len(self._cache) < 200_000:
                self._cache[feat] = r
        return r

    def features(self, text: str) -> dict[str, float]:
        toks = tokens(text)
        f: dict[str, float] = {}
        for t in toks:
            f["u:" + t] = f.get("u:" + t, 0) + 1
        for a, b in zip(toks, toks[1:]):
            k = f"b:{a} {b}"
            f[k] = f.get(k, 0) + 0.7
        for w in set(toks):
            if len(w) >= 6 and w.isalpha():
                for i in range(len(w) - 2):
                    k = "c:" + w[i:i + 3]
                    f[k] = f.get(k, 0) + 0.12
        return f

    def _idf(self, idx: int) -> float:
        return math.log((self.n_docs + 1) / (self.df[idx] + 1)) + 1.0

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for feat, tf in self.features(text).items():
            idx, sign = self._slot(feat)
            w = (1 + math.log(tf)) if tf >= 1 else tf
            v[idx] += sign * w * self._idf(idx)
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    # --- corpus stats ---------------------------------------------------------
    def observe(self, text: str) -> None:
        seen = {self._slot(f)[0] for f in self.features(text)}
        for i in seen:
            self.df[i] += 1
        self.n_docs += 1

    def reset_stats(self) -> None:
        self.df[:] = 0
        self.n_docs = 0

    def dump_stats(self) -> bytes:
        return self.n_docs.to_bytes(8, "little") + self.df.astype(np.float32).tobytes()

    def load_stats(self, blob: bytes) -> None:
        if not blob or len(blob) != 8 + 4 * self.dim:
            return
        self.n_docs = int.from_bytes(blob[:8], "little")
        self.df = np.frombuffer(blob[8:], dtype=np.float32).astype(np.float64).copy()


def make_embedder(dim: int = 1024) -> HashTfidfEmbedder:
    return HashTfidfEmbedder(dim)
