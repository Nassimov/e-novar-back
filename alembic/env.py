from logging.config import fileConfig
import os
from sqlalchemy import engine_from_config, pool
from alembic import context
from dotenv import load_dotenv

load_dotenv()

# Import all models so they register with SQLModel metadata
from app.models.user import User  # noqa: F401
from app.models.teacher import TeacherProfile  # noqa: F401
from app.models.booking import Booking  # noqa: F401
from app.models.session import Session  # noqa: F401
from app.models.payment import Payment  # noqa: F401
from app.models.message import Conversation, Message  # noqa: F401
from app.models.kp import KpAccount, KpTransaction  # noqa: F401
from app.models.homework import Homework, HomeworkSubmission, HomeworkGrade  # noqa: F401
from app.models.challenge import ChallengeDef, ChallengeSubmission  # noqa: F401
from app.models.review import Review  # noqa: F401
from app.models.notification import Notification  # noqa: F401
from app.models.promo import PromoCode, Referral  # noqa: F401

from sqlmodel import SQLModel

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata

database_url = os.environ.get("DATABASE_URL", "")
config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
