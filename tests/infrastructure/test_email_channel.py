"""Tests for EmailChannel against the UniSender Go contract."""

import json

import httpx
import pytest
import respx
from event_schemas.types import TriggerEvent
from httpx import AsyncClient, Response

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.domain.models.notification import ChannelContact, ChannelType
from event_notifier.infrastructure.channels.email import EmailChannel, flatten_substitutions

SEND_URL = "https://go.unisender.ru/ru/transactional/api/v1/email/send.json"


class _FakeSql:
    def __init__(self, rows):
        self.rows = rows

    async def fetch_all(self, query, values):
        return self.rows

    async def fetch_one(self, query, values):
        return None

    async def execute(self, query, values):
        pass

    def transaction(self):
        raise NotImplementedError


def _bindings(rows) -> BindingsProvider:
    return BindingsProvider(sql=_FakeSql(rows), ttl_seconds=60)


def _email_row(trigger: str, template_id: str | None = "tmpl-uuid-created", enabled: bool = True) -> dict:
    return {
        "trigger_event": trigger,
        "recipient_role": "client",
        "channel": "email",
        "enabled": enabled,
        "unisender_template_id": template_id,
        "telegram_body": None,
    }


_DEFAULT_BINDINGS_ROWS = [
    _email_row("BOOKING_CREATED", "tmpl-uuid-created"),
    _email_row("BOOKING_CANCELLED", "tmpl-uuid-cancelled"),
    _email_row("BOOKING_REJECTED_BLACKLISTED", "tmpl-uuid-blacklisted"),
    _email_row("BOOKING_RESCHEDULED", "tmpl-uuid-rescheduled"),
    _email_row("BOOKING_REASSIGNED", "tmpl-uuid-reassigned"),
    _email_row("BOOKING_REJECTED", "tmpl-uuid-rejected"),
    # BOOKING_REMINDER intentionally absent (no template configured)
]


@pytest.fixture
async def email_channel():
    bindings = _bindings(_DEFAULT_BINDINGS_ROWS)
    async with AsyncClient(base_url="https://go.unisender.ru", headers={"X-API-KEY": "secret-key"}) as client:
        yield EmailChannel(
            http_client=client,
            bindings=bindings,
            from_email="noreply@example.com",
            from_name="Test",
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
        trigger_event=TriggerEvent.BOOKING_REMINDER,  # not configured in default bindings
        template_data={},
    )

    assert result.success is False
    assert result.retryable is False


async def test_disabled_binding_fails_permanently(contact):
    bindings = _bindings([_email_row("BOOKING_CREATED", "tmpl-uuid-created", enabled=False)])
    async with AsyncClient(base_url="https://go.unisender.ru") as client:
        channel = EmailChannel(
            http_client=client, bindings=bindings, from_email="a@b.com", from_name="X"
        )
        result = await channel.send(
            contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={}
        )

    assert result.success is False
    assert result.retryable is False


async def test_null_template_id_fails_permanently(contact):
    bindings = _bindings([_email_row("BOOKING_CREATED", None)])
    async with AsyncClient(base_url="https://go.unisender.ru") as client:
        channel = EmailChannel(
            http_client=client, bindings=bindings, from_email="a@b.com", from_name="X"
        )
        result = await channel.send(
            contact=contact, trigger_event=TriggerEvent.BOOKING_CREATED, template_data={}
        )

    assert result.success is False
    assert result.retryable is False


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
