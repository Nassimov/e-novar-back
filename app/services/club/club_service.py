from __future__ import annotations

"""
Clash Club Manager — Competitive Arena Phase 11, Part A (Foundation).

Mirrors tournament_service.py / battle_royale_service.py's style deliberately:
a get_or_404 helper, permission-gated mutating functions that raise
HTTPException on failure, and a defensive log_event() that never breaks the
action that triggered it. Unlike a tournament/battle royale (each a single
row with a bounded lifecycle), a Club is a permanent entity with no terminal
"completed" state — its only "closing" transition is a soft delete.

Logging: reuses the EXISTING app.models.admin.AuditLog table (target_type=
'club') rather than introducing a new club_logs table — migration 089's own
table list only calls for clubs/club_members/club_invitations, and AuditLog
is already the generic "who did what to which entity" audit trail this
codebase uses outside the per-match/per-tournament event-log tables (which
exist because THOSE domains needed player-facing history feeds — Part B may
still add a dedicated club activity/feed table of its own; this module's own
audit needs are fully served by AuditLog).

Key interpretive decisions (see this phase's final report for the full
write-up):
  - One club per player: a user may only be an ACTIVE member of one club at
    a time (mirrors Battle Royale's "one active BR per player" precedent —
    not 100% explicit in the spec, but the conservative reading for a
    permanent-team concept).
  - Privacy value -> join behavior:
      'public'               -> join_club() joins immediately.
      'application_required' -> join_club() files a pending application
                                 (kind='application') instead of joining.
      'invite_only'           -> join_club() rejects; only an owner/officer
                                 invite (invite_member + accept) can add a
                                 member.
      'private'                -> join_club() rejects entirely, same as
                                 invite_only from the JOINER's point of view
                                 — the only distinction V1 draws between the
                                 two is informational (private clubs don't
                                 even invite the idea of a public roster
                                 join path), since invite_member() itself is
                                 never privacy-gated (an owner/officer can
                                 always directly invite regardless of the
                                 club's privacy setting).
  - Discovery visibility: search_clubs() returns ALL non-deleted, non-
    suspended clubs regardless of privacy value — privacy governs JOIN
    behavior, not search visibility (a private club can still be found and
    admired, just not self-service-joined).
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, func, select

from app.models.admin import AuditLog, PlatformSettings
from app.models.club import (
    CLUB_MAX_MEMBERS_OPTIONS,
    CLUB_MEMBER_ROLES,
    Club,
    ClubInvitation,
    ClubMember,
)
from app.models.competitive import CompetitiveStatistics
from app.models.kp import KpBalance
from app.models.profile import Profile
from app.services.club import permission_service
from app.services.club.permission_service import get_club_or_404, require_permission
from app.services.competitive import statistics_service
from app.services.kp import spend_kp
from app.services.notification_engine import emit

logger = logging.getLogger(__name__)

#: Simple hardcoded reserved-word list — a full admin-configurable reserved-
#: word table would be over-engineering for V1 (no such precedent exists
#: elsewhere in this codebase; Phase 8's competitive_blocked_words is scoped
#: to in-match CHAT moderation, a different concern). TODO(Part B/admin):
#: promote to an admin-editable list if moderation needs grow.
_RESERVED_WORDS = {
    "admin", "administrator", "administrateur", "enovar", "e-novar", "moderator",
    "modérateur", "support", "official", "officiel", "system", "root", "staff",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log_event(db: Session, *, club_id: UUID, actor_id: Optional[UUID], event_type: str, meta: Optional[Dict[str, Any]] = None) -> None:
    """Never raises — see module docstring for why AuditLog is reused here
    instead of a dedicated club_logs table."""
    try:
        db.add(AuditLog(actor_id=actor_id, action=event_type, target_type="club", target_id=club_id, meta=meta or {}))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("club log_event failed club_id=%s event_type=%s", club_id, event_type)


def _settings(db: Session) -> PlatformSettings:
    return db.get(PlatformSettings, True) or PlatformSettings()


# ─── Name / tag validation ───────────────────────────────────────────────────

def validate_club_name(db: Session, name: str, tag: str, *, exclude_club_id: Optional[UUID] = None) -> None:
    """Raises HTTPException on failure. Uniqueness is checked case-
    insensitively (recommended, and matches the DB's own lower(name)/
    lower(tag) unique indexes — see migration 089) — this pre-check exists
    only to surface a clean 409 with a specific field instead of relying on
    the DB's IntegrityError."""
    settings_row = _settings(db)
    min_len = settings_row.competitive_club_name_min_length or 3
    max_len = settings_row.competitive_club_name_max_length or 40

    name = (name or "").strip()
    tag = (tag or "").strip()

    if not (min_len <= len(name) <= max_len):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Le nom du club doit contenir entre {min_len} et {max_len} caractères.")
    if not (2 <= len(tag) <= 6):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Le tag du club doit contenir entre 2 et 6 caractères.")
    if not re.match(r"^[A-Za-z0-9]+$", tag):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Le tag du club ne peut contenir que des lettres et des chiffres.")

    lname, ltag = name.lower(), tag.lower()
    for word in _RESERVED_WORDS:
        if word in lname or word == ltag:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ce nom ou tag de club n'est pas autorisé.")

    name_query = select(Club).where(func.lower(Club.name) == lname).where(Club.status != "deleted")
    tag_query = select(Club).where(func.lower(Club.tag) == ltag).where(Club.status != "deleted")
    if exclude_club_id is not None:
        name_query = name_query.where(Club.id != exclude_club_id)
        tag_query = tag_query.where(Club.id != exclude_club_id)

    if db.exec(name_query).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce nom de club est déjà utilisé.")
    if db.exec(tag_query).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce tag de club est déjà utilisé.")


