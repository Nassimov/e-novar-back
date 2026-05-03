# Backward-compat shim — superseded by gamification.py
from app.models.enums import (  # noqa: F401
    ChallengeAudience,
    ChallengeStatus,
    ProofType,
)
from app.models.gamification import (  # noqa: F401
    Badge,
    Challenge,
    ChallengeParticipation,
    UserBadge,
)

# Legacy aliases
ChallengeDef = Challenge
ChallengeSubmission = ChallengeParticipation
ChallengeSubmissionStatus = ChallengeStatus
