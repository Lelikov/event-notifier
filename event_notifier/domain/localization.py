"""Per-recipient localization of template context — pure functions, no infrastructure deps.

Booking payloads carry start_time/end_time as UTC ISO strings shared by all
recipients. When the receiver resolved a recipient's IANA time zone
(normalized.participants[].time_zone), templates additionally get
``start_time_local``/``end_time_local``/``time_zone`` rendered in the
recipient's zone. Original keys are never touched, so existing templates and
provider substitutions keep working.

Language localization: when the recipient's preferred language is known
(producer recipients[].locale / normalized.participants[].locale, originally
cal.com ``language.locale``), the template context gets a ``locale`` key.
Channels use it to select the template language, falling back to the
configured default locale (``Settings.default_locale``, "ru").
"""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LOCALIZABLE_KEYS = ("start_time", "end_time")
_LOCAL_TIME_FORMAT = "%d.%m.%Y %H:%M"


def normalize_locale(locale: str | None) -> str | None:
    """Reduce a language tag to its lowercase primary subtag: 'pt-BR'/'ru_RU' → 'pt'/'ru'.

    Unknown/empty values degrade to None so callers fall back to the default locale.
    """
    if not locale:
        return None
    primary = locale.strip().replace("_", "-").split("-")[0].lower()
    if not primary:
        return None
    return primary


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


def localize_template_context(
    context: dict[str, Any],
    time_zone: str | None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Return a copy of ``context`` with ``*_local`` time keys and the recipient's ``locale``.

    Unknown/missing zones, unparseable values and absent locales degrade to the
    unchanged context — localization must never block a delivery.
    """
    localized = dict(context)
    normalized_locale = normalize_locale(locale)
    if normalized_locale:
        localized["locale"] = normalized_locale

    zone = _zone(time_zone)
    if zone is None:
        return localized

    for key in _LOCALIZABLE_KEYS:
        local_value = _to_local(context.get(key), zone)
        if local_value is not None:
            localized[f"{key}_local"] = local_value
    localized["time_zone"] = time_zone
    return localized


def _zone(time_zone: str | None) -> ZoneInfo | None:
    if not time_zone:
        return None
    try:
        return ZoneInfo(time_zone)
    except ZoneInfoNotFoundError, ValueError:
        return None
