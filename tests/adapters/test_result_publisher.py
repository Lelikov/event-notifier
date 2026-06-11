"""Tests for DeliveryResultPublisher: binary CloudEvent POST to event-receiver."""

import json

import httpx
import pytest
import respx
from httpx import AsyncClient, Response

from event_notifier.adapters.result_publisher import DeliveryResultPublisher
from event_notifier.domain.models.notification import OutboxRecord

ENDPOINT = "http://receiver:8000/event/admin"


def make_record(**kwargs) -> OutboxRecord:
    defaults = {
        "id": "record-uuid-1",
        "cloud_event_id": "evt-001",
        "booking_id": "booking-abc",
        "user_id": "uuid-user-001",
        "recipient_email": "user@example.com",
        "recipient_address": "chat-123",
        "recipient_role": "organizer",
        "channel": "telegram",
        "trigger_event": "BOOKING_CREATED",
        "template_context": {},
        "retry_count": 0,
        "max_retries": 10,
    }
    defaults.update(kwargs)
    return OutboxRecord(**defaults)


@pytest.fixture
async def http_client():
    async with AsyncClient() as client:
        yield client


async def test_publishes_binary_cloudevent_for_email(http_client):
    publisher = DeliveryResultPublisher(http_client=http_client, endpoint_url=ENDPOINT, api_key="admin-key")
    record = make_record(channel="email", recipient_address="user@example.com")

    with respx.mock:
        route = respx.post(ENDPOINT).mock(return_value=Response(202))

        await publisher.publish_delivered(record, "job-42")

    request = route.calls[0].request
    assert request.headers["ce-type"] == "notification.email.message_sent"
    assert request.headers["ce-source"] == "event-notifier"
    assert request.headers["Authorization"] == "admin-key"
    body = json.loads(request.content)
    assert body["email"] == "user@example.com"
    assert body["job_id"] == "job-42"
    assert body["recipient_role"] == "organizer"
    assert body["trigger_event"] == "BOOKING_CREATED"
    assert body["booking_uid"] == "booking-abc"


async def test_telegram_result_uses_recipient_email_not_chat_id(http_client):
    publisher = DeliveryResultPublisher(http_client=http_client, endpoint_url=ENDPOINT)

    with respx.mock:
        route = respx.post(ENDPOINT).mock(return_value=Response(202))

        await publisher.publish_delivered(make_record(channel="telegram"), "777")

    request = route.calls[0].request
    assert request.headers["ce-type"] == "notification.telegram.message_sent"
    body = json.loads(request.content)
    assert body["email"] == "user@example.com"


async def test_event_id_is_deterministic_per_outbox_record(http_client):
    publisher = DeliveryResultPublisher(http_client=http_client, endpoint_url=ENDPOINT)

    with respx.mock:
        route = respx.post(ENDPOINT).mock(return_value=Response(202))

        await publisher.publish_delivered(make_record(), "1")
        await publisher.publish_delivered(make_record(), "1")

    ids = [call.request.headers["ce-id"] for call in route.calls]
    assert ids[0] == ids[1]


async def test_publish_failure_is_swallowed(http_client):
    publisher = DeliveryResultPublisher(http_client=http_client, endpoint_url=ENDPOINT)

    with respx.mock:
        respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("down"))

        await publisher.publish_delivered(make_record(), "1")  # must not raise


async def test_disabled_publisher_is_noop():
    publisher = DeliveryResultPublisher(http_client=None)

    await publisher.publish_delivered(make_record(), "1")  # must not raise
