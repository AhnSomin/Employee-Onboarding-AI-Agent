"""Reciprocal rank fusion of several rankings (Cormack et al. 2009, k=60)."""

from __future__ import annotations

from collections.abc import Sequence


def reciprocal_rank_fusion(rankings: Sequence[Sequence[int]], k: int = 60) -> list[int]:
    """Items ordered by the sum of 1/(k + rank) over the rankings they appear in."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))
