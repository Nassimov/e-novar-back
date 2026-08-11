from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.competitive import INVITATION_STATUSES, MATCH_TYPES


class MatchCreateRequest(BaseModel):
    """Creates a match draft. For a duel, pass opponent_code to immediately
    send an invitation (see app/routers/competitive/matches.py) — the match
    itself is always created first regardless of match_type."""

    match_type: str
    subject_ids: List[UUID] = Field(default_factory=list)
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    question_count: int = 10
    visibility: str = "private"
    scheduled_at: Optional[datetime] = None
    opponent_code: Optional[str] = None  # student_code of the invitee (duel only)
    is_ranked: bool = True  # Phase 13 — player may opt into a casual (no Arena Rating change) match

    @field_validator("match_type")
    @classmethod
    def _valid_match_type(cls, v: str) -> str:
        if v not in MATCH_TYPES:
            raise ValueError(f"match_type must be one of {MATCH_TYPES}")
        return v

    @field_validator("visibility")
    @classmethod
    def _valid_visibility(cls, v: str) -> str:
        if v not in ("private", "public"):
            raise ValueError("visibility must be 'private' or 'public'")
        return v

    @field_validator("question_count")
    @classmethod
    def _valid_question_count(cls, v: int) -> int:
        if not (1 <= v <= 50):
            raise ValueError("question_count must be between 1 and 50")
        return v


class ParticipantResponse(BaseModel):
    id: UUID
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    role: str
    is_ready: bool
    result: Optional[str] = None
    score: int

    model_config = {"from_attributes": True}


class MatchResponse(BaseModel):
    id: UUID
    match_type: str
    status: str
    creator_id: UUID
    subject_ids: List[UUID]
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    question_count: int
    visibility: str
    max_players: int
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    created_at: datetime
    participants: List[ParticipantResponse] = Field(default_factory=list)
    is_ranked: bool = True  # Phase 13

    model_config = {"from_attributes": True}


class InvitationCreateRequest(BaseModel):
    match_id: UUID
    opponent_code: str


class InvitationResponse(BaseModel):
    id: UUID
    match_id: UUID
    inviter_id: UUID
    invitee_id: UUID
    inviter_name: Optional[str] = None
    invitee_name: Optional[str] = None
    status: str
    expires_at: datetime
    responded_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in INVITATION_STATUSES:
            raise ValueError(f"status must be one of {INVITATION_STATUSES}")
        return v


class ChoiceOut(BaseModel):
    """Never includes is_correct until the question is revealed — the
    backend is the only source of truth, the frontend must never be able
    to infer the answer from this payload."""

    id: UUID
    label: str
    text: str


class CurrentQuestionOut(BaseModel):
    match_question_id: UUID
    position: int
    statement: str
    is_multi_choice: bool
    choices: List[ChoiceOut]


class RevealOut(BaseModel):
    match_question_id: UUID
    correct_choice_ids: List[UUID]
    my_selected_choice_ids: List[UUID]
    opponent_selected_choice_ids: List[UUID]
    my_is_correct: bool
    my_points_earned: int
    my_response_time_ms: Optional[int] = None
    explanation: str = ""


class ScoreboardEntryOut(BaseModel):
    user_id: UUID
    full_name: Optional[str] = None
    score: int
    correct_count: int
    wrong_count: int
    current_streak: int
    accuracy_pct: float = 0.0
    # Phase 11, Part C — populated only for match_type='club_battle' (see
    # CompetitiveMatchParticipant.club_id/team_side, migration 091); None
    # for every other match type. Lets the frontend group this same generic
    # scoreboard into two teams without a parallel compute function.
    team_side: Optional[str] = None
    club_id: Optional[UUID] = None


class FinalResultOut(BaseModel):
    winner_id: Optional[UUID] = None
    is_draw: bool
    duration_sec: Optional[int] = None
    stats: List[ScoreboardEntryOut]


class MatchStateResponse(BaseModel):
    """Single computed state payload — the only thing the frontend ever
    renders during gameplay. Pushed over /ws/competitive/{match_id} on every
    phase change AND returned by GET .../state for reconnect/refresh, so
    both paths always agree on shape."""

    match_id: UUID
    status: str
    phase: str  # countdown|reading|answering|locked|reveal|transition|finished
    phase_ends_at: Optional[datetime] = None
    server_time: datetime
    question_count: int
    current_position: Optional[int] = None
    current_question: Optional[CurrentQuestionOut] = None
    my_selected_choice_ids: Optional[List[UUID]] = None
    opponent_has_answered: Optional[bool] = None
    reveal: Optional[RevealOut] = None
    scoreboard: List[ScoreboardEntryOut] = Field(default_factory=list)
    final_result: Optional[FinalResultOut] = None


