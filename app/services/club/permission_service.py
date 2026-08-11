from __future__ import annotations

"""
Clash Club Permission Engine — Competitive Arena Phase 11, Part A.

A single, central, hardcoded permission matrix for the 4 usable V1 roles
(owner/officer/moderator/member — see app/models/club.py's CLUB_MEMBER_
ROLES). Deliberately NOT a configurable-permissions DB table — over-
engineering for a fixed 4-role system in V1 (per spec). Every mutating club
endpoint (app/routers/club/clubs.py, app/routers/admin/club.py's owner-
gated bypass paths) funnels through require_permission() so there is
exactly ONE place that decides "can this role do this" — extending the
matrix later (e.g. adding a Coach/Mentor role — see migration 089's header)
is a small, local change here, not a rewrite.

Role hierarchy (for rank-based comparisons, e.g. "can't kick/ban someone at
or above your own rank"): member(0) < moderator(1) < officer(2) < owner(3).
"""

from typing import Dict, Optional, Set
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.club import Club, ClubMember

ROLE_RANK: Dict[str, int] = {"member": 0, "moderator": 1, "officer": 2, "owner": 3}

#: action -> set of roles allowed to perform it. Spelled out fully per the
#: spec's own example lists (Task 2) — Part B/C should extend this dict
#: rather than inventing a parallel permission mechanism.
PERMISSION_MATRIX: Dict[str, Set[str]] = {
    # Owner-only.
    "delete_club": {"owner"},
    "transfer_ownership": {"owner"},
    "manage_officers": {"owner"},          # appoint/remove an officer specifically
    # Officer+.
    "edit_club": {"owner", "officer"},
    "invite": {"owner", "officer"},
    "approve_requests": {"owner", "officer"},   # accept/decline an application
    "schedule_battles": {"owner", "officer"},   # Part C hook point
    "promote": {"owner", "officer"},            # member<->moderator moves; officer-level moves require manage_officers too (see club_service)
    "demote": {"owner", "officer"},             # same caveat as promote
    "post_announcement": {"owner", "officer"},  # Part B
    "select_battle_roster": {"owner", "officer"},  # Part C — Team Selection
    "manage_achievements": {"owner"},           # Part B — trophy room curation only V1 lets an owner do (grants themselves stay admin-triggered)
    # Moderator+.
    "kick": {"owner", "officer", "moderator"},
    "ban": {"owner", "officer", "moderator"},
    "moderate_chat": {"owner", "officer", "moderator"},   # Part B — pin/unpin/delete
    "remove_messages": {"owner", "officer", "moderator"}, # Part B
    # Everyone (member+).
    "participate": {"owner", "officer", "moderator", "member"},
    "chat": {"owner", "officer", "moderator", "member"},    # Part B
    "accept_invitations": {"owner", "officer", "moderator", "member"},
    "view_analytics": {"owner", "officer", "moderator", "member"},  # Part B
}


def has_permission(role: str, action: str) -> bool:
    return role in PERMISSION_MATRIX.get(action, set())


def role_rank(role: str) -> int:
    return ROLE_RANK.get(role, -1)


def get_active_membership(db: Session, club_id: UUID, user_id: UUID) -> Optional[ClubMember]:
    return db.exec(
        select(ClubMember)
        .where(ClubMember.club_id == club_id)
        .where(ClubMember.user_id == user_id)
        .where(ClubMember.status == "active")
    ).first()


def require_permission(db: Session, club_id: UUID, user_id: UUID, action: str) -> ClubMember:
    """Raises HTTPException(403) (or 404 if the user isn't even a club
    member) on failure; returns the caller's ClubMember row on success so
    the caller doesn't need a second query."""
    membership = get_active_membership(db, club_id, user_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu n'es pas membre de ce club.")
    if not has_permission(membership.role, action):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tu n'as pas la permission d'effectuer cette action.")
    return membership


def require_higher_rank(actor: ClubMember, target: ClubMember) -> None:
    """Guards kick/ban/demote-style actions: the actor's role must outrank
    the target's role — a moderator (rank 1) may not kick/ban an officer
    (rank 2) even though 'kick'/'ban' are in the moderator's permission set;
    only the owner may act on an officer. The owner (rank 3) always
    outranks everyone else and is itself never a valid target for these
    actions (ownership transfer is a separate, dedicated flow)."""
    if target.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Impossible d'agir sur le propriétaire du club — transfère la propriété du club d'abord.",
        )
    if role_rank(actor.role) <= role_rank(target.role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ton rôle ne permet pas d'agir sur un membre de rang égal ou supérieur.")


def get_club_or_404(db: Session, club_id: UUID) -> Club:
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Club introuvable.")
    return club