# ─── Eligibility ──────────────────────────────────────────────────────────────

def _active_membership_anywhere(db: Session, user_id: UUID) -> Optional[ClubMember]:
    return db.exec(
        select(ClubMember).where(ClubMember.user_id == user_id).where(ClubMember.status == "active")
    ).first()


def check_club_creation_eligibility(db: Session, user_id: UUID) -> None:
    """Raises HTTPException on failure. Authentication itself is already
    guaranteed by the router's Depends(get_current_user)."""
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None and stats.suspended_until is not None and stats.suspended_until > _now():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ton accès à l'Arène compétitive est suspendu.")

    settings_row = _settings(db)

    # "Minimum level" — interpreted as the player's gamification/KP level
    # (KpBalance.level), the only "level" concept that exists on a player's
    # profile — StudentProfile.level_main is a coarse ACADEMIC level
    # (primaire/bem/lycée/univ), not a progression level, and is already
    # used for a different purpose (tournament/BR school-level eligibility).
    if settings_row.competitive_club_min_level is not None:
        kp_account = db.exec(select(KpBalance).where(KpBalance.user_id == user_id)).first()
        player_level = kp_account.level if kp_account is not None else 1
        if player_level < settings_row.competitive_club_min_level:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ton niveau est insuffisant pour créer un club.")

    if settings_row.competitive_club_min_rating is not None:
        rating = stats.rating if stats is not None else settings_row.competitive_mmr_initial_rating
        if rating < settings_row.competitive_club_min_rating:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ta cote Arena est insuffisante pour créer un club.")

    if settings_row.competitive_club_min_account_age_days is not None:
        profile = db.get(Profile, user_id)
        if profile is not None and profile.created_at is not None:
            created_at = profile.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_days = (_now() - created_at).days
            if age_days < settings_row.competitive_club_min_account_age_days:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ton compte est trop récent pour créer un club.")

    if _active_membership_anywhere(db, user_id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tu fais déjà partie d'un club — quitte-le avant d'en créer un nouveau.")


def check_join_eligibility(db: Session, club: Club, user_id: UUID) -> None:
    """Shared by join_club() and respond_to_invitation() (accepting an
    'invite'). Deliberately narrower than club-creation eligibility — no
    level/rating/account-age gates for JOINING (those are creation-specific
    per spec), only suspension + one-club-per-player + not-banned-from-this-
    club."""
    stats = db.get(CompetitiveStatistics, user_id)
    if stats is not None and stats.suspended_until is not None and stats.suspended_until > _now():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ton accès à l'Arène compétitive est suspendu.")

    existing = db.exec(
        select(ClubMember).where(ClubMember.club_id == club.id).where(ClubMember.user_id == user_id)
    ).first()
    if existing is not None:
        if existing.status == "banned":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu as été banni(e) de ce club.")
        if existing.status == "active":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tu es déjà membre de ce club.")

    other_active = db.exec(
        select(ClubMember)
        .where(ClubMember.user_id == user_id)
        .where(ClubMember.status == "active")
        .where(ClubMember.club_id != club.id)
    ).first()
    if other_active is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tu fais déjà partie d'un club — quitte-le avant d'en rejoindre un autre.")

    if club.member_count >= club.max_members:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce club a atteint sa capacité maximale.")


# ─── Creation ────────────────────────────────────────────────────────────────

def create_club(
    db: Session,
    *,
    owner_id: UUID,
    name: str,
    tag: str,
    description: Optional[str] = None,
    logo_url: Optional[str] = None,
    banner_url: Optional[str] = None,
    privacy: str = "public",
    max_members: Optional[int] = None,
    subject_focus_id: Optional[UUID] = None,
    primary_language: Optional[str] = None,
    region: Optional[str] = None,
    skip_eligibility: bool = False,
    skip_cost: bool = False,
    actor_email: Optional[str] = None,
) -> Club:
    """skip_eligibility/skip_cost=True is the admin-created-club path (see
    app/routers/admin/club.py's admin_create_club) — an admin bypasses
    eligibility/cost entirely, per spec."""
    validate_club_name(db, name, tag)

    if not skip_eligibility:
        check_club_creation_eligibility(db, owner_id)

    settings_row = _settings(db)
    resolved_max_members = max_members or settings_row.competitive_club_default_max_members
    if resolved_max_members not in CLUB_MAX_MEMBERS_OPTIONS:
        resolved_max_members = settings_row.competitive_club_default_max_members

    cost = settings_row.competitive_club_creation_cost_ep or 0
    if not skip_cost and cost > 0:
        try:
            spend_kp(owner_id, cost, "Création de club", db)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="EP insuffisant pour créer un club.")

    club = Club(
        name=name.strip(), tag=tag.strip(), description=description, logo_url=logo_url, banner_url=banner_url,
        owner_id=owner_id, privacy=privacy, max_members=resolved_max_members, subject_focus_id=subject_focus_id,
        primary_language=primary_language, region=region, member_count=1,
    )
    db.add(club)
    db.commit()
    db.refresh(club)

    db.add(ClubMember(club_id=club.id, user_id=owner_id, role="owner"))
    db.commit()

    log_event(db, club_id=club.id, actor_id=owner_id, event_type="club_created", meta={"name": club.name, "tag": club.tag, "cost_ep": cost if not skip_cost else 0, "admin_email": actor_email})

    # Phase 14 — Arena Achievement Engine: 'Club Leader' (arena_club_owner).
    try:
        from app.services.competitive import arena_achievement_service
        arena_achievement_service.check_and_unlock_arena_achievements(db, owner_id)
    except Exception:
        pass

    return club


