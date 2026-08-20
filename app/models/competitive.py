from __future__ import annotations

"""
Competitive Arena — Phase 1: Foundation & Architecture.

Mirrors migration 074_competitive_arena_foundation.sql. This phase only
builds the pre-match architecture (match lifecycle, lobby, invitations,
empty statistics row) — no gameplay/question-serving logic yet. See
app/services/competitive/ for the domain services and
app/routers/competitive/ for the REST API.

A "lobby" is not a separate table — a CompetitiveMatch IS the lobby before
it starts (status draft/waiting_for_opponent/accepted/scheduled/waiting_room/
countdown); this avoids duplicating a second near-identical entity.

Reused, not duplicated: profiles (creator/participants/invitees), subjects,
levels (existing reference tables), and the existing Question/QuestionChoice/
QuizAttempt/QuizAnswer tables in app/models/practice.py, which the future
Question Engine will read from.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlmodel import Field, SQLModel

#: Every match type Phase 1's schema must support without future redesign.
MATCH_TYPES = ["duel", "club_battle", "battle_royale", "tournament"]

#: Full lifecycle. Phase 1 only ever drives a match up through
#: waiting_room/countdown (once every participant is ready) — in_progress
#: onward is reserved for the future gameplay engine (phase 2+).
MATCH_STATUSES = [
    "draft", "waiting_for_opponent", "accepted", "scheduled", "waiting_room",
    "countdown", "in_progress", "paused", "completed", "cancelled", "expired",
    "abandoned", "disconnected",
]

INVITATION_STATUSES = ["pending", "accepted", "declined", "cancelled", "expired"]

RANK_TIERS = ["bronze", "silver", "gold", "platinum", "diamond", "master", "grand_master", "legend"]


class CompetitiveMatch(SQLModel, table=True):
    """Mirrors public.competitive_matches. One row per match — also the lobby
    representation before the match starts (see module docstring)."""

    __tablename__ = "competitive_matches"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_type: str = Field()
    status: str = Field(default="draft")
    # Phase 10 — nullable: a Battle Royale can be admin-created with no
    # single "creator" player (open lobby, no inviter/invitee concept —
    # mirrors why CompetitiveTournament.organizer_id is nullable, since
    # admins are never profiles rows in this codebase). Every other
    # match_type is unaffected — the application layer still always
    # populates this for duels/tournaments/club_battle.
    creator_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id", index=True)
    subject_ids: List[UUID] = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.UUID(as_uuid=True)), nullable=False, server_default="{}"),
    )
    school_level_id: Optional[UUID] = Field(default=None, foreign_key="levels.id")
    difficulty: Optional[str] = Field(default=None)
    question_count: int = Field(default=10)
    visibility: str = Field(default="private")
    max_players: int = Field(default=2)
    scheduled_at: Optional[datetime] = Field(default=None)
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    cancelled_at: Optional[datetime] = Field(default=None)
    cancelled_reason: Optional[str] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None)
    created_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    deleted_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Phase 3 — which position (0-indexed into competitive_match_questions)
    # is currently active. O(1) lookup instead of inferring progress from
    # locked_at/revealed_at IS NULL scans.
    current_question_position: Optional[int] = Field(default=None)
    # Phase 9 — per-match spectator cap override (nullable = fall back to
    # PlatformSettings.competitive_spectator_max_default). Lives here rather
    # than on competitive_tournaments since it's useful for ANY match, not
    # tournament-specific — see app/main.py's websocket_competitive_spectate.
    max_spectators: Optional[int] = Field(default=None)
    # Phase 13 — Ranked vs Casual (spec's core distinction). Default True
    # preserves pre-Phase-13 behavior (every match already affected rating);
    # casual is an explicit opt-in. gameplay_service.finish_match checks
    # this before calling ranking_service.apply_match_result — a casual
    # match still fully updates lifetime counters/stats, only Arena Rating/
    # league movement is skipped (mirrors Battle Royale's pre-existing
    # ranking_impact toggle, generalized to every match_type).
    is_ranked: bool = Field(default=True)


class CompetitiveMatchParticipant(SQLModel, table=True):
    """Mirrors public.competitive_match_participants."""

    __tablename__ = "competitive_match_participants"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    role: str = Field(default="player")  # player|spectator (future)
    is_ready: bool = Field(default=False)
    result: Optional[str] = Field(default=None)  # win|loss|draw — set by future gameplay engine
    score: int = Field(default=0)  # set by future gameplay engine
    joined_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    left_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Phase 10 — Battle Royale-only columns, NULL for every other match_type
    # (mirrors how CompetitiveMatch.max_spectators was added in Phase 9:
    # reuse the existing per-participant row rather than a parallel table).
    lives: Optional[int] = Field(default=None)  # only meaningful for elimination_mode='lives'
    elimination_round: Optional[int] = Field(default=None)  # question_position eliminated at; NULL = still active / no elimination
    final_rank: Optional[int] = Field(default=None)  # 1..N final placement once the match completes
    eliminated_at: Optional[datetime] = Field(default=None)
    # Phase 11 (Part C) — Club Battle-only columns, NULL for every other
    # match_type (same precedent as the Phase 10 BR-only columns above).
    club_id: Optional[UUID] = Field(default=None, foreign_key="clubs.id")
    team_side: Optional[str] = Field(default=None)  # 'a'|'b', only for match_type='club_battle'


class CompetitiveInvitation(SQLModel, table=True):
    """Mirrors public.competitive_invitations. Tracked separately from
    participants since a declined/expired invite never becomes one."""

    __tablename__ = "competitive_invitations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    inviter_id: UUID = Field(foreign_key="profiles.id", index=True)
    invitee_id: UUID = Field(foreign_key="profiles.id", index=True)
    status: str = Field(default="pending")
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc) + timedelta(minutes=15)
    )
    responded_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveStatistics(SQLModel, table=True):
    """Mirrors public.competitive_statistics. One lazily-created row per
    student (see app/services/competitive/statistics_service.py). Phase 1
    only creates the schema — every counter stays at its zero default until
    a future phase wires real match results into it."""

    __tablename__ = "competitive_statistics"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    matches_played: int = Field(default=0)
    wins: int = Field(default=0)
    losses: int = Field(default=0)
    draws: int = Field(default=0)
    accuracy_pct: float = Field(default=0)
    avg_response_time_ms: Optional[int] = Field(default=None)
    current_streak: int = Field(default=0)
    longest_streak: int = Field(default=0)
    favorite_subject_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    favorite_difficulty: Optional[str] = Field(default=None)
    perfect_matches: int = Field(default=0)
    rank_tier: str = Field(default="bronze")
    best_rank_tier: str = Field(default="bronze")
    rating: int = Field(default=1000)
    # Phase 7 — precise, division-aware pointer into competitive_leagues
    # (rank_tier/best_rank_tier stay as the coarse family slug for
    # backward-compatible display). highest_rating is the spec's "Highest
    # Rating" core principle field.
    league_id: Optional[UUID] = Field(default=None, foreign_key="competitive_leagues.id")
    highest_rating: int = Field(default=1000)
    tournament_wins: int = Field(default=0)
    club_wins: int = Field(default=0)
    battle_royale_wins: int = Field(default=0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Module-scoped suspension (Phase 2) — mirrors StudentProfile.booking_
    # suspended_until's pattern but only blocks competitive participation,
    # not the whole platform (no platform-wide ban system exists).
    suspended_until: Optional[datetime] = Field(default=None)
    suspended_reason: Optional[str] = Field(default=None)
    # Phase 4 — long-term breakdowns, updated after every completed match by
    # app/services/competitive/analysis_service.py. Shape:
    # subject_breakdown: {subject_id: {matches,correct,wrong,accuracy_pct,avg_response_time_ms}}
    # difficulty_breakdown: {easy|medium|hard|expert: {...same...}}
    subject_breakdown: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    difficulty_breakdown: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    # Phase 13 — Ranked Ladder V2.
    placement_matches_played: int = Field(default=0)
    placement_complete: bool = Field(default=False)
    fair_play_score: int = Field(default=100)
    # Reserved: mirrors `rating` until a genuinely divergent internal
    # matchmaking value is ever needed (spec: "architecture ready, not
    # visible to users") — never displayed, only read by matchmaking_
    # service if/when it starts diverging from public Arena Rating.
    matchmaking_rating: Optional[int] = Field(default=None)
    last_ranked_match_at: Optional[datetime] = Field(default=None)
    inactivity_decay_applied_at: Optional[datetime] = Field(default=None)
    season_best_streak: int = Field(default=0)


class CompetitiveScheduleProposal(SQLModel, table=True):
    """Mirrors public.competitive_schedule_proposals — Phase 2's slot
    negotiation for a "Scheduled Match" (skipped entirely for immediate
    matches). Unlimited back-and-forth: each new proposal from either party
    supersedes the match's previous pending one."""

    __tablename__ = "competitive_schedule_proposals"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    proposed_by: UUID = Field(foreign_key="profiles.id")
    proposed_at_time: datetime = Field()
    timezone_label: Optional[str] = Field(default=None)
    status: str = Field(default="pending")  # pending|accepted|declined|superseded
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    responded_at: Optional[datetime] = Field(default=None)


