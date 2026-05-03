# Backward-compat shim — superseded by profile.py
# Profile.id = Supabase auth.users.id (no separate supabase_id field)
from app.models.profile import (  # noqa: F401
    AdminProfile,
    ParentProfile,
    Profile,
    StudentProfile,
    TeacherProfile,
    UserRole,
)

# Alias kept for legacy imports: `from app.models.user import User`
User = Profile