# ─── Read ─────────────────────────────────────────────────────────────────────

def _profile_map(db: Session, user_ids: List[UUID]) -> Dict[UUID, Profile]:
    if not user_ids:
        return {}
    return {p.id: p for p in db.exec(select(Profile).where(Profile.id.in_(user_ids))).all()}


def club_to_out(db: Session, club: Club) -> Dict[str, Any]:
    owner = db.get(Profile, club.owner_id)
    return {
        "id": club.id, "name": club.name, "tag": club.tag, "logo_url": club.logo_url, "banner_url": club.banner_url,
        "description": club.description, "owner_id": club.owner_id, "owner_name": owner.full_name if owner else None,
        "privacy": club.privacy, "max_members": club.max_members, "member_count": club.member_count,
        "subject_focus_id": club.subject_focus_id, "primary_language": club.primary_language, "region": club.region,
        "status": club.status, "suspended_at": club.suspended_at, "suspended_reason": club.suspended_reason,
        "rating": club.rating, "reputation": club.reputation, "wins": club.wins, "losses": club.losses,
        "draws": club.draws, "matches_played": club.matches_played, "current_streak": club.current_streak,
        "best_streak": club.best_streak, "xp": club.xp, "total_ep_earned": club.total_ep_earned,
        "last_activity_at": club.last_activity_at, "created_at": club.created_at, "updated_at": club.updated_at,
    }


def get_club_profile(db: Session, club_id: UUID) -> Dict[str, Any]:
    club = get_club_or_404(db, club_id)
    return club_to_out(db, club)


def list_members(db: Session, club_id: UUID, *, include_banned: bool = False) -> List[Dict[str, Any]]:
    query = select(ClubMember).where(ClubMember.club_id == club_id)
    if not include_banned:
        query = query.where(ClubMember.status == "active")
    rows = list(db.exec(query.order_by(ClubMember.joined_at.asc())).all())
    profiles = _profile_map(db, [r.user_id for r in rows])
    out = []
    for r in rows:
        profile = profiles.get(r.user_id)
        out.append({
            "id": r.id, "club_id": r.club_id, "user_id": r.user_id,
            "full_name": profile.full_name if profile else None, "avatar_url": profile.avatar_url if profile else None,
            "role": r.role, "status": r.status, "joined_at": r.joined_at,
            "banned_at": r.banned_at, "banned_reason": r.banned_reason,
        })
    return out


def get_my_club(db: Session, user_id: UUID) -> Tuple[Optional[Club], Optional[ClubMember]]:
    membership = _active_membership_anywhere(db, user_id)
    if membership is None:
        return None, None
    club = db.get(Club, membership.club_id)
    return club, membership


