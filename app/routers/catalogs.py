from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["catalogs"])

SUBJECTS = [
    "Mathématiques", "Physique", "Chimie", "Sciences naturelles",
    "Français", "Arabe", "Anglais", "Tamazight",
    "Histoire-Géographie", "Philosophie", "Économie",
    "Informatique", "Technologie", "Éducation islamique",
    "Éducation civique", "Sport", "Musique", "Arts plastiques",
]

LEVELS = [
    # Primaire
    "1ère AP", "2ème AP", "3ème AP", "4ème AP", "5ème AP",
    # Moyen
    "1ère AM", "2ème AM", "3ème AM", "4ème AM",
    # Secondaire
    "1ère AS", "2ème AS", "3ème AS",
    # Supérieur
    "Licence 1", "Licence 2", "Licence 3", "Master 1", "Master 2",
    # Adulte / Autre
    "Adulte", "Professionnel",
]

ALGERIAN_WILAYAS = [
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
]

STORE_REWARDS = [
    {"id": "discount_10", "name": "Réduction 10%", "description": "10% de réduction sur la prochaine session", "kp_cost": 500, "type": "discount", "icon": "🏷️"},
    {"id": "discount_20", "name": "Réduction 20%", "description": "20% de réduction sur la prochaine session", "kp_cost": 900, "type": "discount", "icon": "🎫"},
    {"id": "free_session", "name": "Session gratuite", "description": "Une session de 60 min offerte", "kp_cost": 2000, "type": "session", "icon": "🎓"},
    {"id": "priority_match", "name": "Matching prioritaire", "description": "En tête des résultats pendant 7 jours", "kp_cost": 400, "type": "perk", "icon": "⭐"},
    {"id": "avatar_frame", "name": "Cadre avatar premium", "description": "Cadre exclusif pour votre profil", "kp_cost": 200, "type": "cosmetic", "icon": "🖼️"},
    {"id": "analytics_report", "name": "Rapport d'analyse", "description": "Rapport détaillé de vos progrès", "kp_cost": 300, "type": "report", "icon": "📊"},
]


@router.get("/subjects")
async def get_subjects():
    """Get all available subjects."""
    return {"subjects": SUBJECTS, "total": len(SUBJECTS)}


@router.get("/levels")
async def get_levels():
    """Get all school levels."""
    return {
        "levels": LEVELS,
        "total": len(LEVELS),
        "groups": {
            "primaire": LEVELS[:5],
            "moyen": LEVELS[5:9],
            "secondaire": LEVELS[9:12],
            "superieur": LEVELS[12:17],
            "autre": LEVELS[17:],
        },
    }


@router.get("/wilayas")
async def get_wilayas():
    """Get all 48 Algerian wilayas."""
    return {"wilayas": ALGERIAN_WILAYAS, "total": len(ALGERIAN_WILAYAS)}


@router.get("/store/rewards")
async def get_store_rewards():
    """Get all available KP store rewards."""
    return {"rewards": STORE_REWARDS, "total": len(STORE_REWARDS)}
