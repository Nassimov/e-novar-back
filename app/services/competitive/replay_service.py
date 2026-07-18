from __future__ import annotations

"""
Replay Service — reserved seam, not implemented in Phase 1.

Replays require real match data (question order, answers, timings, score
evolution) that doesn't exist until the gameplay engine (phase 2+) exists.
Kept as an explicit stub so the module layout matches the target
architecture without inventing a table/format prematurely.
"""


def get_replay(match_id):  # noqa: ANN001, ANN201
    raise NotImplementedError("Replay Service ships in a future Competitive Arena phase.")
