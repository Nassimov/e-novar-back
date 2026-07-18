from __future__ import annotations

"""
Ranking Service — Competitive Arena Phase 1.

Phase 1 does not calculate rating changes yet (no match results exist to
rate). This module only provides the pure rating->tier mapping, so a
student's default rating (1000) already displays a real, correct tier
("Bronze") instead of a hardcoded placeholder. Future phases will update
CompetitiveStatistics.rating after each match and call get_rank_tier() to
detect promotions/demotions (see the competitive_rank_promoted/demoted
notification templates already seeded in migration 074).
"""

#: (floor rating, tier) pairs, checked from highest to lowest. The starting
#: rating (1000, the standard ELO default) deliberately falls in the Bronze
#: range — every new player starts at the bottom of the ladder.
_TIER_THRESHOLDS = [
    (2100, "legend"),
    (1900, "master"),
    (1700, "diamond"),
    (1500, "platinum"),
    (1300, "gold"),
    (1100, "silver"),
    (0, "bronze"),
]


def get_rank_tier(rating: int) -> str:
    for floor, tier in _TIER_THRESHOLDS:
        if rating >= floor:
            return tier
    return "bronze"
