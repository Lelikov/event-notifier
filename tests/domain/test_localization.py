"""Tests for per-recipient template-context localization."""

from event_notifier.domain.localization import localize_template_context, normalize_locale

UTC_CONTEXT = {"start_time": "2026-05-12T22:05:00", "end_time": "2026-05-12T23:05:00", "title": "Session"}


def test_adds_local_keys_in_recipient_zone():
    result = localize_template_context(UTC_CONTEXT, "Europe/Madrid")

    assert result["start_time_local"] == "13.05.2026 00:05"  # UTC+2 in May
    assert result["end_time_local"] == "13.05.2026 01:05"
    assert result["time_zone"] == "Europe/Madrid"


def test_original_keys_are_never_touched():
    result = localize_template_context(UTC_CONTEXT, "Europe/Madrid")

    assert result["start_time"] == "2026-05-12T22:05:00"
    assert result["end_time"] == "2026-05-12T23:05:00"
    assert result["title"] == "Session"


def test_aware_timestamps_are_converted_not_reinterpreted():
    result = localize_template_context({"start_time": "2026-05-12T22:05:00+00:00"}, "Europe/Moscow")

    assert result["start_time_local"] == "13.05.2026 01:05"  # UTC+3


def test_no_time_zone_returns_unchanged_copy():
    result = localize_template_context(UTC_CONTEXT, None)

    assert result == UTC_CONTEXT
    assert result is not UTC_CONTEXT


def test_unknown_zone_degrades_to_unchanged_context():
    result = localize_template_context(UTC_CONTEXT, "Mars/Olympus_Mons")

    assert result == UTC_CONTEXT


def test_unparseable_or_missing_values_are_skipped():
    result = localize_template_context({"start_time": "tomorrow-ish"}, "Europe/Madrid")

    assert "start_time_local" not in result
    assert "end_time_local" not in result
    assert result["time_zone"] == "Europe/Madrid"


def test_locale_is_added_to_context():
    result = localize_template_context(UTC_CONTEXT, "Europe/Madrid", locale="ru")

    assert result["locale"] == "ru"
    assert result["time_zone"] == "Europe/Madrid"


def test_regional_locale_is_reduced_to_primary_subtag():
    result = localize_template_context({}, None, locale="en-GB")

    assert result["locale"] == "en"


def test_absent_locale_adds_no_key():
    result = localize_template_context(UTC_CONTEXT, "Europe/Madrid")

    assert "locale" not in result


def test_normalize_locale_handles_edge_cases():
    assert normalize_locale(None) is None
    assert normalize_locale("") is None
    assert normalize_locale("  ") is None
    assert normalize_locale("RU") == "ru"
    assert normalize_locale("pt_BR") == "pt"
    assert normalize_locale(" en-GB ") == "en"
