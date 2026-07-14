from __future__ import annotations

"""
Teacher payout-rail validation — shared by teacher onboarding
(app/routers/onboarding.py) and the teacher profile payout-info update
endpoint (app/routers/teachers.py), so both entry points enforce the exact
same rule the DB CHECK constraint (migration 059) encodes:

  - rail == "bank"      -> iban + bank_holder required, payout_phone forbidden
  - rail == "baridimob" -> payout_phone required, iban/bank_holder forbidden

Only display/reference metadata is ever accepted here — never a full card
PAN, CVV or expiry.
"""

import re
from typing import Optional

from fastapi import HTTPException

_PHONE_RE = re.compile(r"^0[5-7]\d{8}$")
_LAST4_RE = re.compile(r"^\d{4}$")

VALID_PAYOUT_RAILS = ("bank", "baridimob")


def validate_payout_fields(
    payout_rail: Optional[str],
    iban: Optional[str],
    bank_holder: Optional[str],
    payout_phone: Optional[str],
    bank_last4: Optional[str] = None,
) -> str:
    """Validate a (rail, identifier) combo. Returns the normalized rail.
    Raises HTTPException(422) on any violation."""
    rail = (payout_rail or "bank").strip().lower()
    if rail not in VALID_PAYOUT_RAILS:
        raise HTTPException(status_code=422, detail="payout_rail doit être 'bank' ou 'baridimob'.")

    if rail == "bank":
        if not (iban and iban.strip()) or not (bank_holder and bank_holder.strip()):
            raise HTTPException(
                status_code=422,
                detail="IBAN/RIB et titulaire du compte requis pour un versement bancaire.",
            )
        if bank_last4 and not _LAST4_RE.match(bank_last4.strip()):
            raise HTTPException(status_code=422, detail="bank_last4 doit être exactement 4 chiffres.")
    else:  # baridimob
        if not payout_phone or not _PHONE_RE.match(payout_phone.strip()):
            raise HTTPException(
                status_code=422,
                detail="Numéro BaridiMob invalide (format attendu : 05/06/07 suivi de 8 chiffres).",
            )

    return rail
