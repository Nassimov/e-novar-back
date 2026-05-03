# Backward-compat shim — superseded by admin.py and referral.py
from app.models.admin import PromoCode, PromoRedemption  # noqa: F401
from app.models.referral import Referral  # noqa: F401
from app.models.review import Favorite  # noqa: F401