class CompetitiveMatchEvent(SQLModel, table=True):
    """Mirrors public.competitive_match_events — append-only per-match audit
    trail (invitation lifecycle, ready toggles, disconnects, admin actions)."""

    __tablename__ = "competitive_match_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    actor_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    event_type: str = Field()
    meta: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Phase 3: lets a future consumer (Ranking Engine in Phase 7, Reward
    # Engine later) safely claim 'match_finished'/'rewards_pending' events
    # exactly once — see app/services/competitive/gameplay_service.py's
    # finish_match() for what gets logged here.
    processed_at: Optional[datetime] = Field(default=None)


class CompetitiveMatchQuestion(SQLModel, table=True):
    """Mirrors public.competitive_match_questions — Phase 3: the
    authoritative, generated-once-at-match-start ordered question list for a
    match. References app/models/practice.py's Question (never duplicates
    question content); only stores this match's per-question timing
    windows. Immutable once created — "lock match configuration"."""

    __tablename__ = "competitive_match_questions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    question_id: UUID = Field(foreign_key="questions.id")
    position: int = Field()
    reading_ends_at: Optional[datetime] = Field(default=None)
    answer_ends_at: Optional[datetime] = Field(default=None)
    locked_at: Optional[datetime] = Field(default=None)
    revealed_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveQuestionAttempt(SQLModel, table=True):
    """Mirrors public.competitive_question_attempts — one row per (match
    question, player) answer. Kept separate from practice.py's QuizAnswer
    since a duel attempt has a different lifecycle owner (a match, not a
    solo QuizAttempt) and different anti-cheat/timing columns."""

    __tablename__ = "competitive_question_attempts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_question_id: UUID = Field(foreign_key="competitive_match_questions.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    selected_choice_ids: List[UUID] = Field(
        default_factory=list,
        sa_column=sa.Column(ARRAY(sa.UUID(as_uuid=True)), nullable=False, server_default="{}"),
    )
    is_correct: bool = Field(default=False)
    response_time_ms: Optional[int] = Field(default=None)
    points_earned: int = Field(default=0)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Phase 4 — set only when is_correct=False, by
    # app/services/competitive/analysis_service.py's timing heuristic
    # (guess|time_pressure|knowledge_gap). TEXT (no enum/CHECK) so finer
    # future categories (calculation/concept/reading mistake — which need
    # free-text/formula question types, not yet enabled) need no migration.
    mistake_category: Optional[str] = Field(default=None)


class CompetitiveMatchPlayerStats(SQLModel, table=True):
    """Mirrors public.competitive_match_player_stats — final per-player-per-
    match tally, written once at match completion. Fast reads for history/
    profile without re-aggregating raw attempts every time."""

    __tablename__ = "competitive_match_player_stats"

    match_id: UUID = Field(foreign_key="competitive_matches.id", primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", primary_key=True)
    score: int = Field(default=0)
    correct_count: int = Field(default=0)
    wrong_count: int = Field(default=0)
    unanswered_count: int = Field(default=0)
    accuracy_pct: float = Field(default=0)
    avg_response_time_ms: Optional[int] = Field(default=None)
    best_streak: int = Field(default=0)
    result: str = Field()  # win|loss|draw
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveMatchReplay(SQLModel, table=True):
    """Mirrors public.competitive_match_replays — Phase 4's generation
    envelope. The actual question-by-question content is never duplicated
    here (already immutable in competitive_match_questions/
    competitive_question_attempts) — only generation status/timing and a
    precomputed score_timeline for fast rendering."""

    __tablename__ = "competitive_match_replays"

    match_id: UUID = Field(foreign_key="competitive_matches.id", primary_key=True)
    status: str = Field(default="pending")  # pending|processing|ready|failed
    score_timeline: Any = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"),
    )
    duration_sec: Optional[int] = Field(default=None)
    error: Optional[str] = Field(default=None)
    generated_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Phase 12 — Replay Privacy (migration 092). 'friends' is accepted but
    # behaves identically to 'private' in V1 — no friends-graph exists
    # anywhere in this codebase (see migration 092's header). visibility_
    # locked = an admin override that a student can no longer change.
    visibility: str = Field(default="private")
    visibility_locked: bool = Field(default=False)
    deleted_at: Optional[datetime] = Field(default=None)
    deleted_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")


class CompetitiveMatchAiAnalysis(SQLModel, table=True):
    """Mirrors public.competitive_match_ai_analysis — one row per
    (match, player). Real Claude-generated (see app/services/ai.py's
    generate_match_analysis) or template-fallback educational report."""

    __tablename__ = "competitive_match_ai_analysis"

    match_id: UUID = Field(foreign_key="competitive_matches.id", primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", primary_key=True)
    status: str = Field(default="pending")  # pending|processing|ready|failed
    overall_summary: Optional[str] = Field(default=None)
    strengths: Any = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"),
    )
    weaknesses: Any = Field(
        default_factory=list,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'[]'::jsonb"),
    )
    behavior_notes: Optional[str] = Field(default=None)
    mastery_level: Optional[str] = Field(default=None)  # debutant|intermediaire|avance|expert
    confidence: Optional[str] = Field(default=None)  # high|medium|low
    progress_comparison: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    error: Optional[str] = Field(default=None)
    generated_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Phase 12 — Match Rating (migration 092). Deterministic, computed by
    # analysis_service.compute_match_grade from real CompetitiveMatchPlayerStats
    # (accuracy/response-time/result) — never AI-generated, so it stays
    # reproducible and evidence-based like every other field here.
    grade: Optional[str] = Field(default=None)  # A+|A|B+|B|C|D|F


class CompetitiveReplayFavorite(SQLModel, table=True):
    """Mirrors public.competitive_replay_favorites — Phase 12."""

    __tablename__ = "competitive_replay_favorites"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveReplayView(SQLModel, table=True):
    """Mirrors public.competitive_replay_views — append-only watch-session
    log, Phase 12. Powers view_count/avg_watch_time/completion_rate via a
    live aggregate query (replay_service.get_replay_stats) — no separate
    mutable counters table (same precedent as migration 079's queue
    monitoring)."""

    __tablename__ = "competitive_replay_views"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id")
    watch_duration_sec: int = Field(default=0)
    completed: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveRecommendation(SQLModel, table=True):
    """Mirrors public.competitive_recommendations — persisted, student-
    level, active recommendations. New matches supersede (not delete)
    obsolete ones from the same category."""

    __tablename__ = "competitive_recommendations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    source_match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_matches.id")
    category: str = Field()  # practice|session|teacher|challenge|arena
    title: str = Field()
    description: str = Field()
    confidence: str = Field(default="medium")  # high|medium|low
    action_type: Optional[str] = Field(default=None)
    action_payload: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    status: str = Field(default="active")  # active|dismissed|completed|obsolete
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    superseded_at: Optional[datetime] = Field(default=None)


