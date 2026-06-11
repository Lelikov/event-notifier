"""Tests for EmailChannel against the UniSender Go contract."""

import json

import httpx
import pytest
import respx
from event_schemas.types import TriggerEvent
from httpx import AsyncClient, Response

from event_notifier.domain.models.notification import ChannelContact, ChannelType
from event_notifier.infrastructure.channels.email import EmailChannel, flatten_substitutions

SEND_URL = "https://go.unisender.ru/ru/transactional/api/v1/email/send.json"

TEMPLATE_IDS_BY_LOCALE = {
    "ru": {"BOOKING_CREATED": "tmpl-uuid-created", "BOOKING_CANCELLED": "tmpl-uuid-cancelled"},
    "en": {"BOOKING_CREATED": "tmpl-uuid-created-en"},
}


@pytest.fixture
async def email_channel():
    async with AsyncClient(base_url="https://go.unisender.ru", headers={"X-API-KEY": "secret-key"}) as client:
        yield EmailChannel(
            http_client=client,
            template_ids_by_locale=TEMPLATE_IDS_BY_LOCALE,
            from_email="noreply@example.com",
            from_name="Test",
            default_locale="ru",
        )


@pytest.fixture
def contact():
    return ChannelContact(
        channel=ChannelType.EMAIL,
        contact_id="recipient@example.com",
        user_id="uuid-recipient-001",
        email="recipient@example.com",
        role="client",
    )


async def test_sends_exact_unisender_payload(email_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"status": "success", "job_id": "job-xyz"}))

        result = await email_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CREATED,
            template_data={
                "start_time": "2026-06-12T10:00:00Z",
                "organizer_name": "Jane",
                "recipients": [{"email": "x@example.com"}],  # nested → must be dropped
                "booking_id": "b-1",
            },
        )

    assert result.success is True
    assert result.message_id == "job-xyz"
    request = route.calls[0].request
    # API key travels in the header, never in the body
    assert request.headers["X-API-KEY"] == "secret-key"
    body = json.loads(request.content)
    assert "api_key" not in body
    assert body["message"]["template_id"] == "tmpl-uuid-created"
    assert body["message"]["recipients"] == [{"email": "recipient@example.com"}]
    assert body["message"]["from_email"] == "noreply@example.com"
    # Flat scalars only — the nested recipients list never reaches the provider
    assert body["message"]["global_substitutions"] == {
        "start_time": "2026-06-12T10:00:00Z",
        "organizer_name": "Jane",
        "booking_id": "b-1",
    }


async def test_unconfigured_template_fails_permanently(email_channel, contact):
    result = await email_channel.send(
        contact=contact,
        trigger_event=TriggerEvent.BOOKING_REMINDER,  # not configured for any locale
        template_data={},
    )

    assert result.success is False
    assert result.retryable is False


async def test_locale_selects_locale_keyed_template_id(email_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"status": "success", "job_id": "j-1"}))

        result = await email_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CREATED,
            template_data={"locale": "en"},
        )

    assert result.success is True
    body = json.loads(route.calls[0].request.content)
    assert body["message"]["template_id"] == "tmpl-uuid-created-en"


async def test_locale_without_own_template_falls_back_to_default_locale(email_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"status": "success", "job_id": "j-2"}))

        # 'en' has no BOOKING_CANCELLED template — the default 'ru' set is used.
        result = await email_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CANCELLED,
            template_data={"locale": "en"},
        )

    assert result.success is True
    body = json.loads(route.calls[0].request.content)
    assert body["message"]["template_id"] == "tmpl-uuid-cancelled"


async def test_missing_locale_uses_default_locale_template(email_channel, contact):
    with respx.mock:
        route = respx.post(SEND_URL).mock(return_value=Response(200, json={"status": "success", "job_id": "j-3"}))

        result = await email_channel.send(
            contact=contact,
            trigger_event=TriggerEvent.BOOKING_CREATED,
            template_data={},
        )

    assert result.success is True
    body = json.loads(route.calls[0].request.content)
    assert body["message"]["template_id"] == "tmpl-uuid-created"


async def test_4xx_is_permanent(email_channel, contact):
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=Response(400, json={"failure_reason": "invalid template"}))

        result = await email_channel.send(contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={})

    assert result.success is False
    assert result.retryable is False


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
async def test_transient_statuses_are_retryable(email_channel, contact, status_code):
    with respx.mock:
        respx.post(SEND_URL).mock(return_value=Response(status_code))

        result = await email_channel.send(contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={})

    assert result.success is False
    assert result.retryable is True


async def test_transport_error_is_retryable(email_channel, contact):
    with respx.mock:
        respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("refused"))

        result = await email_channel.send(contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={})

    assert result.success is False
    assert result.retryable is True


def test_flatten_substitutions_drops_nested_and_stringifies():
    flat = flatten_substitutions({"a": 1, "b": "x", "c": True, "d": [1], "e": {"k": "v"}, "f": None})

    assert flat == {"a": "1", "b": "x", "c": "True"}
