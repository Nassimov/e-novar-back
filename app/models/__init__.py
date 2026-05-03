"""
ENOVAR models package.

All SQLModel table classes mirror the public.* schema deployed to Supabase.
Import from this package for convenience; Alembic env.py uses these imports
to detect all metadata for autogenerate.

Architecture notes:
- profiles.id  = Supabase auth.users.id  (never auto-generated in Python)
- full_name     = PostgreSQL GENERATED ALWAYS AS column (read-only)
- JSONB/ARRAY   = PostgreSQL native types; psycopg2 handles serialization
- Circular FK   = bookings.payment_id <-> payments.id is DB-level only
- Triggers      = apply_kp_transaction, handle_homework_grade, etc. live in Supabase
"""

from app.models.enums import (  # noqa: F401
    AppRole,
    BadgeCategory,
    BadgeTier,
    BookingFormula,
    BookingStatus,
    BudgetTier,
    ChallengeAudience,
    ChallengeStatus,
    ClaimStatus,
    EvalStatus,
    Gender,
    HomeworkStatus,
    KpSource,
    Lang,
    LevelMain,
    MonitoringMode,
    NotifChannel,
    ParentLinkStatus,
    PaymentMethodType,
    PaymentStatus,
    ProofType,
    ReferralStatus,
    ReportStatus,
    ReviewStatus,
    SessionStatus,
    SessionType,
    SlotStatus,
    StoreCategory,
    TeacherBadge,
    TeacherStatus,
    TeachingMode,
    ThemePref,
    WithdrawalStatus,
)

# Core profiles
from app.models.profile import (  # noqa: F401
    AdminProfile,
    ParentProfile,
    Profile,
    StudentProfile,
    TeacherProfile,
    UserRole,
)

# Catalogue
from app.models.catalog import (  # noqa: F401
    Level,
    Subject,
    TeacherDiploma,
    TeacherLevel,
    TeacherMode,
    TeacherSessionType,
    TeacherSubject,
    Wilaya,
)

# Relationships
from app.models.parent_link import ParentStudentLink  # noqa: F401
from app.models.scheduling import TeacherSlot  # noqa: F401

# Booking & sessions
from app.models.booking import (  # noqa: F401
    Booking,
    SessionAttendee,
    TutoringSession,
)

# Post-session
from app.models.evaluation import Evaluation  # noqa: F401
from app.models.homework import (  # noqa: F401
    Homework,
    HomeworkGrade,
    HomeworkSubmission,
)
from app.models.review import Favorite, Review  # noqa: F401

# Payments
from app.models.payment import (  # noqa: F401
    Invoice,
    Payment,
    PaymentMethod,
    Wallet,
    Withdrawal,
)

# Live classroom
from app.models.classroom import ClassroomMessage, ClassroomRoom  # noqa: F401

# Messaging
from app.models.conversation import (  # noqa: F401
    ChatMessage,
    Conversation,
    ConversationParticipant,
)

# Gamification
from app.models.kp import KpBalance, KpLevel, KpTransaction  # noqa: F401
from app.models.gamification import (  # noqa: F401
    Badge,
    Challenge,
    ChallengeParticipation,
    UserBadge,
)
from app.models.store import RewardClaim, StoreItem  # noqa: F401

# Social
from app.models.referral import Referral  # noqa: F401

# Notifications
from app.models.notification import (  # noqa: F401
    Notification,
    NotificationPreference,
)

# AI tutor
from app.models.ai import (  # noqa: F401
    AiConversation,
    AiMessage,
    AiProgress,
    PracticeAttempt,
)

# Admin / CMS / Moderation
from app.models.admin import (  # noqa: F401
    AuditLog,
    CmsPage,
    Invitation,
    LegalAcceptance,
    PromoCode,
    PromoRedemption,
    Report,
)
