"""Okapi BM25 over pre-tokenized documents. Rebuilt from the documents; nothing is stored."""

from __future__ import annotations

import math
from collections import Counter

import numpy as np


class BM25Index:
    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.freqs = [Counter(doc) for doc in documents]
        self.lengths = np.array([len(doc) for doc in documents], dtype=np.float32)
        self.avg_length = float(self.lengths.mean()) if len(documents) else 0.0
        df: Counter[str] = Counter()
        for freq in self.freqs:
            df.update(freq.keys())
        n = len(documents)
        self.idf = {term: math.log(1 + (n - count + 0.5) / (count + 0.5)) for term, count in df.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(len(self.freqs), dtype=np.float32)
        if not self.freqs:
            return out
        norm = self.k1 * (1 - self.b + self.b * self.lengths / max(self.avg_length, 1e-9))
        for term in dict.fromkeys(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            tf = np.array([freq.get(term, 0) for freq in self.freqs], dtype=np.float32)
            out += idf * tf * (self.k1 + 1) / (tf + norm)
        return out

    def matched(self, query: list[str], index: int) -> set[str]:
        """Query terms that occur in document `index`."""
        return {term for term in query if term in self.freqs[index]}
