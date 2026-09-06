from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlmodel import Session

from app.core.cache import cached_json
from app.dependencies import get_db

router = APIRouter(tags=["catalogs"])

# Reference data (subjects/levels/goals/wilayas) barely ever changes but is
# hit on nearly every page (registration, booking, search filters) — cache
# it so most requests never touch Postgres. Freshness on admin edits comes
# from the explicit cache_invalidate("catalog:subjects"/"catalog:levels")
# calls in app/routers/admin/content.py, not this TTL — so it's safe to set
# long (this is a "cache très long" category-A static dataset per the perf
# audit), it's only a ceiling in case an invalidation call is ever missed.
_CATALOG_TTL = 3600

# ── Fallback hardcoded data (used only if DB tables are empty) ────────────────

_FB_SUBJECTS = [
    "Mathématiques", "Physique", "Chimie", "Sciences naturelles",
    "Français", "Arabe", "Anglais", "Tamazight",
    "Histoire-Géographie", "Philosophie", "Économie",
    "Informatique", "Technologie", "Éducation islamique",
    "Éducation civique", "Sport", "Musique", "Arts plastiques",
]

_FB_GROUPS: dict[str, list[str]] = {
    "primaire":   ["1ère AP", "2ème AP", "3ème AP", "4ème AP", "5ème AP"],
    "moyen":      ["1ère AM", "2ème AM", "3ème AM", "4ème AM"],
    "secondaire": ["1ère AS", "2ème AS", "3ème AS"],
    "superieur":  ["Licence 1", "Licence 2", "Licence 3", "Master 1", "Master 2", "Doctorat"],
    "autre":      ["Adulte", "Professionnel"],
}

_FB_GOALS = [
    {"id": "bac",              "label": "Réussir mon BAC"},
    {"id": "bem",              "label": "Réussir mon BEM"},
    {"id": "improve_grades",   "label": "Améliorer mes notes"},
    {"id": "regular_support",  "label": "Soutien scolaire régulier"},
    {"id": "fill_gaps",        "label": "Combler des lacunes"},
    {"id": "deepen",           "label": "Approfondir une matière"},
    {"id": "competition",      "label": "Préparer un concours"},
    {"id": "certification",    "label": "Certification professionnelle"},
]

_FB_WILAYAS = [
    {"code": "01", "name": "Adrar"},
    {"code": "02", "name": "Chlef"},
    {"code": "03", "name": "Laghouat"},
    {"code": "04", "name": "Oum El Bouaghi"},
    {"code": "05", "name": "Batna"},
    {"code": "06", "name": "Béjaïa"},
    {"code": "07", "name": "Biskra"},
    {"code": "08", "name": "Béchar"},
    {"code": "09", "name": "Blida"},
    {"code": "10", "name": "Bouira"},
    {"code": "11", "name": "Tamanrasset"},
    {"code": "12", "name": "Tébessa"},
    {"code": "13", "name": "Tlemcen"},
    {"code": "14", "name": "Tiaret"},
    {"code": "15", "name": "Tizi Ouzou"},
    {"code": "16", "name": "Alger"},
    {"code": "17", "name": "Djelfa"},
    {"code": "18", "name": "Jijel"},
    {"code": "19", "name": "Sétif"},
    {"code": "20", "name": "Saïda"},
    {"code": "21", "name": "Skikda"},
    {"code": "22", "name": "Sidi Bel Abbès"},
    {"code": "23", "name": "Annaba"},
    {"code": "24", "name": "Guelma"},
    {"code": "25", "name": "Constantine"},
    {"code": "26", "name": "Médéa"},
    {"code": "27", "name": "Mostaganem"},
    {"code": "28", "name": "M'Sila"},
    {"code": "29", "name": "Mascara"},
    {"code": "30", "name": "Ouargla"},
    {"code": "31", "name": "Oran"},
    {"code": "32", "name": "El Bayadh"},
    {"code": "33", "name": "Illizi"},
    {"code": "34", "name": "Bordj Bou Arréridj"},
    {"code": "35", "name": "Boumerdès"},
    {"code": "36", "name": "El Tarf"},
    {"code": "37", "name": "Tindouf"},
    {"code": "38", "name": "Tissemsilt"},
    {"code": "39", "name": "El Oued"},
    {"code": "40", "name": "Khenchela"},
    {"code": "41", "name": "Souk Ahras"},
    {"code": "42", "name": "Tipaza"},
    {"code": "43", "name": "Mila"},
    {"code": "44", "name": "Aïn Defla"},
    {"code": "45", "name": "Naâma"},
    {"code": "46", "name": "Aïn Témouchent"},
    {"code": "47", "name": "Ghardaïa"},
    {"code": "48", "name": "Relizane"},
    {"code": "49", "name": "Timimoun"},
    {"code": "50", "name": "Bordj Badji Mokhtar"},
    {"code": "51", "name": "Ouled Djellal"},
    {"code": "52", "name": "Béni Abbès"},
    {"code": "53", "name": "In Salah"},
    {"code": "54", "name": "In Guezzam"},
    {"code": "55", "name": "Touggourt"},
    {"code": "56", "name": "Djanet"},
    {"code": "57", "name": "El M'Ghair"},
    {"code": "58", "name": "El Meniaa"},
]

