from __future__ import annotations

from enum import Enum


class AppRole(str, Enum):
    student = "student"
    teacher = "teacher"
    parent = "parent"
    admin = "admin"
    moderator = "moderator"


class Gender(str, Enum):
    female = "female"
    male = "male"
    other = "other"


class Lang(str, Enum):
    fr = "fr"
    ar = "ar"
    en = "en"


class ThemePref(str, Enum):
    light = "light"
    dark = "dark"
    system = "system"


class LevelMain(str, Enum):
    primary = "primary"
    bem = "bem"
    lycee = "lycee"
    univ = "univ"


class MonitoringMode(str, Enum):
    solo = "solo"
    parent = "parent"


class BudgetTier(str, Enum):
    eco = "eco"
    std = "std"
    premium = "premium"


class TeachingMode(str, Enum):
    online = "online"
    presentiel = "presentiel"
    hybrid = "hybrid"


class SessionType(str, Enum):
    individual = "individual"
    group = "group"


class SlotStatus(str, Enum):
    open = "open"
    booked = "booked"
    blocked = "blocked"
    draft = "draft"


class BookingFormula(str, Enum):
    single = "single"
    pack5 = "pack5"
    monthly = "monthly"


class BookingStatus(str, Enum):
    pending = "pending"
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"
    no_show = "no_show"


class SessionStatus(str, Enum):
    scheduled = "scheduled"
    waiting = "waiting"
    live = "live"
    completed = "completed"
    no_show = "no_show"
    cancelled = "cancelled"


class HomeworkStatus(str, Enum):
    todo = "todo"
    submitted = "submitted"
    graded = "graded"


class EvalStatus(str, Enum):
    draft = "draft"
    sent = "sent"


class ReviewStatus(str, Enum):
    visible = "visible"
    hidden = "hidden"
    flagged = "flagged"


class PaymentMethodType(str, Enum):
    cib = "cib"
    edahabia = "edahabia"
    baridimob = "baridimob"
    visa = "visa"
    transfer = "transfer"
    cash = "cash"


class PaymentStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    succeeded = "succeeded"
    failed = "failed"
    refunded = "refunded"


class WithdrawalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    paid = "paid"


class KpSource(str, Enum):
    lesson = "lesson"
    quiz = "quiz"
    badge = "badge"
    reward = "reward"
    challenge = "challenge"
    bonus = "bonus"
    referral = "referral"
    homework = "homework"
    evaluation = "evaluation"
    promo = "promo"


class BadgeTier(str, Enum):
    bronze = "bronze"
    silver = "silver"
    gold = "gold"
    platinum = "platinum"


class BadgeCategory(str, Enum):
    apprentissage = "Apprentissage"
    regularite = "Régularité"
    excellence = "Excellence"
    communaute = "Communauté"


class ChallengeAudience(str, Enum):
    student = "student"
    teacher = "teacher"
    both = "both"


class ProofType(str, Enum):
    image = "image"
    pdf = "pdf"
    image_or_pdf = "image-or-pdf"


class ChallengeStatus(str, Enum):
    in_progress = "in_progress"
    submitted = "submitted"
    approved = "approved"
    declined = "declined"
    cancelled = "cancelled"


class StoreCategory(str, Enum):
    powerups = "powerups"
    digital = "digital"
    physical = "physical"
    services = "services"
    travel = "travel"


class ClaimStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    delivered = "delivered"
    cancelled = "cancelled"


class ReferralStatus(str, Enum):
    pending = "pending"
    registered = "registered"
    validated = "validated"


class NotifChannel(str, Enum):
    in_app = "in_app"
    push = "push"
    email = "email"
    sms = "sms"


class ParentLinkStatus(str, Enum):
    pending = "pending"
    accepted = "accepted"
    revoked = "revoked"


class TeacherStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    suspended = "suspended"


class TeacherBadge(str, Enum):
    top = "top"
    new = "new"
    verified = "verified"
    recommended = "recommended"


class ReportStatus(str, Enum):
    open = "open"
    reviewed = "reviewed"
    dismissed = "dismissed"
    actioned = "actioned"
