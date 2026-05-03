# Backward-compat shim — superseded by booking.py
from app.models.booking import (  # noqa: F401
    Booking,
    SessionAttendee,
    TutoringSession,
)
from app.models.enums import (  # noqa: F401
    BookingStatus,
    SessionStatus,
    TeachingMode,
)

# Legacy alias
Session = TutoringSession
SessionModel = TutoringSession
