from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.engine import RowMapping


class ISqlSession(Protocol):
    """Statements executed on one open transaction."""

    async def fetch_one(self, query: str, values: dict) -> RowMapping | None: ...

    async def fetch_all(self, query: str, values: dict) -> list[RowMapping]: ...

    async def execute(self, query: str, values: dict) -> None: ...


class ISqlExecutor(Protocol):
    """One-shot statements (each in its own transaction) plus explicit transactions."""

    async def fetch_one(self, query: str, values: dict) -> RowMapping | None: ...

    async def fetch_all(self, query: str, values: dict) -> list[RowMapping]: ...

    async def execute(self, query: str, values: dict) -> None: ...

    def transaction(self) -> AbstractAsyncContextManager[ISqlSession]: ...
