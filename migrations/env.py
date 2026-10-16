"""Alembic environment. The URL is the app's own DATABASE_URL (async driver),
so moving to Postgres is a config change only. Works from the CLI and from
app.db.init_db, which hands over its open connection."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.crm import models as _crm  # noqa: F401  (registers the CRM tables)
from app.db import Base

target_metadata = Base.metadata


def run_sync(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as connection:
        await connection.run_sync(run_sync)
    await engine.dispose()


connection = context.config.attributes.get("connection")
if context.is_offline_mode():
    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif connection is not None:
    run_sync(connection)
else:
    asyncio.run(run_async())
