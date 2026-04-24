from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sqlalchemy.engine import RowMapping


class ISqlExecutor(Protocol):
    async def fetch_one(self, query: str, values: dict) -> RowMapping | None: ...

    async def fetch_all(self, query: str, values: dict) -> list[RowMapping]: ...

    async def execute(self, query: str, values: dict) -> None: ...

    async def commit(self) -> None: ...
