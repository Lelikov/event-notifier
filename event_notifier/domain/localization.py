"""Per-recipient localization of template context — pure functions, no infrastructure deps.

Booking payloads carry start_time/end_time as UTC ISO strings shared by all
recipients. When the receiver resolved a recipient's IANA time zone
(normalized.participants[].time_zone), templates additionally get
``start_time_local``/``end_time_local``/``time_zone`` rendered in the
recipient's zone. Original keys are never touched, so existing templates and
provider substitutions keep working.

Language/locale localization is NOT done here: cal.com's locale is dropped at
ingress and never reaches the envelope (cross-service contract gap).
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOCALIZABLE_KEYS = ("start_time", "end_time")
_LOCAL_TIME_FORMAT = "%d.%m.%Y %H:%M"


def _to_local(value: Any, zone: ZoneInfo) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)  # naive booking times are UTC
    return moment.astimezone(zone).strftime(_LOCAL_TIME_FORMAT)


def localize_template_context(context: dict[str, Any], time_zone: str | None) -> dict[str, Any]:
    """Return a copy of ``context`` with ``*_local`` keys in the recipient's zone.

    Unknown/missing zones and unparseable values degrade to the unchanged
    context — localization must never block a delivery.
    """
    if not time_zone:
        return dict(context)
    try:
        zone = ZoneInfo(time_zone)
    except ZoneInfoNotFoundError, ValueError:
        return dict(context)

    localized = dict(context)
    for key in _LOCALIZABLE_KEYS:
        local_value = _to_local(context.get(key), zone)
        if local_value is not None:
            localized[f"{key}_local"] = local_value
    localized["time_zone"] = time_zone
    return localized