class SubmitAnswerRequest(BaseModel):
    match_question_id: UUID
    selected_choice_ids: List[UUID] = Field(default_factory=list)


class LeagueOut(BaseModel):
    id: UUID
    league_name: str
    division_label: Optional[str] = None
    sort_order: int
    min_mmr: int
    max_mmr: Optional[int] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    promotion_bonus_ep: int
    demotion_protection_matches: int
    visible: bool

    model_config = {"from_attributes": True}


class StatisticsResponse(BaseModel):
    user_id: UUID
    matches_played: int
    wins: int
    losses: int
    draws: int
    accuracy_pct: float
    avg_response_time_ms: Optional[int] = None
    current_streak: int
    longest_streak: int
    favorite_subject_id: Optional[UUID] = None
    favorite_difficulty: Optional[str] = None
    perfect_matches: int
    rank_tier: str
    best_rank_tier: str
    rating: int
    tournament_wins: int
    club_wins: int
    battle_royale_wins: int
    updated_at: datetime
    # Phase 7 — Ranking System
    highest_rating: int = 1000
    league: Optional[LeagueOut] = None
    global_rank: Optional[int] = None
    season_rank: Optional[int] = None
    active_season_id: Optional[UUID] = None

    model_config = {"from_attributes": True}


# ─── Phase 4 — Replay Engine & AI Match Analysis ────────────────────────────

class ReplayResponse(BaseModel):
    match_id: UUID
    status: str  # pending|processing|ready|failed
    score_timeline: List[Dict[str, Any]] = Field(default_factory=list)
    duration_sec: Optional[int] = None
    generated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AiAnalysisResponse(BaseModel):
    match_id: UUID
    status: str  # pending|processing|ready|failed
    overall_summary: Optional[str] = None
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    behavior_notes: Optional[str] = None
    mastery_level: Optional[str] = None
    confidence: Optional[str] = None
    progress_comparison: Dict[str, Any] = Field(default_factory=dict)
    generated_at: Optional[datetime] = None
    grade: Optional[str] = None  # Phase 12 — A+|A|B+|B|C|D|F

    model_config = {"from_attributes": True}


class RecommendationResponse(BaseModel):
    id: UUID
    category: str
    title: str
    description: str
    confidence: str
    action_type: Optional[str] = None
    action_payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    model_config = {"from_attributes": True}


class RoadmapStepResponse(BaseModel):
    id: UUID
    position: int
    title: str
    description: Optional[str] = None
    status: str

    model_config = {"from_attributes": True}


# ─── Phase 6 — Matchmaking System & Queue Engine ────────────────────────────

class JoinQueueRequest(BaseModel):
    mode: str = "ranked"  # ranked|friendly
    subject_id: UUID
    school_level_id: UUID
    difficulty: str
    language: Optional[str] = None


class MatchQualityBreakdown(BaseModel):
    rating_score: float
    language_score: float
    win_rate_score: float
    response_time_score: float
    question_pool_ok: bool
    overall_score: float


class QueueStatusResponse(BaseModel):
    queue_entry_id: UUID
    status: str
    mode: str
    subject_id: UUID
    school_level_id: UUID
    difficulty: str
    language: Optional[str] = None
    elapsed_seconds: int
    current_radius: int
    estimated_wait_seconds: int
    proposal_group_id: Optional[UUID] = None
    proposal_expires_at: Optional[datetime] = None
    matched_with_user_id: Optional[UUID] = None
    match_id: Optional[UUID] = None
    match_quality: Optional[MatchQualityBreakdown] = None

    model_config = {"from_attributes": True}


class QueueAdminEntrySummary(BaseModel):
    queue_entry_id: UUID
    user_id: UUID
    mode: str
    subject_id: UUID
    school_level_id: UUID
    difficulty: str
    status: str
    elapsed_seconds: int
    current_radius: int

    model_config = {"from_attributes": True}


class QueueMonitoringResponse(BaseModel):
    players_waiting: int
    avg_wait_seconds: float
    queue_length_by_mode: Dict[str, int] = Field(default_factory=dict)
    active_proposals: int
    matches_created_last_hour: int
    cancelled_searches_last_hour: int
    expired_searches_last_hour: int
    entries: List[QueueAdminEntrySummary] = Field(default_factory=list)


# ─── Phase 7 — Ranking System, Seasons & Competitive Leagues ────────────────