def list_my_invitations(db: Session, user_id: UUID, *, status_filter: Optional[str] = "pending") -> List[Dict[str, Any]]:
    """The player-facing counterpart of list_invitations() — every pending
    invite/application addressed to ME, across every club, not scoped to
    one club's own roster-management view (that one requires the 'invite'
    permission; this one just requires being the addressee)."""
    query = select(ClubInvitation).where(ClubInvitation.user_id == user_id)
    if status_filter:
        query = query.where(ClubInvitation.status == status_filter)
    rows = list(db.exec(query.order_by(ClubInvitation.created_at.desc())).all())
    club_ids = [r.club_id for r in rows]
    clubs = {c.id: c for c in db.exec(select(Club).where(Club.id.in_(club_ids))).all()} if club_ids else {}
    out = []
    for r in rows:
        club = clubs.get(r.club_id)
        out.append({
            "id": r.id, "club_id": r.club_id, "club_name": club.name if club else None,
            "club_tag": club.tag if club else None, "kind": r.kind, "user_id": r.user_id,
            "user_full_name": None, "inviter_id": r.inviter_id, "status": r.status, "message": r.message,
            "created_at": r.created_at, "responded_at": r.responded_at, "expires_at": r.expires_at,
        })
    return out


def list_invitations(db: Session, club_id: UUID, *, status_filter: Optional[str] = "pending") -> List[Dict[str, Any]]:
    query = select(ClubInvitation).where(ClubInvitation.club_id == club_id)
    if status_filter:
        query = query.where(ClubInvitation.status == status_filter)
    rows = list(db.exec(query.order_by(ClubInvitation.created_at.desc())).all())
    profiles = _profile_map(db, [r.user_id for r in rows])
    out = []
    for r in rows:
        profile = profiles.get(r.user_id)
        out.append({
            "id": r.id, "club_id": r.club_id, "kind": r.kind, "user_id": r.user_id,
            "user_full_name": profile.full_name if profile else None, "inviter_id": r.inviter_id,
            "status": r.status, "message": r.message, "created_at": r.created_at,
            "responded_at": r.responded_at, "expires_at": r.expires_at,
        })
    return out


_SORTS = {"newest", "most_active", "highest_ranked", "most_victories"}


def search_clubs(
    db: Session, *, q: Optional[str] = None, subject_id: Optional[UUID] = None, language: Optional[str] = None,
    region: Optional[str] = None, min_members: Optional[int] = None, sort: str = "newest", page: int = 1, size: int = 20,
) -> Tuple[List[Dict[str, Any]], int]:
    """Public discovery — only status='active' clubs. Every privacy value is
    included in results (privacy governs JOIN behavior, not visibility —
    see module docstring). `q` is a case-insensitive substring match against
    name OR tag — needed for both browsing AND for finding a specific club
    to challenge (see app/services/club/battle_service.create_challenge's
    caller, ChallengeClubModal on the frontend)."""
    if sort not in _SORTS:
        sort = "newest"
    query = select(Club).where(Club.status == "active")
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.where(func.lower(Club.name).like(like) | func.lower(Club.tag).like(like))
    if subject_id is not None:
        query = query.where(Club.subject_focus_id == subject_id)
    if language:
        query = query.where(Club.primary_language == language)
    if region:
        query = query.where(Club.region == region)
    if min_members is not None:
        query = query.where(Club.member_count >= min_members)

    total = db.exec(select(func.count()).select_from(query.subquery())).one()

    if sort == "highest_ranked":
        query = query.order_by(Club.rating.desc())
    elif sort == "most_victories":
        query = query.order_by(Club.wins.desc())
    elif sort == "most_active":
        query = query.order_by(Club.last_activity_at.desc())
    else:
        query = query.order_by(Club.created_at.desc())

    query = query.offset((page - 1) * size).limit(size)
    clubs = list(db.exec(query).all())
    return [club_to_out(db, c) for c in clubs], total


# ─── Update / delete ──────────────────────────────────────────────────────────

_CLUB_UPDATABLE_FIELDS = {
    "description", "logo_url", "banner_url", "privacy", "max_members",
    "subject_focus_id", "primary_language", "region",
}


