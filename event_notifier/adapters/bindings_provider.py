import time

from event_notifier.domain.models.binding import NotificationBinding
from event_notifier.domain.models.notification import ChannelType
from event_notifier.interfaces.sql import ISqlExecutor

_QUERY = (
    "SELECT trigger_event, channel, enabled, unisender_template_id, telegram_body "
    "FROM notification_bindings"
)


class BindingsProvider:
    """Reads notification_bindings with a short in-memory TTL cache so admin edits
    apply within the TTL without a restart."""

    def __init__(self, *, sql: ISqlExecutor, ttl_seconds: int = 30) -> None:
        self._sql = sql
        self._ttl = ttl_seconds
        self._cache: dict[tuple[str, str], NotificationBinding] = {}
        self._expires_at = 0.0

    async def _refresh(self) -> None:
        rows = await self._sql.fetch_all(_QUERY, {})
        self._cache = {
            (r["trigger_event"], r["channel"]): NotificationBinding(
                trigger_event=r["trigger_event"],
                channel=ChannelType(r["channel"]),
                enabled=bool(r["enabled"]),
                unisender_template_id=r["unisender_template_id"],
                telegram_body=r["telegram_body"],
            )
            for r in rows
        }
        self._expires_at = time.monotonic() + self._ttl

    async def get(self, trigger_event: str, channel: ChannelType) -> NotificationBinding | None:
        if time.monotonic() >= self._expires_at:
            await self._refresh()
        return self._cache.get((trigger_event, channel.value))

    def invalidate(self) -> None:
        self._expires_at = 0.0