class CompetitiveLearningRoadmapStep(SQLModel, table=True):
    """Mirrors public.competitive_learning_roadmap_steps — regenerated
    after every match."""

    __tablename__ = "competitive_learning_roadmap_steps"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    source_match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_matches.id")
    position: int = Field()
    title: str = Field()
    description: Optional[str] = Field(default=None)
    status: str = Field(default="pending")  # pending|done
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveMatchConnection(SQLModel, table=True):
    """Mirrors public.competitive_match_connections — Phase 5's durable
    connection/latency audit trail (analytics/moderation/fraud-detection).
    The hot "is this user online right now" path stays in Redis (see
    presence_service.py, Phase 2) — this table is the persisted record,
    never queried on the gameplay hot path."""

    __tablename__ = "competitive_match_connections"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    event_type: str = Field()  # connected|disconnected|reconnected|heartbeat_timeout
    latency_ms: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveQueueEntry(SQLModel, table=True):
    """Mirrors public.competitive_queue_entries — Phase 6's matchmaking
    queue. Exact-match hard filters (subject/level/difficulty) live directly
    on the entry — no separate saved-preferences table (the Challenge
    wizard already asks fresh each time; matchmaking does the same). Arena
    Rating reuses CompetitiveStatistics.rating verbatim (rating_at_join is a
    snapshot for this search — Phase 7 is what actually adjusts the live
    value after a match)."""

    __tablename__ = "competitive_queue_entries"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    mode: str = Field(default="ranked")  # ranked|friendly
    subject_id: UUID = Field(foreign_key="subjects.id")
    school_level_id: UUID = Field(foreign_key="levels.id")
    difficulty: str = Field()
    language: Optional[str] = Field(default=None)
    region: str = Field(default="global")  # architecture-ready only, unused for filtering in V1
    status: str = Field(default="searching")
    rating_at_join: int = Field()
    search_started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    current_radius: int = Field(default=50)
    proposal_group_id: Optional[UUID] = Field(default=None)
    matched_with_user_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_matches.id")
    proposal_expires_at: Optional[datetime] = Field(default=None)
    accepted_at: Optional[datetime] = Field(default=None)
    ended_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveQueueEvent(SQLModel, table=True):
    """Mirrors public.competitive_queue_events — append-only pre-match audit
    trail (join/leave/expansion/matched/accepted/declined/timeout), mirroring
    the competitive_match_events convention for anything that happens before
    a match_id exists yet."""

    __tablename__ = "competitive_queue_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    queue_entry_id: Optional[UUID] = Field(default=None, foreign_key="competitive_queue_entries.id")
    event_type: str = Field()
    meta: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Phase 7 — Ranking System, Seasons & Competitive Leagues ────────────────