def update_club(db: Session, club: Club, *, actor_email: Optional[str] = None, **fields) -> Club:
    """name/tag changes go through validate_club_name again (excluding this
    club from the uniqueness check); every other field is a plain
    passthrough update."""
    if club.status == "deleted":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce club a été supprimé.")

    updates = {k: v for k, v in fields.items() if v is not None}
    new_name = updates.pop("name", None)
    new_tag = updates.pop("tag", None)
    if new_name is not None or new_tag is not None:
        validate_club_name(db, new_name or club.name, new_tag or club.tag, exclude_club_id=club.id)
        if new_name is not None:
            club.name = new_name.strip()
        if new_tag is not None:
            club.tag = new_tag.strip()

    for key, value in updates.items():
        if key in _CLUB_UPDATABLE_FIELDS:
            setattr(club, key, value)

    club.updated_at = _now()
    db.add(club)
    db.commit()
    db.refresh(club)
    log_event(db, club_id=club.id, actor_id=None, event_type="club_updated", meta={"fields": list(fields.keys()), "admin_email": actor_email})
    return club


def delete_club(db: Session, club: Club, *, actor_id: Optional[UUID] = None, actor_email: Optional[str] = None) -> Club:
    """Soft-delete (status='deleted') — never hard-deleted, audit trail
    preserved (mirrors tournament/battle royale's cancel, not a DELETE)."""
    if club.status == "deleted":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce club est déjà supprimé.")
    club.status = "deleted"
    club.updated_at = _now()
    db.add(club)
    db.commit()
    db.refresh(club)
    log_event(db, club_id=club.id, actor_id=actor_id, event_type="club_deleted", meta={"admin_email": actor_email})
    return club


def admin_suspend_club(db: Session, club: Club, *, reason: Optional[str] = None, actor_email: Optional[str] = None) -> Club:
    if club.status == "deleted":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce club est supprimé.")
    club.status = "suspended"
    club.suspended_at = _now()
    club.suspended_reason = reason
    club.updated_at = _now()
    db.add(club)
    db.commit()
    db.refresh(club)
    log_event(db, club_id=club.id, actor_id=None, event_type="club_suspended", meta={"reason": reason, "admin_email": actor_email})
    return club


def admin_restore_club(db: Session, club: Club, *, actor_email: Optional[str] = None) -> Club:
    if club.status != "suspended":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce club n'est pas suspendu.")
    club.status = "active"
    club.suspended_at = None
    club.suspended_reason = None
    club.updated_at = _now()
    db.add(club)
    db.commit()
    db.refresh(club)
    log_event(db, club_id=club.id, actor_id=None, event_type="club_restored", meta={"admin_email": actor_email})
    return club


# ─── Membership: join / leave ────────────────────────────────────────────────

def join_club(db: Session, club: Club, user_id: UUID) -> Dict[str, Any]:
    """Returns {'joined': bool, 'membership': ClubMember | None,
    'invitation': ClubInvitation | None} — 'joined' distinguishes an
    immediate join (public club) from a filed application
    (application_required club, no membership created yet)."""
    if club.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce club n'accepte pas de nouveaux membres.")

    if club.privacy == "public":
        check_join_eligibility(db, club, user_id)
        membership = _create_membership(db, club, user_id, role="member")
        log_event(db, club_id=club.id, actor_id=user_id, event_type="club_joined", meta={"user_id": str(user_id)})
        _notify_member_joined(db, club, user_id)
        return {"joined": True, "membership": membership, "invitation": None}

    if club.privacy == "application_required":
        check_join_eligibility(db, club, user_id)
        already = db.exec(
            select(ClubInvitation)
            .where(ClubInvitation.club_id == club.id).where(ClubInvitation.user_id == user_id)
            .where(ClubInvitation.status == "pending")
        ).first()
        if already is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tu as déjà une candidature en attente pour ce club.")
        invitation = ClubInvitation(club_id=club.id, kind="application", user_id=user_id, inviter_id=None, status="pending")
        db.add(invitation)
        db.commit()
        db.refresh(invitation)
        log_event(db, club_id=club.id, actor_id=user_id, event_type="club_application_submitted", meta={"user_id": str(user_id)})
        return {"joined": False, "membership": None, "invitation": invitation}

    # 'invite_only' and 'private': no self-service join path (see module
    # docstring for the exact interpretation of each).
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ce club n'est accessible que sur invitation.")


def _create_membership(db: Session, club: Club, user_id: UUID, *, role: str = "member") -> ClubMember:
    existing = db.exec(
        select(ClubMember).where(ClubMember.club_id == club.id).where(ClubMember.user_id == user_id)
    ).first()
    if existing is not None:
        existing.status = "active"
        existing.role = role
        existing.joined_at = _now()
        existing.banned_at = None
        existing.banned_reason = None
        existing.banned_by = None
        db.add(existing)
        membership = existing
    else:
        membership = ClubMember(club_id=club.id, user_id=user_id, role=role)
        db.add(membership)

    club.member_count = (club.member_count or 0) + 1
    club.last_activity_at = _now()
    db.add(club)
    db.commit()
    db.refresh(membership)
    return membership


