"""Tests for Settings: locale-keyed UniSender template id normalization."""

import pytest

from event_notifier.config import Settings

REQUIRED = {
    "database_url": "postgresql+asyncpg://postgres:password@localhost:5432/event_notifier",
    "event_users_url": "http://localhost:8001",
    "event_users_token": "token",
    "unisender_api_key": "key",
    "unisender_from_email": "noreply@example.com",
    "telegram_bot_token": "token",
    "notifier_admin_token": "dev-admin-token",
}


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **REQUIRED, **overrides)


def test_default_locale_is_russian():
    assert make_settings().default_locale == "ru"


def test_flat_template_ids_map_to_default_locale():
    settings = make_settings(unisender_template_ids={"BOOKING_CREATED": "tmpl-1"})

    assert settings.unisender_template_ids_by_locale() == {"ru": {"BOOKING_CREATED": "tmpl-1"}}


def test_locale_keyed_template_ids_pass_through():
    settings = make_settings(
        unisender_template_ids={
            "ru": {"BOOKING_CREATED": "tmpl-ru"},
            "EN": {"BOOKING_CREATED": "tmpl-en"},
        }
    )

    assert settings.unisender_template_ids_by_locale() == {
        "ru": {"BOOKING_CREATED": "tmpl-ru"},
        "en": {"BOOKING_CREATED": "tmpl-en"},
    }


def test_mixed_template_ids_merge_flat_into_default_locale():
    settings = make_settings(
        unisender_template_ids={
            "BOOKING_CANCELLED": "tmpl-flat",
            "ru": {"BOOKING_CREATED": "tmpl-ru"},
            "en": {"BOOKING_CREATED": "tmpl-en"},
        }
    )

    assert settings.unisender_template_ids_by_locale() == {
        "ru": {"BOOKING_CREATED": "tmpl-ru", "BOOKING_CANCELLED": "tmpl-flat"},
        "en": {"BOOKING_CREATED": "tmpl-en"},
    }


def test_custom_default_locale_attracts_flat_entries():
    settings = make_settings(default_locale="en", unisender_template_ids={"BOOKING_CREATED": "tmpl-1"})

    assert settings.unisender_template_ids_by_locale() == {"en": {"BOOKING_CREATED": "tmpl-1"}}


def test_empty_template_ids_yield_empty_mapping():
    assert make_settings().unisender_template_ids_by_locale() == {}


@pytest.mark.parametrize("value", [{"BOOKING_CREATED": "a"}, {"ru": {"BOOKING_CREATED": "a"}}])
def test_both_config_shapes_validate(value):
    assert make_settings(unisender_template_ids=value).unisender_template_ids == value


def test_blacklisted_rejection_trigger_is_accepted_per_locale():
    settings = make_settings(
        unisender_template_ids={
            "ru": {"BOOKING_REJECTED_BLACKLISTED": "tmpl-bl-ru"},
            "en": {"BOOKING_REJECTED_BLACKLISTED": "tmpl-bl-en"},
        }
    )

    by_locale = settings.unisender_template_ids_by_locale()
    assert by_locale["ru"]["BOOKING_REJECTED_BLACKLISTED"] == "tmpl-bl-ru"
    assert by_locale["en"]["BOOKING_REJECTED_BLACKLISTED"] == "tmpl-bl-en"


def test_unknown_trigger_in_locale_dict_is_rejected():
    with pytest.raises(ValueError, match="unknown trigger events"):
        make_settings(unisender_template_ids={"ru": {"BOOKING_TYPO": "tmpl-1"}})


def test_unknown_flat_trigger_is_rejected():
    with pytest.raises(ValueError, match="unknown trigger event"):
        make_settings(unisender_template_ids={"BOOKING_TYPO": "tmpl-1"})


def test_external_api_base_urls_default_to_production():
    settings = make_settings()

    assert settings.unisender_base_url == "https://go.unisender.ru"
    assert settings.telegram_base_url == "https://api.telegram.org"


def test_external_api_base_urls_are_overridable():
    settings = make_settings(
        unisender_base_url="http://mocks:8080/unisender",
        telegram_base_url="http://mocks:8080/telegram",
    )

    assert settings.unisender_base_url == "http://mocks:8080/unisender"
    assert settings.telegram_base_url == "http://mocks:8080/telegram"