class SeasonOut(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    status: str
    starts_at: datetime
    ends_at: datetime
    reset_strategy: str
    reset_percentage: Optional[int] = None
    rules: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    model_config = {"from_attributes": True}


class SeasonCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    starts_at: datetime
    ends_at: datetime
    reset_strategy: str = "soft"
    reset_percentage: Optional[int] = None
    rules: Dict[str, Any] = Field(default_factory=dict)
    activate_now: bool = False

    @field_validator("reset_strategy")
    @classmethod
    def _valid_strategy(cls, v: str) -> str:
        if v not in ("soft", "hard", "percentage"):
            raise ValueError("reset_strategy must be one of soft|hard|percentage")
        return v


class SeasonUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    reset_strategy: Optional[str] = None
    reset_percentage: Optional[int] = None
    rules: Optional[Dict[str, Any]] = None


class LeagueUpsertRequest(BaseModel):
    league_name: str
    division_label: Optional[str] = None
    sort_order: int
    min_mmr: int
    max_mmr: Optional[int] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    promotion_bonus_ep: int = 0
    demotion_protection_matches: int = 3
    visible: bool = True


class SeasonRewardOut(BaseModel):
    id: UUID
    season_id: UUID
    league_id: Optional[UUID] = None
    rank_min: Optional[int] = None
    rank_max: Optional[int] = None
    reward_type: str
    reward_ref: Optional[str] = None
    reward_amount: Optional[int] = None

    model_config = {"from_attributes": True}


class SeasonRewardCreateRequest(BaseModel):
    league_id: Optional[UUID] = None
    rank_min: Optional[int] = None
    rank_max: Optional[int] = None
    reward_type: str
    reward_ref: Optional[str] = None
    reward_amount: Optional[int] = None

    @field_validator("reward_type")
    @classmethod
    def _valid_reward_type(cls, v: str) -> str:
        if v not in ("ep", "badge", "sticker", "frame", "title", "avatar_decoration"):
            raise ValueError("invalid reward_type")
        return v


class RewardGrantOut(BaseModel):
    id: UUID
    season_id: Optional[UUID] = None
    source: str
    reward_type: str
    reward_ref: Optional[str] = None
    reward_amount: Optional[int] = None
    status: str
    granted_at: datetime

    model_config = {"from_attributes": True}


class LeaderboardEntryOut(BaseModel):
    rank: int
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    mmr: int
    matches_played: Optional[int] = None
    wins: Optional[int] = None
    accuracy_pct: Optional[float] = None
    net_gain: Optional[int] = None
    # Phase 13 — "Ranking Filters" sort dimensions, populated only by
    # _query_all_time (the only board with a lifetime-stats join for every row).
    longest_streak: Optional[int] = None
    battle_royale_wins: Optional[int] = None
    tournament_wins: Optional[int] = None
    club_wins: Optional[int] = None
    # Phase 14 — equipped title, batch-attached to every leaderboard row.
    equipped_title: Optional[str] = None
    title_rarity: Optional[str] = None


class LeaderboardResponse(BaseModel):
    period: str
    season_id: Optional[UUID] = None
    items: List[LeaderboardEntryOut] = Field(default_factory=list)
    total: int
    page: int
    size: int


class RatingHistoryEntryOut(BaseModel):
    id: UUID
    match_id: Optional[UUID] = None  # Phase 13 — None for inactivity-decay rows
    opponent_id: Optional[UUID] = None
    opponent_name: Optional[str] = None
    result: str
    rating_before: int
    rating_after: int
    delta: int
    league_before: Optional[str] = None
    league_after: Optional[str] = None
    created_at: datetime


class SeasonStatsOut(BaseModel):
    season_id: UUID
    season_name: str
    mmr: int
    league: Optional[LeagueOut] = None
    matches_played: int
    wins: int
    losses: int
    draws: int
    accuracy_pct: float
    avg_response_time_ms: Optional[int] = None
    final_rank: Optional[int] = None
    season_rank: Optional[int] = None


class ProfileRankingResponse(BaseModel):
    user_id: UUID
    rating: int
    highest_rating: int
    league: Optional[LeagueOut] = None
    best_rank_tier: str
    global_rank: Optional[int] = None
    win_rate: float
    current_streak: int
    longest_streak: int
    current_season: Optional[SeasonStatsOut] = None
    # Phase 13
    placement_complete: bool = True
    placement_matches_played: int = 0
    placement_matches_required: int = 0
    fair_play_score: int = 100
    season_best_streak: int = 0
    is_ranked_eligible: bool = True


class SuspiciousAccountOut(BaseModel):
    user_id: UUID
    full_name: Optional[str] = None
    signal_types: List[str]
    signal_count: int
    last_signal_at: datetime


# ─── Phase 8 — Spectator Mode, Live Reactions & Predictions ─────────────────

from app.models.competitive import REACTION_EMOJI_SET  # noqa: E402


class SpectatorRevealOut(BaseModel):
    """Unlike the player's RevealOut (which only ever shows `my_selected_
    choice_ids`), spectators see BOTH participants' picks + the correct
    answer once the reveal phase starts — by then it's safe to show anyone
    (players themselves already see it too)."""

    match_question_id: UUID
    correct_choice_ids: List[UUID]
    player_a_id: UUID
    player_a_selected_choice_ids: List[UUID]
    player_a_is_correct: bool
    player_b_id: Optional[UUID] = None
    player_b_selected_choice_ids: List[UUID] = Field(default_factory=list)
    player_b_is_correct: bool = False
    explanation: str = ""


class SpectatorStateResponse(BaseModel):
    """The single computed state payload pushed over
    /ws/competitive/{match_id}/spectate and returned by any spectator REST
    reconnect path — viewer-agnostic (every spectator sees the identical
    payload, unlike MatchStateResponse which is per-viewer for players).
    Never includes my_selected_choice_ids/opponent_has_answered — spectators
    must never learn who has (or hasn't) answered, or what was selected,
    before phase == 'reveal' (see gameplay_service.compute_spectator_state)."""

    match_id: UUID
    status: str
    phase: str
    phase_ends_at: Optional[datetime] = None
    server_time: datetime
    question_count: int
    current_position: Optional[int] = None
    current_question: Optional[CurrentQuestionOut] = None
    reveal: Optional[SpectatorRevealOut] = None
    scoreboard: List[ScoreboardEntryOut] = Field(default_factory=list)
    final_result: Optional[FinalResultOut] = None
    spectator_count: int = 0


class LiveMatchParticipantOut(BaseModel):
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    league: Optional[LeagueOut] = None
    score: int = 0


class LiveMatchOut(BaseModel):
    match_id: UUID
    match_type: str
    subject_ids: List[UUID] = Field(default_factory=list)
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    question_count: int
    current_position: Optional[int] = None
    started_at: Optional[datetime] = None
    duration_sec: Optional[int] = None
    spectator_count: int = 0
    players: List[LiveMatchParticipantOut] = Field(default_factory=list)
    score_delta: Optional[int] = None


class LiveMatchListResponse(BaseModel):
    items: List[LiveMatchOut] = Field(default_factory=list)
    total: int
    page: int
    size: int


class PredictionCreateRequest(BaseModel):
    predicted_winner_id: Optional[UUID] = None  # None = draw prediction


class PredictionOut(BaseModel):
    id: UUID
    match_id: UUID
    user_id: UUID
    predicted_winner_id: Optional[UUID] = None
    is_correct: Optional[bool] = None
    xp_awarded: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class PredictionSummaryResponse(BaseModel):
    match_id: UUID
    predictions_open: bool
    total_predictions: int
    player_a_id: Optional[UUID] = None
    player_a_votes: int = 0
    player_b_id: Optional[UUID] = None
    player_b_votes: int = 0
    draw_votes: int = 0
    my_prediction: Optional[PredictionOut] = None


class ReactionCreateRequest(BaseModel):
    emoji: str

    @field_validator("emoji")
    @classmethod
    def _valid_emoji(cls, v: str) -> str:
        if v not in REACTION_EMOJI_SET:
            raise ValueError(f"emoji must be one of {REACTION_EMOJI_SET}")
        return v


class ChatMessageCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    reply_to_id: Optional[UUID] = None


class ChatReportCreateRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class ChatMessageOut(BaseModel):
    id: UUID
    match_id: UUID
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    text: str
    reply_to_id: Optional[UUID] = None
    deleted_at: Optional[datetime] = None
    created_at: datetime


class ChatHistoryResponse(BaseModel):
    items: List[ChatMessageOut] = Field(default_factory=list)
    total: int
    page: int
    size: int


class SpectatorStatsResponse(BaseModel):
    user_id: UUID
    matches_watched: int = 0
    predictions_made: int = 0
    predictions_correct: int = 0
    reactions_sent: int = 0
    chat_messages_sent: int = 0
    arena_xp: int = 0
    spectator_xp: int = 0
    updated_at: datetime

    model_config = {"from_attributes": True}


class ReplayAvailabilityResponse(BaseModel):
    match_id: UUID
    status: str  # pending|processing|ready|failed
    ready: bool


class MuteUserRequest(BaseModel):
    user_id: UUID
    duration_minutes: Optional[int] = None  # None = permanent
    reason: Optional[str] = None


class UnmuteUserRequest(BaseModel):
    user_id: UUID


class ChatMuteOut(BaseModel):
    id: UUID
    user_id: UUID
    full_name: Optional[str] = None
    muted_by: Optional[UUID] = None
    reason: Optional[str] = None
    muted_until: Optional[datetime] = None
    created_at: datetime


class BlockedWordCreateRequest(BaseModel):
    word: str = Field(min_length=1, max_length=100)


class BlockedWordOut(BaseModel):
    id: UUID
    word: str
    created_by: Optional[UUID] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatReportOut(BaseModel):
    id: UUID
    message_id: UUID
    message_text: Optional[str] = None
    reporter_id: UUID
    reporter_name: Optional[str] = None
    reason: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[UUID] = None
    created_at: datetime


class ChatReportResolveRequest(BaseModel):
    action: str  # delete_message|dismiss

    @field_validator("action")
    @classmethod
    def _valid_action(cls, v: str) -> str:
        if v not in ("delete_message", "dismiss"):
            raise ValueError("action must be one of delete_message|dismiss")
        return v


class SpectatorAnalyticsResponse(BaseModel):
    live_matches: int
    current_total_spectators: int
    peak_spectators_estimate: int
    avg_spectators_per_match: float


# ─── Phase 9 — Tournament System (Single Elimination), Part A ──────────────

from app.models.competitive import (  # noqa: E402
    TOURNAMENT_FORMATS,
    TOURNAMENT_FORMATS_SUPPORTED,
    TOURNAMENT_PARTICIPANT_STATUSES,
    TOURNAMENT_REWARD_PLACEMENTS,
    TOURNAMENT_REWARD_TYPES,
    TOURNAMENT_SEEDING_METHODS,
    TOURNAMENT_SEEDING_METHODS_SUPPORTED,
    TOURNAMENT_TYPES,
    TOURNAMENT_TYPES_SUPPORTED,
)


class TournamentCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    banner_url: Optional[str] = None
    cover_image_url: Optional[str] = None
    subject_id: Optional[UUID] = None
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    language: Optional[str] = None
    format: str = "single_elimination"
    tournament_type: str = "public"
    max_participants: int
    seeding_method: str = "mmr"
    registration_opens_at: datetime
    registration_closes_at: datetime
    starts_at: datetime
    rules: Dict[str, Any] = Field(default_factory=dict)
    max_spectators_default: Optional[int] = None

    @field_validator("format")
    @classmethod
    def _valid_format(cls, v: str) -> str:
        if v not in TOURNAMENT_FORMATS:
            raise ValueError(f"format must be one of {TOURNAMENT_FORMATS}")
        if v not in TOURNAMENT_FORMATS_SUPPORTED:
            raise ValueError(f"format '{v}' is not yet supported — only {TOURNAMENT_FORMATS_SUPPORTED} in V1")
        return v

    @field_validator("tournament_type")
    @classmethod
    def _valid_tournament_type(cls, v: str) -> str:
        if v not in TOURNAMENT_TYPES:
            raise ValueError(f"tournament_type must be one of {TOURNAMENT_TYPES}")
        if v not in TOURNAMENT_TYPES_SUPPORTED:
            raise ValueError(f"tournament_type '{v}' is not yet supported — only {TOURNAMENT_TYPES_SUPPORTED} in V1")
        return v

    @field_validator("seeding_method")
    @classmethod
    def _valid_seeding_method(cls, v: str) -> str:
        if v not in TOURNAMENT_SEEDING_METHODS:
            raise ValueError(f"seeding_method must be one of {TOURNAMENT_SEEDING_METHODS}")
        if v not in TOURNAMENT_SEEDING_METHODS_SUPPORTED:
            raise ValueError(f"seeding_method '{v}' is not yet supported — only {TOURNAMENT_SEEDING_METHODS_SUPPORTED} in V1")
        return v

    @field_validator("max_participants")
    @classmethod
    def _valid_max_participants(cls, v: int) -> int:
        if v not in (8, 16, 32, 64, 128, 256):
            raise ValueError("max_participants must be one of 8, 16, 32, 64, 128, 256")
        return v


class TournamentUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    banner_url: Optional[str] = None
    cover_image_url: Optional[str] = None
    subject_id: Optional[UUID] = None
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    language: Optional[str] = None
    max_participants: Optional[int] = None
    seeding_method: Optional[str] = None
    registration_opens_at: Optional[datetime] = None
    registration_closes_at: Optional[datetime] = None
    starts_at: Optional[datetime] = None
    rules: Optional[Dict[str, Any]] = None
    max_spectators_default: Optional[int] = None

    @field_validator("seeding_method")
    @classmethod
    def _valid_seeding_method(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in TOURNAMENT_SEEDING_METHODS_SUPPORTED:
            raise ValueError(f"seeding_method '{v}' is not yet supported — only {TOURNAMENT_SEEDING_METHODS_SUPPORTED} in V1")
        return v

    @field_validator("max_participants")
    @classmethod
    def _valid_max_participants(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in (8, 16, 32, 64, 128, 256):
            raise ValueError("max_participants must be one of 8, 16, 32, 64, 128, 256")
        return v


class TournamentOut(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    banner_url: Optional[str] = None
    cover_image_url: Optional[str] = None
    subject_id: Optional[UUID] = None
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    language: Optional[str] = None
    format: str
    tournament_type: str
    max_participants: int
    seeding_method: str
    entry_cost_ep: Optional[int] = None
    registration_opens_at: datetime
    registration_closes_at: datetime
    starts_at: datetime
    status: str
    rules: Dict[str, Any] = Field(default_factory=dict)
    max_spectators_default: Optional[int] = None
    organizer_id: Optional[UUID] = None
    organizer_name: Optional[str] = None
    participant_count: int = 0
    waiting_list_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TournamentListResponse(BaseModel):
    items: List[TournamentOut] = Field(default_factory=list)
    total: int
    page: int
    size: int


class TournamentParticipantOut(BaseModel):
    id: UUID
    tournament_id: UUID
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    seed: Optional[int] = None
    status: str
    waiting_list_position: Optional[int] = None
    registered_at: datetime
    eliminated_at: Optional[datetime] = None
    final_round_reached: Optional[int] = None

    model_config = {"from_attributes": True}

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in TOURNAMENT_PARTICIPANT_STATUSES:
            raise ValueError(f"status must be one of {TOURNAMENT_PARTICIPANT_STATUSES}")
        return v


class TournamentRewardCreateRequest(BaseModel):
    placement: str
    reward_type: str
    reward_ref: Optional[str] = None
    reward_amount: Optional[int] = None

    @field_validator("placement")
    @classmethod
    def _valid_placement(cls, v: str) -> str:
        if v not in TOURNAMENT_REWARD_PLACEMENTS:
            raise ValueError(f"placement must be one of {TOURNAMENT_REWARD_PLACEMENTS}")
        return v

    @field_validator("reward_type")
    @classmethod
    def _valid_reward_type(cls, v: str) -> str:
        if v not in TOURNAMENT_REWARD_TYPES:
            raise ValueError(f"reward_type must be one of {TOURNAMENT_REWARD_TYPES}")
        return v


class TournamentRewardOut(BaseModel):
    id: UUID
    tournament_id: UUID
    placement: str
    reward_type: str
    reward_ref: Optional[str] = None
    reward_amount: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TournamentMatchPlayerOut(BaseModel):
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    seed: Optional[int] = None
    league: Optional[LeagueOut] = None


class TournamentMatchOut(BaseModel):
    id: UUID
    round_id: UUID
    bracket_position: int
    player_a: Optional[TournamentMatchPlayerOut] = None
    player_b: Optional[TournamentMatchPlayerOut] = None
    match_id: Optional[UUID] = None
    winner_id: Optional[UUID] = None
    status: str
    next_match_id: Optional[UUID] = None
    next_match_slot: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    score_a: Optional[int] = None
    score_b: Optional[int] = None
    spectator_count: int = 0


class TournamentRoundOut(BaseModel):
    id: UUID
    round_number: int
    round_name: str
    status: str
    starts_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    matches: List[TournamentMatchOut] = Field(default_factory=list)


class TournamentBracketResponse(BaseModel):
    tournament_id: UUID
    format: str
    max_participants: int
    status: str
    rounds: List[TournamentRoundOut] = Field(default_factory=list)


# ─── Phase 10 — Battle Royale, Part A ───────────────────────────────────────

from app.models.competitive import (  # noqa: E402
    BATTLE_ROYALE_DISCONNECT_POLICIES,
    BATTLE_ROYALE_DISCONNECT_POLICIES_SUPPORTED,
    BATTLE_ROYALE_ELIMINATION_MODES,
    BATTLE_ROYALE_FINAL_DUEL_METHODS,
    BATTLE_ROYALE_MAX_PLAYERS_OPTIONS,
    BATTLE_ROYALE_PHASES,
    BATTLE_ROYALE_REWARD_TYPES,
)


class BattleRoyaleCreateRequest(BaseModel):
    creator_id: Optional[UUID] = None  # None = admin-created, no single "creator" player
    subject_ids: List[UUID] = Field(default_factory=list)
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    question_count: int = 10
    min_players: int
    max_players: int
    elimination_mode: str
    elimination_config: Dict[str, Any] = Field(default_factory=dict)
    final_phase_threshold: int = 5
    final_duel_method: str = "total_score"
    tie_break_rules: Dict[str, Any] = Field(default_factory=dict)
    disconnect_policy: str = "auto_eliminate"
    disconnect_grace_seconds: int = 30
    ranking_impact: bool = True
    auto_start_countdown_seconds: int = 30
    visibility: str = "public"
    max_spectators: Optional[int] = None
    entry_cost_ep: Optional[int] = None  # reserved — no payment logic in V1

    @field_validator("max_players")
    @classmethod
    def _valid_max_players(cls, v: int) -> int:
        if v not in BATTLE_ROYALE_MAX_PLAYERS_OPTIONS:
            raise ValueError(f"max_players must be one of {BATTLE_ROYALE_MAX_PLAYERS_OPTIONS}")
        return v

    @field_validator("min_players")
    @classmethod
    def _valid_min_players(cls, v: int) -> int:
        if v < 2:
            raise ValueError("min_players must be at least 2")
        return v

    @field_validator("elimination_mode")
    @classmethod
    def _valid_elimination_mode(cls, v: str) -> str:
        if v not in BATTLE_ROYALE_ELIMINATION_MODES:
            raise ValueError(f"elimination_mode must be one of {BATTLE_ROYALE_ELIMINATION_MODES}")
        return v

    @field_validator("final_duel_method")
    @classmethod
    def _valid_final_duel_method(cls, v: str) -> str:
        if v not in BATTLE_ROYALE_FINAL_DUEL_METHODS:
            raise ValueError(f"final_duel_method must be one of {BATTLE_ROYALE_FINAL_DUEL_METHODS}")
        return v

    @field_validator("disconnect_policy")
    @classmethod
    def _valid_disconnect_policy(cls, v: str) -> str:
        if v not in BATTLE_ROYALE_DISCONNECT_POLICIES:
            raise ValueError(f"disconnect_policy must be one of {BATTLE_ROYALE_DISCONNECT_POLICIES}")
        if v not in BATTLE_ROYALE_DISCONNECT_POLICIES_SUPPORTED:
            raise ValueError(f"disconnect_policy '{v}' is not yet supported — only {BATTLE_ROYALE_DISCONNECT_POLICIES_SUPPORTED} in V1")
        return v


class BattleRoyaleUpdateRequest(BaseModel):
    """Only allowed while current_phase='waiting_room' (see
    battle_royale_service.update_battle_royale)."""

    subject_ids: Optional[List[UUID]] = None
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    question_count: Optional[int] = None
    min_players: Optional[int] = None
    max_players: Optional[int] = None
    elimination_mode: Optional[str] = None
    elimination_config: Optional[Dict[str, Any]] = None
    final_phase_threshold: Optional[int] = None
    final_duel_method: Optional[str] = None
    tie_break_rules: Optional[Dict[str, Any]] = None
    disconnect_policy: Optional[str] = None
    disconnect_grace_seconds: Optional[int] = None
    ranking_impact: Optional[bool] = None
    auto_start_countdown_seconds: Optional[int] = None
    visibility: Optional[str] = None
    max_spectators: Optional[int] = None
    entry_cost_ep: Optional[int] = None

    @field_validator("max_players")
    @classmethod
    def _valid_max_players(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in BATTLE_ROYALE_MAX_PLAYERS_OPTIONS:
            raise ValueError(f"max_players must be one of {BATTLE_ROYALE_MAX_PLAYERS_OPTIONS}")
        return v

    @field_validator("elimination_mode")
    @classmethod
    def _valid_elimination_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in BATTLE_ROYALE_ELIMINATION_MODES:
            raise ValueError(f"elimination_mode must be one of {BATTLE_ROYALE_ELIMINATION_MODES}")
        return v

    @field_validator("disconnect_policy")
    @classmethod
    def _valid_disconnect_policy(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in BATTLE_ROYALE_DISCONNECT_POLICIES_SUPPORTED:
            raise ValueError(f"disconnect_policy '{v}' is not yet supported — only {BATTLE_ROYALE_DISCONNECT_POLICIES_SUPPORTED} in V1")
        return v


class BattleRoyaleOut(BaseModel):
    match_id: UUID
    creator_id: Optional[UUID] = None
    match_status: str
    subject_ids: List[UUID] = Field(default_factory=list)
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    question_count: int
    visibility: str
    min_players: int
    max_players: int
    elimination_mode: str
    elimination_config: Dict[str, Any] = Field(default_factory=dict)
    final_phase_threshold: int
    final_duel_method: str
    tie_break_rules: Dict[str, Any] = Field(default_factory=dict)
    disconnect_policy: str
    disconnect_grace_seconds: int
    ranking_impact: bool
    auto_start_countdown_seconds: int
    current_phase: str
    remaining_players_count: Optional[int] = None
    joined_count: int = 0
    entry_cost_ep: Optional[int] = None
    max_spectators: Optional[int] = None
    countdown_deadline: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("current_phase")
    @classmethod
    def _valid_current_phase(cls, v: str) -> str:
        if v not in BATTLE_ROYALE_PHASES:
            raise ValueError(f"current_phase must be one of {BATTLE_ROYALE_PHASES}")
        return v


class BattleRoyaleListResponse(BaseModel):
    items: List[BattleRoyaleOut] = Field(default_factory=list)
    total: int
    page: int
    size: int


class BattleRoyaleParticipantOut(BaseModel):
    id: UUID
    match_id: UUID
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    is_ready: bool = False
    score: int = 0
    lives: Optional[int] = None
    elimination_round: Optional[int] = None
    final_rank: Optional[int] = None
    eliminated_at: Optional[datetime] = None
    joined_at: datetime
    left_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class BattleRoyaleWaitingRoomResponse(BaseModel):
    match_id: UUID
    match_status: str
    current_phase: str
    subject_ids: List[UUID] = Field(default_factory=list)
    school_level_id: Optional[UUID] = None
    difficulty: Optional[str] = None
    min_players: int
    max_players: int
    joined_count: int
    countdown_deadline: Optional[datetime] = None
    estimated_start_at: Optional[datetime] = None
    players: List[BattleRoyaleParticipantOut] = Field(default_factory=list)


class BattleRoyaleRewardCreateRequest(BaseModel):
    rank_min: int
    rank_max: int
    reward_type: str
    reward_ref: Optional[str] = None
    reward_amount: Optional[int] = None

    @field_validator("reward_type")
    @classmethod
    def _valid_reward_type(cls, v: str) -> str:
        if v not in BATTLE_ROYALE_REWARD_TYPES:
            raise ValueError(f"reward_type must be one of {BATTLE_ROYALE_REWARD_TYPES}")
        return v

    @field_validator("rank_max")
    @classmethod
    def _valid_rank_range(cls, v: int, info) -> int:
        rank_min = info.data.get("rank_min")
        if rank_min is not None and v < rank_min:
            raise ValueError("rank_max must be >= rank_min")
        return v


class BattleRoyaleRewardOut(BaseModel):
    id: UUID
    match_id: UUID
    rank_min: int
    rank_max: int
    reward_type: str
    reward_ref: Optional[str] = None
    reward_amount: Optional[int] = None
    created_at: datetime


# ─── Phase 10 — Battle Royale, Part B (live gameplay) ───────────────────────

class BattleRoyaleLeaderboardEntryOut(BaseModel):
    rank: int
    user_id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    score: int
    is_eliminated: bool = False


class BattleRoyaleRevealOut(BaseModel):
    """BR-specific reveal — showing every other player's individual pick the
    way the 2-player RevealOut does isn't meaningful (or reasonably payload-
    sized) at up to 100 players, so this only ever shows the correct answer
    + the viewer's OWN result — the same own-visibility principle as
    MatchStateResponse.my_selected_choice_ids elsewhere in this file."""

    match_question_id: UUID
    correct_choice_ids: List[UUID]
    my_selected_choice_ids: List[UUID] = Field(default_factory=list)
    my_is_correct: bool = False
    my_points_earned: int = 0
    my_response_time_ms: Optional[int] = None
    explanation: str = ""


class BattleRoyaleStateResponse(BaseModel):
    """Single computed state payload for a live Battle Royale — the BR
    counterpart of MatchStateResponse, pushed over the SAME /ws/competitive/
    {match_id} connection (see app/main.py's _push_personal_gameplay_state,
    branched on match.match_type) and computed by gameplay_service.
    compute_battle_royale_state. `phase` mirrors MatchStateResponse.phase
    (reading/answering/reveal/countdown/finished/<match.status> fallback) —
    an addition beyond Part B's literal field list, kept for frontend parity
    with the existing duel UI's question-level phase handling."""

    match_id: UUID
    status: str
    current_phase: str
    phase: str = ""
    phase_ends_at: Optional[datetime] = None
    server_time: datetime
    question_count: int
    current_position: Optional[int] = None
    current_question: Optional[CurrentQuestionOut] = None
    my_status: str = "active"  # active|eliminated|winner
    my_selected_choice_ids: Optional[List[UUID]] = None
    my_rank: Optional[int] = None
    my_score: int = 0
    remaining_players_count: Optional[int] = None
    leaderboard: List[BattleRoyaleLeaderboardEntryOut] = Field(default_factory=list)
    reveal: Optional[BattleRoyaleRevealOut] = None
    final_result: Optional[FinalResultOut] = None

    model_config = {"from_attributes": True}