def _notify_member_joined(db: Session, club: Club, new_member_id: UUID) -> None:
    officers = db.exec(
        select(ClubMember).where(ClubMember.club_id == club.id).where(ClubMember.status == "active")
        .where(ClubMember.role.in_(("owner", "officer")))
    ).all()
    for m in officers:
        emit(
            db, event_type="club_member_joined", user_id=m.user_id, context={"club_name": club.name},
            data={"club_id": str(club.id), "new_member_id": str(new_member_id)},
            dedup_key=f"club_member_joined:{club.id}:{new_member_id}:{m.user_id}",
        )


def leave_club(db: Session, club: Club, user_id: UUID) -> None:
    membership = db.exec(
        select(ClubMember).where(ClubMember.club_id == club.id).where(ClubMember.user_id == user_id).where(ClubMember.status == "active")
    ).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tu n'es pas membre de ce club.")
    if membership.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Le propriétaire ne peut pas quitter le club — transfère la propriété ou supprime le club.",
        )

    db.delete(membership)
    club.member_count = max(0, (club.member_count or 1) - 1)
    club.last_activity_at = _now()
    db.add(club)
    db.commit()
    log_event(db, club_id=club.id, actor_id=user_id, event_type="club_left", meta={"user_id": str(user_id)})


# ─── Invitations ──────────────────────────────────────────────────────────────

def invite_member(db: Session, club: Club, inviter_id: UUID, invitee_user_id: UUID, *, message: Optional[str] = None) -> ClubInvitation:
    require_permission(db, club.id, inviter_id, "invite")

    if club.member_count >= club.max_members:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce club a atteint sa capacité maximale.")

    existing_member = db.exec(
        select(ClubMember).where(ClubMember.club_id == club.id).where(ClubMember.user_id == invitee_user_id)
    ).first()
    if existing_member is not None:
        if existing_member.status == "active":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce joueur est déjà membre du club.")
        if existing_member.status == "banned":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce joueur a été banni de ce club.")

    other_active = db.exec(
        select(ClubMember).where(ClubMember.user_id == invitee_user_id).where(ClubMember.status == "active")
    ).first()
    if other_active is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce joueur fait déjà partie d'un autre club.")

    already_pending = db.exec(
        select(ClubInvitation).where(ClubInvitation.club_id == club.id).where(ClubInvitation.user_id == invitee_user_id)
        .where(ClubInvitation.status == "pending")
    ).first()
    if already_pending is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Une invitation ou candidature est déjà en attente pour ce joueur.")

    invitation = ClubInvitation(club_id=club.id, kind="invite", user_id=invitee_user_id, inviter_id=inviter_id, status="pending", message=message)
    db.add(invitation)
    db.commit()
    db.refresh(invitation)

    log_event(db, club_id=club.id, actor_id=inviter_id, event_type="club_invitation_sent", meta={"invitee_id": str(invitee_user_id)})
    emit(
        db, event_type="club_invitation_received", user_id=invitee_user_id, context={"club_name": club.name},
        data={"club_id": str(club.id), "invitation_id": str(invitation.id)},
        dedup_key=f"club_invitation_received:{invitation.id}",
    )
    return invitation


def get_invitation_or_404(db: Session, invitation_id: UUID) -> ClubInvitation:
    invitation = db.get(ClubInvitation, invitation_id)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation introuvable.")
    return invitation


def respond_to_invitation(db: Session, invitation: ClubInvitation, user_id: UUID, accept: bool) -> Dict[str, Any]:
    """Handles BOTH directions in one function (kept together rather than
    split — the two branches share almost all of the accept-side plumbing;
    only the "who may respond" and permission check differ)."""
    if invitation.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette invitation/candidature n'est plus en attente.")

    club = get_club_or_404(db, invitation.club_id)

    if invitation.kind == "invite":
        if invitation.user_id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Seul le joueur invité peut répondre à cette invitation.")
        target_user_id = invitation.user_id
    else:  # 'application'
        require_permission(db, club.id, user_id, "approve_requests")
        target_user_id = invitation.user_id

    membership: Optional[ClubMember] = None
    if accept:
        check_join_eligibility(db, club, target_user_id)
        membership = _create_membership(db, club, target_user_id, role="member")
        invitation.status = "accepted"
        _notify_member_joined(db, club, target_user_id)
    else:
        invitation.status = "declined"

    invitation.responded_at = _now()
    db.add(invitation)
    db.commit()
    db.refresh(invitation)

    log_event(
        db, club_id=club.id, actor_id=user_id, event_type="club_invitation_responded",
        meta={"invitation_id": str(invitation.id), "kind": invitation.kind, "accepted": accept},
    )
    return {"invitation": invitation, "membership": membership}