class CompetitiveLeague(SQLModel, table=True):
    """Mirrors public.competitive_leagues — the ordered league/division
    ladder. Replaces the fully-hardcoded 7-tier ranking_service thresholds
    with a real, admin-editable table (spec: "No hardcoded values" for
    league thresholds/icons/colors/promotion/demotion rules). Each row is
    one division (e.g. Bronze/III..Bronze/I, up through undivided
    Grand Master/Legend at the top); ordered by sort_order."""

    __tablename__ = "competitive_leagues"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    league_name: str = Field()
    division_label: Optional[str] = Field(default=None)  # III|II|I, None for undivided top leagues
    sort_order: int = Field()
    min_mmr: int = Field()
    max_mmr: Optional[int] = Field(default=None)  # None = open-ended (top league)
    icon: Optional[str] = Field(default=None)
    color: Optional[str] = Field(default=None)
    promotion_bonus_ep: int = Field(default=0)
    demotion_protection_matches: int = Field(default=3)
    visible: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveSeason(SQLModel, table=True):
    """Mirrors public.competitive_seasons. Only one row may have
    status='active' at a time (DB partial unique index). Season rows and
    their competitive_season_stats are NEVER deleted — "history must never
    be lost" — a season is only ever frozen (status='ended'), never
    archived-away or purged."""

    __tablename__ = "competitive_seasons"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field()
    description: Optional[str] = Field(default=None)
    status: str = Field(default="upcoming")  # upcoming|active|ended
    starts_at: datetime = Field()
    ends_at: datetime = Field()
    reset_strategy: str = Field(default="soft")  # soft|hard|percentage
    reset_percentage: Optional[int] = Field(default=None)
    rules: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveRatingHistory(SQLModel, table=True):
    """Mirrors public.competitive_rating_history — one row per (match,
    user): MMR before/after/delta + league transition + season. Everything
    else about the match (opponent name, subject, difficulty, duration,
    replay) is read live from competitive_matches/competitive_match_player_
    stats/competitive_match_replays — never duplicated here."""

    __tablename__ = "competitive_rating_history"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    # Phase 13 — nullable: an inactivity-decay adjustment (ranking_service.
    # apply_inactivity_decay) has no underlying match to point at, unlike
    # every match-driven row here.
    match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_matches.id")
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    opponent_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    season_id: Optional[UUID] = Field(default=None, foreign_key="competitive_seasons.id")
    result: str = Field()  # win|loss|draw|decay
    rating_before: int = Field()
    rating_after: int = Field()
    delta: int = Field()
    league_before_id: Optional[UUID] = Field(default=None, foreign_key="competitive_leagues.id")
    league_after_id: Optional[UUID] = Field(default=None, foreign_key="competitive_leagues.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveFairPlayEvent(SQLModel, table=True):
    """Mirrors public.competitive_fair_play_events — Phase 13's audit
    ledger behind CompetitiveStatistics.fair_play_score (a running counter,
    starts at 100). Written by fair_play_service.py — never directly by
    callers, same "one funnel" convention as reputation_service.award_
    reputation for clubs."""

    __tablename__ = "competitive_fair_play_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_matches.id")
    delta: int = Field()
    reason: str = Field()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveSeasonStats(SQLModel, table=True):
    """Mirrors public.competitive_season_stats — the durable, continuously-
    updated per-season leaderboard source of truth. Each finished match
    updates only the two participants' rows (O(1) per match — satisfies
    "avoid recalculating the entire leaderboard, only update impacted
    players"). final_rank is set once, at season end."""

    __tablename__ = "competitive_season_stats"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    season_id: UUID = Field(foreign_key="competitive_seasons.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    mmr: int = Field()
    league_id: Optional[UUID] = Field(default=None, foreign_key="competitive_leagues.id", index=True)
    matches_played: int = Field(default=0)
    wins: int = Field(default=0)
    losses: int = Field(default=0)
    draws: int = Field(default=0)
    accuracy_pct: float = Field(default=0)
    avg_response_time_ms: Optional[int] = Field(default=None)
    final_rank: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveSeasonReward(SQLModel, table=True):
    """Mirrors public.competitive_season_rewards — admin-configured reward
    rules for a season, targeted either by final league or by final global
    rank range (e.g. top 10)."""

    __tablename__ = "competitive_season_rewards"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    season_id: UUID = Field(foreign_key="competitive_seasons.id")
    league_id: Optional[UUID] = Field(default=None, foreign_key="competitive_leagues.id")
    rank_min: Optional[int] = Field(default=None)
    rank_max: Optional[int] = Field(default=None)
    reward_type: str = Field()  # ep|badge|sticker|frame|title|avatar_decoration
    reward_ref: Optional[str] = Field(default=None)
    reward_amount: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveRewardGrant(SQLModel, table=True):
    """Mirrors public.competitive_reward_grants — audit trail of every
    reward actually distributed. status='granted' means it was actually
    applied via real infrastructure (KP award, UserBadge); status=
    'recorded_only' means the grant is logged but not auto-delivered
    (sticker/frame/title/avatar_decoration — no per-item ownership/equip
    system exists yet for those, see Phase 7's final report)."""

    __tablename__ = "competitive_reward_grants"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    season_id: Optional[UUID] = Field(default=None, foreign_key="competitive_seasons.id")
    source: str = Field()  # season_end|promotion|tournament|battle_royale
    reward_type: str = Field()
    reward_ref: Optional[str] = Field(default=None)
    reward_amount: Optional[int] = Field(default=None)
    status: str = Field(default="granted")
    granted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Phase 9 Part B — migration 085. Only ever set when source='tournament';
    # lets a "my rewards" view unambiguously group a grant back to the
    # tournament it came from (season_id is irrelevant for this source).
    tournament_id: Optional[UUID] = Field(default=None, foreign_key="competitive_tournaments.id")
    # Phase 10 Part A — migration 086. Only ever set when
    # source='battle_royale'. Reuses competitive_matches.id (a Battle
    # Royale IS a competitive_matches row, no separately-named FK needed).
    battle_royale_match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_matches.id")


# ─── Phase 8 — Spectator Mode, Live Reactions & Predictions ─────────────────

#: Fixed V1 reaction set — no custom emoji (spec explicit V1 scope decision).
REACTION_EMOJI_SET = ["🔥", "👏", "❤️", "😲", "🎉", "😅", "💯", "🚀"]


class CompetitiveSpectator(SQLModel, table=True):
    """Mirrors public.competitive_spectators — one row per spectate session
    (join/leave), mirroring CompetitiveMatchConnection's audit-trail role but
    for spectators. Live "who's watching right now" count is Redis-backed
    (see app/services/competitive/spectator_presence_service.py) — this
    table is the durable record, never queried on the hot broadcast path."""

    __tablename__ = "competitive_spectators"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    joined_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    left_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveMatchReaction(SQLModel, table=True):
    """Mirrors public.competitive_match_reactions. emoji is validated
    against REACTION_EMOJI_SET at the API layer (Pydantic), not a DB CHECK —
    matches this codebase's existing convention (see MATCH_TYPES/
    MATCH_STATUSES validated the same way)."""

    __tablename__ = "competitive_match_reactions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    emoji: str = Field()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitivePrediction(SQLModel, table=True):
    """Mirrors public.competitive_predictions — a pick, never a bet.
    predicted_winner_id=None means a draw prediction. Resolved once, at
    match completion (gameplay_service.finish_match ->
    spectator_service.resolve_predictions_for_match): is_correct set,
    xp_awarded credited to "Arena XP"/"Spectator XP" ONLY (never Arena
    Rating/MMR, never EP — spec's explicit "no wagering" rule)."""

    __tablename__ = "competitive_predictions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    predicted_winner_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    is_correct: Optional[bool] = Field(default=None)
    xp_awarded: int = Field(default=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveMatchChat(SQLModel, table=True):
    """Mirrors public.competitive_match_chat — spectator chat, distinct from
    the platform's own DM messaging system (app/models/conversation.py).
    Soft-deleted (deleted_at/deleted_by) rather than hard-deleted, so a
    moderation action always leaves an audit trail; reply_to_id is a
    self-FK for simple threaded replies."""

    __tablename__ = "competitive_match_chat"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    text: str = Field()
    reply_to_id: Optional[UUID] = Field(default=None, foreign_key="competitive_match_chat.id")
    deleted_at: Optional[datetime] = Field(default=None)
    deleted_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveChatReport(SQLModel, table=True):
    """Mirrors public.competitive_chat_reports — the moderation reports
    queue (manual/rule-based only in V1, "AI moderation is future")."""

    __tablename__ = "competitive_chat_reports"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    message_id: UUID = Field(foreign_key="competitive_match_chat.id", index=True)
    reporter_id: UUID = Field(foreign_key="profiles.id", index=True)
    reason: Optional[str] = Field(default=None)
    resolved_at: Optional[datetime] = Field(default=None)
    resolved_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveChatMute(SQLModel, table=True):
    """Mirrors public.competitive_chat_mutes — admin-issued spectator-chat
    mute. muted_until=None means permanent. A user may have several rows
    over time (history preserved); the "currently muted" check
    (spectator_service._is_muted) looks for any row still in effect."""

    __tablename__ = "competitive_chat_mutes"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    muted_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    reason: Optional[str] = Field(default=None)
    muted_until: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveBlockedWord(SQLModel, table=True):
    """Mirrors public.competitive_blocked_words — simple case-insensitive
    substring match against chat text (spectator_service._contains_blocked_word)."""

    __tablename__ = "competitive_blocked_words"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    word: str = Field()
    created_by: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveSpectatorStats(SQLModel, table=True):
    """Mirrors public.competitive_spectator_stats — one row per user,
    lazily created (see spectator_service.get_or_create_spectator_stats,
    mirroring statistics_service.get_or_create_statistics's pattern).
    arena_xp/spectator_xp are an intentional stub currency: no redemption
    economy/shop exists yet, same 'recorded_only' philosophy as Phase 7's
    reward_service for grant types with no consumption system."""

    __tablename__ = "competitive_spectator_stats"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    matches_watched: int = Field(default=0)
    predictions_made: int = Field(default=0)
    predictions_correct: int = Field(default=0)
    reactions_sent: int = Field(default=0)
    chat_messages_sent: int = Field(default=0)
    arena_xp: int = Field(default=0)
    spectator_xp: int = Field(default=0)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveMatchBookmark(SQLModel, table=True):
    """Mirrors public.competitive_match_bookmarks — "watch later" / "notify
    me when the replay is ready" marker for a match a spectator follows."""

    __tablename__ = "competitive_match_bookmarks"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Phase 9 — Tournament System (Single Elimination), Part A ──────────────
#
# Tournament matches are structurally 2-player duels underneath — they reuse
# the existing CompetitiveMatch / Arena Match Engine verbatim
# (match_type="tournament", already a reserved MATCH_TYPES literal). No
# duplicate gameplay logic here — see app/services/competitive/
# tournament_service.py for bracket generation and app/routers/competitive/
# tournaments.py + app/routers/admin/competitive.py for the API surface.
#
# Only 'single_elimination' format and 'public'/'private' tournament_type
# have real logic in V1 — the wider CHECK/list values below are reserved so
# future phases need zero migration (rejected with 400 at the API layer).

TOURNAMENT_FORMATS = ["single_elimination", "double_elimination", "swiss", "round_robin", "league", "club_championship"]
TOURNAMENT_FORMATS_SUPPORTED = ["single_elimination"]

TOURNAMENT_TYPES = ["public", "private", "school", "club", "sponsored"]
TOURNAMENT_TYPES_SUPPORTED = ["public", "private"]

TOURNAMENT_SEEDING_METHODS = ["mmr", "random", "manual"]
TOURNAMENT_SEEDING_METHODS_SUPPORTED = ["mmr", "random"]

TOURNAMENT_MAX_PARTICIPANTS_OPTIONS = [8, 16, 32, 64, 128, 256]

TOURNAMENT_STATUSES = [
    "draft", "published", "registration_open", "registration_closed", "bracket_generated",
    "running", "paused", "completed", "cancelled", "archived",
]

TOURNAMENT_PARTICIPANT_STATUSES = [
    "registered", "waiting_list", "active", "eliminated", "disqualified", "withdrawn", "runner_up", "champion",
]

TOURNAMENT_ROUND_STATUSES = ["pending", "in_progress", "completed"]

#: 'ready' = both slots known, awaiting the actual duel to be created by
#: Part B's progression engine (match_id still NULL). 'bye' = pre-resolved
#: at bracket-generation time (winner_id already set, or None for a
#: "double bye" — see tournament_service._propagate_byes). 'pending' = at
#: least one slot still unknown, waiting on an earlier round's real match.
TOURNAMENT_MATCH_STATUSES = ["pending", "ready", "scheduled", "in_progress", "completed", "bye", "disqualified"]

TOURNAMENT_REWARD_PLACEMENTS = ["champion", "runner_up", "semifinalist", "quarterfinalist", "participation"]
TOURNAMENT_REWARD_TYPES = ["ep", "arena_xp", "arena_rating", "badge", "sticker", "title", "frame", "season_points"]


class CompetitiveTournament(SQLModel, table=True):
    """Mirrors public.competitive_tournaments. Status machine (mirrors
    CompetitiveSeason's draft→active→ended style, widened for a tournament's
    extra pre-bracket steps): draft -> published -> registration_open ->
    registration_closed -> bracket_generated -> running -> completed, with
    cancelled reachable from any non-terminal status and paused <-> running."""

    __tablename__ = "competitive_tournaments"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field()
    description: Optional[str] = Field(default=None)
    banner_url: Optional[str] = Field(default=None)
    cover_image_url: Optional[str] = Field(default=None)
    subject_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    school_level_id: Optional[UUID] = Field(default=None, foreign_key="levels.id")
    difficulty: Optional[str] = Field(default=None)
    language: Optional[str] = Field(default=None)
    format: str = Field(default="single_elimination")
    tournament_type: str = Field(default="public")
    max_participants: int = Field()
    seeding_method: str = Field(default="mmr")
    entry_cost_ep: Optional[int] = Field(default=None)  # reserved — V1 tournaments are always free
    registration_opens_at: datetime = Field()
    registration_closes_at: datetime = Field()
    starts_at: datetime = Field()
    status: str = Field(default="draft")
    rules: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    max_spectators_default: Optional[int] = Field(default=None)
    organizer_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveTournamentParticipant(SQLModel, table=True):
    """Mirrors public.competitive_tournament_participants. Waiting list is
    NOT a separate table — reused via status='waiting_list' +
    waiting_list_position, mirroring how Phase 7/8 already merged several
    spec-suggested arena_* tables into the competitive_* family."""

    __tablename__ = "competitive_tournament_participants"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tournament_id: UUID = Field(foreign_key="competitive_tournaments.id", index=True)
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    seed: Optional[int] = Field(default=None)  # NULL until bracket generated
    status: str = Field(default="registered")
    waiting_list_position: Optional[int] = Field(default=None)
    registered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    eliminated_at: Optional[datetime] = Field(default=None)
    final_round_reached: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveTournamentRound(SQLModel, table=True):
    """Mirrors public.competitive_tournament_rounds. round_name is computed
    at bracket-generation time from the remaining round count (e.g.
    "Finale"/"Demi-finales"/"Quarts de finale"...), never admin-entered."""

    __tablename__ = "competitive_tournament_rounds"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tournament_id: UUID = Field(foreign_key="competitive_tournaments.id", index=True)
    round_number: int = Field()  # 1 = earliest round
    round_name: str = Field()
    status: str = Field(default="pending")
    starts_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveTournamentMatch(SQLModel, table=True):
    """Mirrors public.competitive_tournament_matches — the bracket tree.
    player_a_id/player_b_id are nullable (unknown until a previous round
    resolves); match_id is nullable until the actual duel (CompetitiveMatch
    row) is created for that slot (Part B's progression engine); next_
    match_id/next_match_slot is a self-FK pointer into the following
    round's slot the winner advances into (NULL for the final).

    status='bye' rows are pre-resolved at bracket-generation time (see
    tournament_service.generate_bracket / _propagate_byes) — winner_id is
    already set (or None for a "double bye", when neither seed in the pair
    exists). Part B's progression engine should treat status='bye' matches
    as pre-completed and never try to create a real duel for them."""

    __tablename__ = "competitive_tournament_matches"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tournament_id: UUID = Field(foreign_key="competitive_tournaments.id", index=True)
    round_id: UUID = Field(foreign_key="competitive_tournament_rounds.id", index=True)
    bracket_position: int = Field()  # 0-indexed position within the round
    player_a_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    player_b_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_matches.id")
    winner_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")
    status: str = Field(default="pending")
    next_match_id: Optional[UUID] = Field(default=None, foreign_key="competitive_tournament_matches.id")
    next_match_slot: Optional[str] = Field(default=None)  # a|b
    scheduled_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveTournamentReward(SQLModel, table=True):
    """Mirrors public.competitive_tournament_rewards — closely mirrors
    CompetitiveSeasonReward's shape, targeted by final placement instead of
    league/rank range."""

    __tablename__ = "competitive_tournament_rewards"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tournament_id: UUID = Field(foreign_key="competitive_tournaments.id", index=True)
    placement: str = Field()  # champion|runner_up|semifinalist|quarterfinalist|participation
    reward_type: str = Field()  # ep|arena_xp|arena_rating|badge|sticker|title|frame|season_points
    reward_ref: Optional[str] = Field(default=None)
    reward_amount: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveTournamentLog(SQLModel, table=True):
    """Mirrors public.competitive_tournament_logs — tournament-level
    admin/system audit trail. A dedicated table rather than overloading
    CompetitiveMatchEvent, since tournament-level events (created/
    published/bracket generated/registration opened...) aren't tied to one
    match_id — simpler additive choice than modifying a shared table other
    phases depend on."""

    __tablename__ = "competitive_tournament_logs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tournament_id: UUID = Field(foreign_key="competitive_tournaments.id", index=True)
    actor_id: Optional[UUID] = Field(default=None, foreign_key="profiles.id")  # NULL = system-generated
    event_type: str = Field()
    meta: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Phase 10 — Battle Royale (up to 100 players), Part A ──────────────────
#
# Mirrors migration 086_competitive_arena_battle_royale.sql. Reuses
# CompetitiveMatch (match_type='battle_royale', already a reserved MATCH_
# TYPES literal since migration 074) and CompetitiveMatchParticipant
# verbatim as the one-row-per-match / one-row-per-participant primitives —
# CompetitiveBattleRoyaleMatch is only a 1:1 SIDE-TABLE of BR-specific
# config + live state, not a duplicate of CompetitiveMatch itself.
#
# Elimination config contract (elimination_config JSONB, keyed by
# elimination_mode — Part B/C read these exact keys):
#   score_elimination: {"interval_questions": int, "eliminate_pct": int}
#     Every `interval_questions` questions, the bottom `eliminate_pct`% of
#     still-active players by score are eliminated.
#   lives:             {"lives": int}
#     Every player starts with this many lives; a wrong answer costs one
#     life; 0 lives = eliminated. (CompetitiveMatchParticipant.lives holds
#     the LIVE remaining count per participant.)
#   survival:          {"survival_window_ms": Optional[int], "eliminate_on_wrong": bool}
#     eliminate_on_wrong=true is pure sudden-death (any wrong answer
#     eliminates immediately); survival_window_ms, if set, additionally
#     eliminates anyone who answers slower than this many milliseconds
#     even if correct (a hard response-time cutoff), independent of the
#     platform-wide per-question answer window.
#
# Only Part A's own waiting_room <-> countdown transition is exercised here
# (see battle_royale_service.py). running/final_phase/final_duel/completed
# are Part B/C's territory (the live elimination/gameplay engine) — see
# BATTLE_ROYALE_PHASE_TRANSITIONS below, provided now so Part B doesn't need
# to invent its own status-machine dict.

BATTLE_ROYALE_MAX_PLAYERS_OPTIONS = [10, 20, 30, 50, 75, 100]
BATTLE_ROYALE_ELIMINATION_MODES = ["score_elimination", "lives", "survival"]
BATTLE_ROYALE_FINAL_DUEL_METHODS = ["total_score", "fastest", "sudden_death", "combination"]
BATTLE_ROYALE_DISCONNECT_POLICIES = ["reconnect_grace", "auto_eliminate", "ai_placeholder"]
#: 'ai_placeholder' is architecture-ready only — rejected at the API layer
#: in V1 (see schemas/competitive.py's BattleRoyaleCreateRequest validator).
BATTLE_ROYALE_DISCONNECT_POLICIES_SUPPORTED = ["reconnect_grace", "auto_eliminate"]
BATTLE_ROYALE_PHASES = ["waiting_room", "countdown", "running", "final_phase", "final_duel", "completed"]
BATTLE_ROYALE_REWARD_TYPES = ["ep", "arena_xp", "arena_rating", "badge", "sticker", "title", "frame", "season_points"]

#: current_phase status machine — Part A only ever drives waiting_room ->
#: countdown (see battle_royale_service.py). running onward belongs to
#: Part B's live elimination engine; provided here so it has a ready-made,
#: consistent state machine to call rather than inventing its own (mirrors
#: match_service.TRANSITIONS / tournament_service.TRANSITIONS's own style).
BATTLE_ROYALE_PHASE_TRANSITIONS: Dict[str, set] = {
    "waiting_room": {"countdown"},
    "countdown": {"waiting_room", "running"},  # waiting_room = re-validation failed (dropped below min_players)
    "running": {"final_phase", "completed"},
    "final_phase": {"final_duel", "completed"},
    "final_duel": {"completed"},
    "completed": set(),
}


class CompetitiveBattleRoyaleMatch(SQLModel, table=True):
    """Mirrors public.competitive_battle_royale_matches — 1:1 side-table of
    a CompetitiveMatch (match_type='battle_royale'). Holds every BR-specific
    config value + live gameplay sub-state (current_phase/
    remaining_players_count) that doesn't belong on the generic
    CompetitiveMatch row shared by every match type."""

    __tablename__ = "competitive_battle_royale_matches"

    match_id: UUID = Field(foreign_key="competitive_matches.id", primary_key=True)
    min_players: int = Field()
    max_players: int = Field()
    elimination_mode: str = Field()  # score_elimination|lives|survival
    elimination_config: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    final_phase_threshold: int = Field(default=5)
    final_duel_method: str = Field(default="total_score")
    tie_break_rules: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    disconnect_policy: str = Field(default="auto_eliminate")
    disconnect_grace_seconds: int = Field(default=30)
    ranking_impact: bool = Field(default=True)
    auto_start_countdown_seconds: int = Field(default=30)
    current_phase: str = Field(default="waiting_room")
    remaining_players_count: Optional[int] = Field(default=None)
    entry_cost_ep: Optional[int] = Field(default=None)  # reserved — V1 always NULL/free
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveBattleRoyaleReward(SQLModel, table=True):
    """Mirrors public.competitive_battle_royale_rewards — closely mirrors
    CompetitiveTournamentReward's shape, but RANK-RANGE based (rank_min/
    rank_max) instead of a named placement, since a Battle Royale's outcome
    is a full 1..N ranking, not a bracket-shaped champion/runner-up set."""

    __tablename__ = "competitive_battle_royale_rewards"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    rank_min: int = Field()
    rank_max: int = Field()
    reward_type: str = Field()  # ep|arena_xp|arena_rating|badge|sticker|title|frame|season_points
    reward_ref: Optional[str] = Field(default=None)
    reward_amount: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveBattleRoyaleRankingSnapshot(SQLModel, table=True):
    """Mirrors public.competitive_battle_royale_ranking_snapshots —
    leaderboard evolution over time (one row per match/question_position/
    user). Powers the live rank-delta animation (Part B) and the replay's
    "Leaderboard Evolution" (Part C). Written by Part B's live engine —
    this phase only creates the table."""

    __tablename__ = "competitive_battle_royale_ranking_snapshots"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    match_id: UUID = Field(foreign_key="competitive_matches.id", index=True)
    question_position: int = Field()
    user_id: UUID = Field(foreign_key="profiles.id", index=True)
    rank: int = Field()
    score: int = Field()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CompetitiveBattleRoyaleStatistics(SQLModel, table=True):
    """Mirrors public.competitive_battle_royale_statistics — one lazily-
    created row per user, the Battle Royale section of a Player Profile.
    Genuinely distinct from CompetitiveStatistics (Phase 1), which is a
    win/loss/draw/rating shape that doesn't fit an N-player ranked outcome
    (e.g. "47th place" isn't a loss in the duel sense). Populated by Part C
    on match completion — this phase only creates the table."""

    __tablename__ = "competitive_battle_royale_statistics"

    user_id: UUID = Field(primary_key=True, foreign_key="profiles.id")
    matches_played: int = Field(default=0)
    wins: int = Field(default=0)  # rank=1 finishes
    top3_finishes: int = Field(default=0)
    top10_finishes: int = Field(default=0)
    best_rank: Optional[int] = Field(default=None)  # lowest number = best
    longest_survival_position: Optional[int] = Field(default=None)  # furthest question_position reached before elimination, best-ever
    # Running SUM of every match's accuracy_pct (not a pre-averaged value —
    # divide by matches_played for the display average); mirrors the same
    # incremental-average intent as CompetitiveStatistics.accuracy_pct's
    # update formula in gameplay_service.finish_match, just stored as a sum
    # instead of a running average to avoid repeated round-trip rounding.
    total_accuracy_pct_sum: float = Field(default=0)
    favorite_subject_id: Optional[UUID] = Field(default=None, foreign_key="subjects.id")
    favorite_difficulty: Optional[str] = Field(default=None)
    # Phase 10 Part C — migration 087. Incremental occurrence counters
    # ({subject_id: count} / {difficulty: count}) this user's Battle Royale
    # matches have touched — favorite_subject_id/favorite_difficulty above
    # are simply re-derived as "the key with the highest count" after every
    # match (increment-a-counter-take-the-max), the same style
    # CompetitiveStatistics.subject_breakdown/difficulty_breakdown already
    # use for their own (fuller, per-attempt) breakdowns — a smaller,
    # cheaper incremental variant here since only the favorite matters, not
    # a full accuracy/response-time breakdown.
    subject_counts: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    difficulty_counts: Any = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default="'{}'::jsonb"),
    )
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
