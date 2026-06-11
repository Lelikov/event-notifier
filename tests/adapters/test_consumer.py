"""Tests for NotificationConsumer envelope/contract parsing."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from event_schemas.notification import NotificationCommandPayload
from event_schemas.queues import NOTIFICATION_COMMANDS_QUEUE
from pydantic import ValidationError

from event_notifier.adapters.consumer import NotificationConsumer, _resolve_recipients


def make_consumer() -> tuple[NotificationConsumer, AsyncMock]:
    use_case = AsyncMock()
    consumer = NotificationConsumer(
        broker=MagicMock(),
        exchange=MagicMock(),
        use_case=use_case,
    )
    return consumer, use_case


def make_headers(event_type: str, booking_id: str = "b-1") -> dict:
    return {
        "ce-id": "evt-1",
        "ce-source": "booking",
        "ce-type": event_type,
        "ce-specversion": "1.0",
        "ce-bookingid": booking_id,
        "content-type": "application/json",
    }


def make_command_body() -> bytes:
    return json.dumps(
        {
            "original": {
                "booking_id": "b-1",
                "trigger_event": "BOOKING_CREATED",
                "recipients": [
                    {"email": "org@example.com", "role": "organizer"},
                    {"email": "cli@example.com", "role": "client"},
                ],
                "template_data": {"title": "Session"},
            },
            "normalized": {
                "participants": [
                    {"email": "org@example.com", "role": "organizer", "user_id": "uuid-org"},
                    {"email": "cli@example.com", "role": "client", "user_id": "uuid-cli"},
                ]
            },
        }
    ).encode()


async def test_unwraps_envelope_and_resolves_user_ids() -> None:
    consumer, use_case = make_consumer()

    await consumer._handle(  # noqa: SLF001
        body=make_command_body(),
        headers=make_headers("notification.send_requested"),
    )

    use_case.execute.assert_awaited_once()
    event = use_case.execute.call_args.args[0]
    assert event.booking_id == "b-1"
    assert event.data["template_data"] == {"title": "Session"}
    assert event.data["recipients"] == [
        {"email": "org@example.com", "role": "organizer", "user_id": "uuid-org"},
        {"email": "cli@example.com", "role": "client", "user_id": "uuid-cli"},
    ]


async def test_booking_id_falls_back_to_payload() -> None:
    consumer, use_case = make_consumer()
    headers = make_headers("notification.send_requested")
    del headers["ce-bookingid"]

    await consumer._handle(body=make_command_body(), headers=headers)  # noqa: SLF001

    event = use_case.execute.call_args.args[0]
    assert event.booking_id == "b-1"


async def test_invalid_command_payload_is_dead_lettered() -> None:
    consumer, use_case = make_consumer()
    body = json.dumps({"original": {"recipients": "garbage"}, "normalized": {"participants": []}}).encode()

    with pytest.raises(ValidationError):
        await consumer._handle(body=body, headers=make_headers("notification.send_requested"))  # noqa: SLF001

    use_case.execute.assert_not_awaited()


async def test_unknown_event_type_is_skipped() -> None:
    consumer, use_case = make_consumer()

    await consumer._handle(  # noqa: SLF001
        body=json.dumps({"original": {}, "normalized": {"participants": []}}).encode(),
        headers=make_headers("getstream.member.added"),
    )

    use_case.execute.assert_not_awaited()


def test_resolve_recipients_without_user_id_keeps_email_and_role() -> None:
    payload = NotificationCommandPayload(
        booking_id="b-1",
        trigger_event="BOOKING_CREATED",
        recipients=[{"email": "org@example.com", "role": "organizer"}],
    )

    recipients = _resolve_recipients(payload, [])

    assert recipients == [{"email": "org@example.com", "role": "organizer"}]


def test_uses_canonical_queue_spec() -> None:
    assert NOTIFICATION_COMMANDS_QUEUE.name == "events.notification.commands"
    assert NOTIFICATION_COMMANDS_QUEUE.arguments == {
        "x-max-priority": 10,
        "x-dead-letter-exchange": "events.dlx",
        "x-dead-letter-routing-key": "events.notification.commands.dlq",
    }