# ─── Kick / ban ───────────────────────────────────────────────────────────────

def _get_membership_or_404(db: Session, club_id: UUID, user_id: UUID) -> ClubMember:
    membership = db.exec(
        select(ClubMember).where(ClubMember.club_id == club_id).where(ClubMember.user_id == user_id)
    ).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ce joueur n'est pas membre de ce club.")
    return membership


def kick_member(db: Session, club: Club, actor_id: UUID, target_user_id: UUID, *, reason: Optional[str] = None) -> None:
    actor = require_permission(db, club.id, actor_id, "kick")
    target = _get_membership_or_404(db, club.id, target_user_id)
    if target.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce joueur n'est pas un membre actif de ce club.")
    permission_service.require_higher_rank(actor, target)

    db.delete(target)
    club.member_count = max(0, (club.member_count or 1) - 1)
    club.last_activity_at = _now()
    db.add(club)
    db.commit()

    log_event(db, club_id=club.id, actor_id=actor_id, event_type="club_member_kicked", meta={"target_user_id": str(target_user_id), "reason": reason})
    emit(
        db, event_type="club_kicked", user_id=target_user_id, context={"club_name": club.name},
        data={"club_id": str(club.id), "reason": reason}, dedup_key=f"club_kicked:{club.id}:{target_user_id}:{_now().timestamp()}",
    )


def ban_member(db: Session, club: Club, actor_id: UUID, target_user_id: UUID, *, reason: Optional[str] = None) -> None:
    actor = require_permission(db, club.id, actor_id, "ban")
    target = _get_membership_or_404(db, club.id, target_user_id)
    if target.status == "banned":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce joueur est déjà banni de ce club.")
    permission_service.require_higher_rank(actor, target)

    was_active = target.status == "active"
    target.status = "banned"
    target.role = "member"
    target.banned_at = _now()
    target.banned_reason = reason
    target.banned_by = actor_id
    db.add(target)
    if was_active:
        club.member_count = max(0, (club.member_count or 1) - 1)
    club.last_activity_at = _now()
    db.add(club)
    db.commit()

    log_event(db, club_id=club.id, actor_id=actor_id, event_type="club_member_banned", meta={"target_user_id": str(target_user_id), "reason": reason})
    emit(
        db, event_type="club_kicked", user_id=target_user_id, context={"club_name": club.name},
        data={"club_id": str(club.id), "reason": reason, "banned": True}, dedup_key=f"club_banned:{club.id}:{target_user_id}:{_now().timestamp()}",
    )


# ─── Promote / demote ──────────────────────────────────────────────────────────

