from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from app.dependencies import get_db, require_role
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.session import Session as SessionModel, SessionStatus
from app.models.teacher import TeacherProfile, TeacherWithdrawal
from app.models.user import User, UserRole
from app.schemas.admin import StatsResponse

router = APIRouter(tags=["admin-stats"])

admin_required = require_role("admin")


@router.get("/", response_model=StatsResponse)
async def get_platform_stats(
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Get platform-wide KPIs and statistics."""
    # User counts
    all_users = db.exec(select(User)).all()
    total_users = len(all_users)
    total_teachers = sum(1 for u in all_users if u.role == UserRole.teacher)
    total_students = sum(1 for u in all_users if u.role == UserRole.student)

    # Booking counts
    all_bookings = db.exec(select(Booking)).all()
    total_bookings = len(all_bookings)

    # Session counts
    completed_sessions = db.exec(
        select(SessionModel).where(SessionModel.status == SessionStatus.completed)
    ).all()
    total_sessions_completed = len(completed_sessions)

    # Revenue
    completed_payments = db.exec(
        select(Payment).where(Payment.status == PaymentStatus.completed)
    ).all()
    total_revenue_dzd = sum(p.amount for p in completed_payments)

    # Active users this week
    week_ago = datetime.utcnow() - timedelta(days=7)
    recent_bookings = db.exec(
        select(Booking).where(Booking.created_at >= week_ago)
    ).all()
    active_users_this_week = len({str(b.student_id) for b in recent_bookings})

    # New users this month
    month_ago = datetime.utcnow() - timedelta(days=30)
    new_users_this_month = sum(1 for u in all_users if u.created_at >= month_ago)

    # Pending withdrawals
    pending_withdrawals = len(db.exec(
        select(TeacherWithdrawal).where(TeacherWithdrawal.status == "pending")
    ).all())

    # Pending teacher approvals
    pending_teacher_approvals = len(db.exec(
        select(TeacherProfile).where(TeacherProfile.is_approved == False)
    ).all())

    return StatsResponse(
        total_users=total_users,
        total_teachers=total_teachers,
        total_students=total_students,
        total_bookings=total_bookings,
        total_sessions_completed=total_sessions_completed,
        total_revenue_dzd=total_revenue_dzd,
        active_users_this_week=active_users_this_week,
        new_users_this_month=new_users_this_month,
        pending_withdrawals=pending_withdrawals,
        pending_teacher_approvals=pending_teacher_approvals,
    )


@router.get("/revenue")
async def get_revenue_stats(
    period: str = "month",
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Get detailed revenue statistics."""
    if period == "week":
        since = datetime.utcnow() - timedelta(days=7)
    elif period == "month":
        since = datetime.utcnow() - timedelta(days=30)
    elif period == "year":
        since = datetime.utcnow() - timedelta(days=365)
    else:
        since = datetime.utcnow() - timedelta(days=30)

    payments = db.exec(
        select(Payment).where(
            Payment.status == PaymentStatus.completed,
            Payment.created_at >= since,
        )
    ).all()

    total = sum(p.amount for p in payments)
    by_method: Dict[str, int] = {}
    for p in payments:
        method = p.method.value
        by_method[method] = by_method.get(method, 0) + p.amount

    return {
        "period": period,
        "total_revenue_dzd": total,
        "transactions_count": len(payments),
        "average_transaction": total // len(payments) if payments else 0,
        "by_method": by_method,
    }


@router.get("/users-growth")
async def get_user_growth(
    current_user: Dict[str, Any] = Depends(admin_required),
    db: Session = Depends(get_db),
):
    """Get user growth over the last 30 days."""
    users = db.exec(select(User)).all()
    since = datetime.utcnow() - timedelta(days=30)
    recent = [u for u in users if u.created_at >= since]

    by_role = {
        "student": sum(1 for u in recent if u.role == UserRole.student),
        "teacher": sum(1 for u in recent if u.role == UserRole.teacher),
        "parent": sum(1 for u in recent if u.role == UserRole.parent),
    }

    return {
        "last_30_days": len(recent),
        "by_role": by_role,
        "total_users": len(users),
    }
