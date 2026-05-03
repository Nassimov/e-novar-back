# Backward-compat shim — superseded by profile.py and scheduling.py
from app.models.catalog import TeacherDiploma  # noqa: F401
from app.models.payment import Wallet, Withdrawal  # noqa: F401
from app.models.profile import TeacherProfile  # noqa: F401
from app.models.scheduling import TeacherSlot  # noqa: F401

# Legacy alias
TeacherWithdrawal = Withdrawal