def promote_member(db: Session, club: Club, actor_id: UUID, target_user_id: UUID, new_role: str) -> ClubMember:
    if new_role not in CLUB_MEMBER_ROLES or new_role == "owner":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Rôle cible invalide (utilise transfer-ownership pour transférer la propriété).")

    actor = require_permission(db, club.id, actor_id, "promote")
    target = _get_membership_or_404(db, club.id, target_user_id)
    if target.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce joueur n'est pas un membre actif de ce club.")
    if target.role == "owner":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Impossible de modifier le rôle du propriétaire.")
    if permission_service.role_rank(new_role) <= permission_service.role_rank(target.role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Le nouveau rôle doit être supérieur au rôle actuel pour une promotion.")

    # Officer-level moves (promoting TO officer) are owner-only, per spec's
    # "Owner: ...manage officers" — an officer may promote member<->moderator
    # but not appoint a fellow officer.
    if new_role == "officer" and actor.role != "owner":
        require_permission(db, club.id, actor_id, "manage_officers")

    target.role = new_role
    db.add(target)
    club.last_activity_at = _now()
    db.add(club)
    db.commit()
    db.refresh(target)

    log_event(db, club_id=club.id, actor_id=actor_id, event_type="club_member_promoted", meta={"target_user_id": str(target_user_id), "new_role": new_role})
    emit(
        db, event_type="club_promoted", user_id=target_user_id, context={"club_name": club.name, "role": new_role},
        data={"club_id": str(club.id), "new_role": new_role}, dedup_key=f"club_promoted:{club.id}:{target_user_id}:{new_role}:{_now().timestamp()}",
    )
    return target


def demote_member(db: Session, club: Club, actor_id: UUID, target_user_id: UUID, new_role: str) -> ClubMember:
    if new_role not in CLUB_MEMBER_ROLES or new_role == "owner":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Rôle cible invalide.")

    actor = require_permission(db, club.id, actor_id, "demote")
    target = _get_membership_or_404(db, club.id, target_user_id)
    if target.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ce joueur n'est pas un membre actif de ce club.")
    if target.role == "owner":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Impossible de modifier le rôle du propriétaire.")
    if permission_service.role_rank(new_role) >= permission_service.role_rank(target.role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Le nouveau rôle doit être inférieur au rôle actuel pour une rétrogradation.")

    # Demoting FROM officer is owner-only, same "manage officers" carve-out
    # as promote_member.
    if target.role == "officer" and actor.role != "owner":
        require_permission(db, club.id, actor_id, "manage_officers")

    target.role = new_role
    db.add(target)
    club.last_activity_at = _now()
    db.add(club)
    db.commit()
    db.refresh(target)

    log_event(db, club_id=club.id, actor_id=actor_id, event_type="club_member_demoted", meta={"target_user_id": str(target_user_id), "new_role": new_role})
    emit(
        db, event_type="club_demoted", user_id=target_user_id, context={"club_name": club.name, "role": new_role},
        data={"club_id": str(club.id), "new_role": new_role}, dedup_key=f"club_demoted:{club.id}:{target_user_id}:{new_role}:{_now().timestamp()}",
    )
    return target


# ─── Ownership transfer ─────────────────────────────────────────────────────

def transfer_ownership(db: Session, club: Club, current_owner_id: UUID, new_owner_user_id: UUID, *, actor_email: Optional[str] = None) -> Club:
    if club.owner_id != current_owner_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Seul le propriétaire actuel peut transférer la propriété du club.")
    if new_owner_user_id == current_owner_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tu es déjà propriétaire de ce club.")

    old_owner = _get_membership_or_404(db, club.id, current_owner_id)
    new_owner = _get_membership_or_404(db, club.id, new_owner_user_id)
    if new_owner.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Le nouveau propriétaire doit être un membre actif du club.")

    # Sequential updates within the same transaction — old owner demoted to
    # 'officer' FIRST, then the new owner promoted to 'owner' — so the
    # partial UNIQUE index on (club_id) WHERE role='owner' is never
    # violated (see migration 089).
    old_owner.role = "officer"
    db.add(old_owner)
    db.flush()

    new_owner.role = "owner"
    db.add(new_owner)
    db.flush()

    club.owner_id = new_owner_user_id
    club.updated_at = _now()
    db.add(club)
    db.commit()
    db.refresh(club)

    log_event(
        db, club_id=club.id, actor_id=current_owner_id, event_type="club_ownership_transferred",
        meta={"old_owner_id": str(current_owner_id), "new_owner_id": str(new_owner_user_id), "admin_email": actor_email},
    )
    emit(
        db, event_type="club_promoted", user_id=new_owner_user_id, context={"club_name": club.name, "role": "owner"},
        data={"club_id": str(club.id), "new_role": "owner"}, dedup_key=f"club_ownership_transferred:{club.id}:{new_owner_user_id}",
    )

    # Phase 14 — Arena Achievement Engine: the new owner may now qualify for 'Club Leader'.
    try:
        from app.services.competitive import arena_achievement_service
        arena_achievement_service.check_and_unlock_arena_achievements(db, new_owner_user_id)
    except Exception:
        pass

    return club


def admin_transfer_ownership(db: Session, club: Club, new_owner_user_id: UUID, *, actor_email: Optional[str] = None) -> Club:
    """Admin override — no current-owner consent needed. Reuses
    transfer_ownership's exact sequencing by calling it with
    current_owner_id=club.owner_id (i.e. impersonating the current owner for
    the purposes of the transfer itself, which is exactly what an admin
    override means here)."""
    return transfer_ownership(db, club, club.owner_id, new_owner_user_id, actor_email=actor_email)


# ─── Admin role override ────────────────────────────────────────────────────

def admin_set_role(db: Session, club: Club, target_user_id: UUID, new_role: str, *, actor_email: Optional[str] = None) -> ClubMember:
    """Admin override — bypasses the promote/demote rank checks entirely
    (an admin may set ANY role directly, including appointing a new
    officer or demoting one), but still respects the one-owner invariant:
    setting role='owner' here is rejected — use admin_transfer_ownership
    instead, which correctly demotes the previous owner atomically."""
    if new_role not in CLUB_MEMBER_ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Rôle invalide.")
    if new_role == "owner":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Utilise le transfert de propriété pour désigner un nouveau propriétaire.")

    target = _get_membership_or_404(db, club.id, target_user_id)
    if target.role == "owner":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Utilise le transfert de propriété pour modifier le rôle du propriétaire.")

    target.role = new_role
    db.add(target)
    db.commit()
    db.refresh(target)

    log_event(db, club_id=club.id, actor_id=None, event_type="club_member_role_admin_set", meta={"target_user_id": str(target_user_id), "new_role": new_role, "admin_email": actor_email})
    return target
