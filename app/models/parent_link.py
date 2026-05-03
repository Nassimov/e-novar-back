from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class ParentStudentLink(SQLModel, table=True):
    """
    Mirrors public.parent_student_links.
    A parent requests monitoring access to a student account via invite_code.
    Student accepts → status becomes 'accepted'.
    """

    __tablename__ = "parent_student_links"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    parent_id: UUID = Field(foreign_key="profiles.id", index=True)
    student_id: UUID = Field(foreign_key="profiles.id", index=True)
    status: str = Field(default="pending")               # public.parent_link_status
    invite_code: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    accepted_at: Optional[datetime] = Field(default=None)

    __table_args__ = (
        sa.UniqueConstraint("parent_id", "student_id", name="uq_parent_student"),
    )
