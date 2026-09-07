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


def compute_variable_duration_amount(leg_amounts: list[int], formula: str, settings: PlatformSettings) -> int:
    """Like compute_pack_prices, but for a booking whose session(s) don't
    all share the same duration — see the hour-based slot-splitting feature
    (app/routers/student_teachers.py's _resolve_leg_range): each leg's own
    already duration-scaled price (price_per_hour * that leg's own hours)
    is summed first, THEN the same admin-configured pack discount
    percentage compute_pack_prices would apply to a uniform
    price_single * pack size is applied to that sum — so a 5-pack mixing a
    1h and a 2h session is still discounted consistently with a uniform
    5-pack of 1h sessions, just on the real total instead of an assumed
    uniform one. "group" is deliberately not handled here — group sessions
    are never time-split, they keep compute_pack_prices' own flat formula.
    """
    total = sum(leg_amounts)
    if formula == "pack5":
        return round(total * (1 - settings.pack5_discount_percent / 100))
    if formula == "pack10":
        return round(total * (1 - settings.pack10_discount_percent / 100))
    return total  # "single" — exactly one leg, no pack discount to apply
