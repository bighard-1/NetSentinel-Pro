"""Alembic environment for the application's existing SQLite engine."""

from alembic import context

from database import Base, engine
import models  # noqa: F401 - Register metadata


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