STORE_REWARDS = [
    {"id": "discount_10",    "name": "Réduction 10%",       "description": "10% de réduction sur la prochaine session",   "kp_cost": 500,  "type": "discount",  "icon": "🏷️"},
    {"id": "discount_20",    "name": "Réduction 20%",       "description": "20% de réduction sur la prochaine session",   "kp_cost": 900,  "type": "discount",  "icon": "🎫"},
    {"id": "free_session",   "name": "Session gratuite",    "description": "Une session de 60 min offerte",               "kp_cost": 2000, "type": "session",   "icon": "🎓"},
    {"id": "priority_match", "name": "Matching prioritaire","description": "En tête des résultats pendant 7 jours",       "kp_cost": 400,  "type": "perk",      "icon": "⭐"},
    {"id": "avatar_frame",   "name": "Cadre avatar premium","description": "Cadre exclusif pour votre profil",            "kp_cost": 200,  "type": "cosmetic",  "icon": "🖼️"},
    {"id": "analytics_report","name": "Rapport d'analyse",  "description": "Rapport détaillé de vos progrès",            "kp_cost": 300,  "type": "report",    "icon": "📊"},
]


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _load_subjects(db: Session):
    try:
        rows = db.execute(
            text("SELECT name FROM public.subjects ORDER BY position, name")
        ).fetchall()
        subjects = [r[0] for r in rows] if rows else _FB_SUBJECTS
    except Exception:
        subjects = _FB_SUBJECTS
    return {"subjects": subjects, "total": len(subjects)}


@router.get("/subjects")
def get_subjects(response: Response, db: Session = Depends(get_db)):
    """Return all subjects ordered by position."""
    response.headers["Cache-Control"] = f"public, max-age={_CATALOG_TTL}"
    return cached_json("catalog:subjects", _CATALOG_TTL, lambda: _load_subjects(db))


def _load_levels(db: Session):
    try:
        rows = db.execute(
            text("SELECT label, main_group FROM public.levels ORDER BY position")
        ).fetchall()
    except Exception:
        rows = []

    if not rows:
        all_levels = [lbl for g in _FB_GROUPS.values() for lbl in g]
        return {"levels": all_levels, "total": len(all_levels), "groups": _FB_GROUPS}

    groups: dict[str, list[str]] = {
        "primaire": [], "moyen": [], "secondaire": [], "superieur": [], "autre": [],
    }
    for label, main_group in rows:
        key = main_group if main_group in groups else "autre"
        groups[key].append(label)

    all_levels = [r[0] for r in rows]
    return {"levels": all_levels, "total": len(all_levels), "groups": groups}


@router.get("/levels")
def get_levels(response: Response, db: Session = Depends(get_db)):
    """Return school levels grouped by main category."""
    response.headers["Cache-Control"] = f"public, max-age={_CATALOG_TTL}"
    return cached_json("catalog:levels", _CATALOG_TTL, lambda: _load_levels(db))


def _load_goals(db: Session):
    try:
        rows = db.execute(
            text("SELECT id, label FROM public.goal_definitions ORDER BY position")
        ).fetchall()
        goals = [{"id": r[0], "label": r[1]} for r in rows] if rows else _FB_GOALS
    except Exception:
        goals = _FB_GOALS
    return {"goals": goals, "total": len(goals)}


@router.get("/goals")
def get_goals(response: Response, db: Session = Depends(get_db)):
    """Return learning goals ordered by position."""
    response.headers["Cache-Control"] = f"public, max-age={_CATALOG_TTL}"
    return cached_json("catalog:goals", _CATALOG_TTL, lambda: _load_goals(db))


def _load_wilayas(db: Session):
    try:
        rows = db.execute(
            text("SELECT code, name FROM public.wilayas ORDER BY code")
        ).fetchall()
        wilayas = [{"code": str(r[0]), "name": r[1]} for r in rows] if rows else _FB_WILAYAS
    except Exception:
        wilayas = _FB_WILAYAS
    return {"wilayas": wilayas, "total": len(wilayas)}


@router.get("/wilayas")
def get_wilayas(response: Response, db: Session = Depends(get_db)):
    """Return all 58 Algerian wilayas ordered by code."""
    response.headers["Cache-Control"] = f"public, max-age={_CATALOG_TTL}"
    return cached_json("catalog:wilayas", _CATALOG_TTL, lambda: _load_wilayas(db))


@router.get("/store/rewards")
def get_store_rewards(response: Response):
    """Return KP store rewards (static catalogue)."""
    response.headers["Cache-Control"] = f"public, max-age={_CATALOG_TTL}"
    return {"rewards": STORE_REWARDS, "total": len(STORE_REWARDS)}
