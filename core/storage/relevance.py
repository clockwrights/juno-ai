"""Relevance and decay (ADR-11, docs/04 §4).

    relevance = importance × (1 + ln(1 + access_count)) × exp(−λ × days)

Computed at read time and never stored. v1 kept a `relevance_score` attribute
with a GSI, which was stale the moment it was written, and used
`ln(1 + access_count)` alone — making every never-accessed memory score exactly
zero regardless of how important it was. The `1 +` is the fix.
"""

from __future__ import annotations

import datetime as dt
import math

#: Half-life of about 35 days. A CDK context value in later VPs; a constant here
#: so the formula is testable without configuration.
DECAY_LAMBDA = 0.02


def relevance(
    *,
    importance: float,
    access_count: int,
    last_accessed: dt.datetime,
    now: dt.datetime,
    decay_lambda: float = DECAY_LAMBDA,
) -> float:
    """Score a memory. Higher survives consolidation; nothing is ever deleted."""
    if importance <= 0:
        return 0.0
    days = max(0.0, (now - last_accessed).total_seconds() / 86400.0)
    frequency = 1.0 + math.log(1.0 + max(0, access_count))
    return importance * frequency * math.exp(-decay_lambda * days)


def normalise(score: float, ceiling: float = 25.0) -> float:
    """Map a relevance score into 0..1 for blending with cosine similarity.

    The ceiling is importance 5 × a well-used memory; anything above it is
    already maximally relevant and the difference does not matter for ranking.
    """
    return min(1.0, max(0.0, score / ceiling))


def rank_score(cosine_similarity: float, relevance_score: float) -> float:
    """docs/04 §3 step 3: 0.7 similarity, 0.3 relevance.

    Similarity dominates because the agent asked for something specific;
    relevance breaks ties toward memories that have earned their place.
    """
    return 0.7 * cosine_similarity + 0.3 * normalise(relevance_score)
