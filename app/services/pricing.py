from __future__ import annotations

from sqlmodel import Session, select

from app.models.admin import PlatformSettings

PACK_SIZES: dict[str, int] = {"single": 1, "pack5": 5, "pack10": 10}


def get_platform_settings(db: Session) -> PlatformSettings:
    """
    Fetch the singleton platform settings row.
    Migration 047 seeds exactly one row (id=true) — if it's somehow missing
    (e.g. a fresh test DB before migrations ran), fall back to safe defaults
    rather than crashing every pricing call.
    """
    settings = db.get(PlatformSettings, True)
    if settings is None:
        settings = PlatformSettings(id=True)
    return settings


def compute_pack_prices(price_single: int, settings: PlatformSettings) -> dict[str, int]:
    """
    The single source of truth for pack/group pricing math. None of these are
    ever stored — always derived from price_single + the current
    admin-configured discount percentages, so a discount change applies
    instantly everywhere. "group" is a single lesson (like "single"), just
    priced lower — it is not a multi-session pack, see PACK_SIZES above.
    """
    pack5 = round(price_single * 5 * (1 - settings.pack5_discount_percent / 100))
    pack10 = round(price_single * 10 * (1 - settings.pack10_discount_percent / 100))
    group = round(price_single * (1 - settings.group_discount_percent / 100))
    return {
        "single": price_single,
        "pack5": pack5,
        "pack10": pack10,
        "group": group,
    }
