"""Tests for NotificationConsumer: wire contract, ack policy, retry classification."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from faststream.exceptions import NackMessage, RejectMessage
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange, TestRabbitBroker
from sqlalchemy.exc import OperationalError

from event_notifier.adapters.consumer import NotificationConsumer, parse_notification_command
from event_notifier.interfaces.users_client import UsersServiceError


class FakeMessage:
    def __init__(self, headers: dict[str, str], body: bytes) -> None:
        self.headers = headers
        self.body = body


def make_headers(event_type: str = "notification.send_requested", booking_id: str | None = "b-1") -> dict:
    headers = {
        "ce-id": "evt-1",
        "ce-source": "booking",
        "ce-type": event_type,
        "ce-specversion": "1.0",
        "content-type": "application/json",
    }
    if booking_id:
        headers["ce-bookingid"] = booking_id
    return headers


def make_command_body() -> bytes:
    """The exact on-the-wire envelope produced by event-receiver."""
    return json.dumps(
        {
            "original": {
                "booking_id": "b-1",
                "trigger_event": "BOOKING_CREATED",
                "recipients": [
                    {"email": "org@example.com", "role": "organizer", "locale": "ru"},
                    {"email": "cli@example.com", "role": "client"},
                ],
                "template_data": {"title": "Session", "start_time": "2026-06-12T10:00:00Z"},
            },
            "normalized": {
                "participants": [
                    {
                        "email": "org@example.com",
                        "role": "organizer",
                        "user_id": "uuid-org",
                        "time_zone": "Europe/Moscow",
                    },
                    {"email": "cli@example.com", "role": "client", "user_id": "uuid-cli", "locale": "en"},
                ]
            },
        }
    ).encode()


def make_consumer(**kwargs) -> tuple[NotificationConsumer, AsyncMock]:
    use_case = AsyncMock()
    consumer = NotificationConsumer(
        broker=MagicMock(),
        exchange=MagicMock(),
        use_case=use_case,
        retry_backoff_seconds=0.0,
        **kwargs,
    )
    return consumer, use_case


# --- parse_notification_command (wire contract) ---


def test_parses_canonical_envelope_and_resolves_user_ids() -> None:
    command = parse_notification_command(headers=make_headers(), body=make_command_body())

    assert command is not None
    assert command.event_id == "evt-1"
    assert command.booking_id == "b-1"
    assert command.trigger_event == "BOOKING_CREATED"
    assert [(r.email, r.role, r.user_id, r.time_zone) for r in command.recipients] == [
        ("org@example.com", "organizer", "uuid-org", "Europe/Moscow"),
        ("cli@example.com", "client", "uuid-cli", None),
    ]
    # D6: template_data merged over original
    assert command.template_context["title"] == "Session"
    assert command.template_context["start_time"] == "2026-06-12T10:00:00Z"


def test_recipient_locale_prefers_producer_value_then_envelope() -> None:
    """recipients[].locale (producer) wins; normalized.participants[].locale is the fallback."""
    command = parse_notification_command(headers=make_headers(), body=make_command_body())

    assert command is not None
    assert [r.locale for r in command.recipients] == ["ru", "en"]


def test_booking_id_falls_back_to_payload() -> None:
    command = parse_notification_command(headers=make_headers(booking_id=None), body=make_command_body())

    assert command is not None
    assert command.booking_id == "b-1"


def test_recipient_without_resolved_user_id_keeps_email() -> None:
    body = json.dumps(
        {
            "original": {
                "booking_id": "b-1",
                "trigger_event": "BOOKING_REMINDER",
                "recipients": [{"email": "cli@example.com", "role": "client"}],
                "template_data": {},
            },
            "normalized": {"participants": []},
        }
    ).encode()

    command = parse_notification_command(headers=make_headers(), body=body)

    assert command is not None
    assert command.recipients[0].email == "cli@example.com"
    assert command.recipients[0].user_id is None


def test_unknown_event_type_returns_none() -> None:
    command = parse_notification_command(headers=make_headers(event_type="getstream.member.added"), body=b"{}")

    assert command is None


# --- ack policy via _consume_message ---


async def test_valid_message_invokes_use_case() -> None:
    consumer, use_case = make_consumer()

    await consumer._consume_message(FakeMessage(make_headers(), make_command_body()))  # noqa: SLF001

    use_case.execute.assert_awaited_once()
    command = use_case.execute.call_args.args[0]
    assert command.event_id == "evt-1"


async def test_unparseable_cloudevent_is_rejected_to_dlq() -> None:
    consumer, use_case = make_consumer()

    with pytest.raises(RejectMessage):
        await consumer._consume_message(FakeMessage({"content-type": "application/json"}, b"not-a-cloudevent"))  # noqa: SLF001

    use_case.execute.assert_not_awaited()


async def test_invalid_payload_is_rejected_to_dlq() -> None:
    consumer, use_case = make_consumer()
    body = json.dumps({"original": {"recipients": "garbage"}, "normalized": {"participants": []}}).encode()

    with pytest.raises(RejectMessage):
        await consumer._consume_message(FakeMessage(make_headers(), body))  # noqa: SLF001

    use_case.execute.assert_not_awaited()


async def test_unknown_event_type_is_acked_without_use_case() -> None:
    consumer, use_case = make_consumer()

    await consumer._consume_message(  # noqa: SLF001
        FakeMessage(make_headers(event_type="booking.created"), make_command_body())
    )

    use_case.execute.assert_not_awaited()


async def test_transient_users_failure_retries_then_nacks_with_requeue() -> None:
    consumer, use_case = make_consumer(transient_retry_attempts=3)
    use_case.execute.side_effect = UsersServiceError("event-users 503")

    with pytest.raises(NackMessage) as exc_info:
        await consumer._consume_message(FakeMessage(make_headers(), make_command_body()))  # noqa: SLF001

    assert use_case.execute.await_count == 3
    assert exc_info.value.extra_options == {"requeue": True}


async def test_transient_db_failure_recovers_within_retry_budget() -> None:
    consumer, use_case = make_consumer(transient_retry_attempts=3)
    use_case.execute.side_effect = [OperationalError("select 1", None, ConnectionRefusedError("db down")), None]

    await consumer._consume_message(FakeMessage(make_headers(), make_command_body()))  # noqa: SLF001

    assert use_case.execute.await_count == 2


async def test_non_transient_use_case_failure_is_rejected_to_dlq() -> None:
    consumer, use_case = make_consumer()
    use_case.execute.side_effect = KeyError("boom")

    with pytest.raises(RejectMessage):
        await consumer._consume_message(FakeMessage(make_headers(), make_command_body()))  # noqa: SLF001

    assert use_case.execute.await_count == 1


# --- FastStream integration: handler signature must survive real decoding ---


async def test_faststream_handler_receives_binary_cloudevent() -> None:
    """Regression for the (body, headers) signature bug: a real binary-mode
    CloudEvent published through FastStream must reach the use case."""
    broker = RabbitBroker()
    exchange = RabbitExchange(name="events", type=ExchangeType.TOPIC, durable=True)
    use_case = AsyncMock()
    consumer = NotificationConsumer(broker=broker, exchange=exchange, use_case=use_case)

    queue, channel = consumer.build_queue_and_channel()
    subscriber = broker.subscriber(queue=queue, exchange=exchange, channel=channel)
    subscriber(consumer._make_handler())  # noqa: SLF001

    async with TestRabbitBroker(broker) as test_broker:
        await test_broker.publish(
            make_command_body(),
            queue=queue,
            exchange=exchange,
            headers=make_headers(),
        )

    use_case.execute.assert_awaited_once()
    command = use_case.execute.call_args.args[0]
    assert command.trigger_event == "BOOKING_CREATED"
    assert len(command.recipients) == 2
