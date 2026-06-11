"""Per-operation SQL execution on top of an async_sessionmaker.

Every one-shot call opens its own AsyncSession and commits it, so the executor
is safe to share across concurrent asyncio tasks (consumer handler, outbox
poll loop, cleanup loop). Multi-statement atomic units go through
``transaction()`` which yields a session-bound executor inside one transaction.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SessionSqlExecutor:
    """Executes statements on one already-open session (inside a transaction)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_one(self, query: str, values: dict) -> RowMapping | None:
        result = await self._session.execute(text(query), values)
        return result.mappings().first()

    async def fetch_all(self, query: str, values: dict) -> list[RowMapping]:
        result = await self._session.execute(text(query), values)
        return list(result.mappings().all())

    async def execute(self, query: str, values: dict) -> None:
        await self._session.execute(text(query), values)


class SqlExecutor:
    """Concurrency-safe executor: a fresh session (and transaction) per operation."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def fetch_one(self, query: str, values: dict) -> RowMapping | None:
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(text(query), values)
            return result.mappings().first()

    async def fetch_all(self, query: str, values: dict) -> list[RowMapping]:
        async with self._sessionmaker() as session, session.begin():
            result = await session.execute(text(query), values)
            return list(result.mappings().all())

    async def execute(self, query: str, values: dict) -> None:
        async with self._sessionmaker() as session, session.begin():
            await session.execute(text(query), values)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SessionSqlExecutor]:
        """One transaction for several statements; commits on exit, rolls back on error."""
        async with self._sessionmaker() as session, session.begin():
            yield SessionSqlExecutor(session)
