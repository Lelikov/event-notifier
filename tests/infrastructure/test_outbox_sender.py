"""Tests for OutboxSender: trigger resolution, retry classification, reaper, result events."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from event_notifier.adapters.outbox_sender import OutboxSender, _retry_delay_seconds
from event_notifier.domain.models.notification import ChannelType, DeliveryResult, OutboxRecord


def make_record(**kwargs) -> OutboxRecord:
    defaults = {
        "id": "record-uuid-1",
        "cloud_event_id": "evt-001",
        "booking_id": "booking-abc",
        "user_id": "uuid-user-001",
        "recipient_email": "user@example.com",
        "recipient_address": "user@example.com",
        "recipient_role": "organizer",
        "channel": "email",
        "trigger_event": "BOOKING_CREATED",
        "template_context": {"start_time": "2026-06-12T10:00:00Z"},
        "retry_count": 0,
        "max_retries": 10,
    }
    defaults.update(kwargs)
    return OutboxRecord(**defaults)


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.fetch_pending_outbox = AsyncMock(return_value=[])
    repo.reap_stale_processing = AsyncMock(return_value=0)
    repo.mark_delivered = AsyncMock()
    repo.mark_retry = AsyncMock()
    repo.mark_failed = AsyncMock()
    return repo


@pytest.fixture
def mock_email_channel():
    ch = MagicMock()
    ch.send = AsyncMock(return_value=DeliveryResult(channel=ChannelType.EMAIL, success=True, message_id="job-1"))
    return ch


@pytest.fixture
def mock_result_publisher():
    pub = MagicMock()
    pub.publish_delivered = AsyncMock()
    return pub


def make_sender(repo, channel, publisher) -> OutboxSender:
    return OutboxSender(
        repository=repo,
        channels={ChannelType.EMAIL: channel},
        result_publisher=publisher,
    )


async def test_successful_send_marks_delivered_and_publishes_result(
    mock_repository, mock_email_channel, mock_result_publisher
):
    record = make_record()
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[record])

    await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

    send_kwargs = mock_email_channel.send.call_args.kwargs
    assert send_kwargs["trigger_event"].value == "BOOKING_CREATED"  # resolved from the RECORD, not event_type
    mock_repository.mark_delivered.assert_awaited_once_with("record-uuid-1")
    mock_result_publisher.publish_delivered.assert_awaited_once_with(record, "job-1")


async def test_retryable_failure_schedules_backoff(mock_repository, mock_email_channel, mock_result_publisher):
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record(retry_count=2)])
    mock_email_channel.send = AsyncMock(
        return_value=DeliveryResult(channel=ChannelType.EMAIL, success=False, error="503", retryable=True)
    )

    await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

    kwargs = mock_repository.mark_retry.call_args.kwargs
    assert kwargs["retry_count"] == 3
    assert kwargs["delay_seconds"] == 40  # 10 * 2**2
    assert kwargs["error"] == "503"
    mock_result_publisher.publish_delivered.assert_not_awaited()


async def test_permanent_failure_fails_immediately_without_retries(
    mock_repository, mock_email_channel, mock_result_publisher
):
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record(retry_count=0)])
    mock_email_channel.send = AsyncMock(
        return_value=DeliveryResult(channel=ChannelType.EMAIL, success=False, error="400 bad", retryable=False)
    )

    await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

    mock_repository.mark_failed.assert_awaited_once_with("record-uuid-1", error="400 bad")
    mock_repository.mark_retry.assert_not_awaited()


async def test_retries_exhausted_marks_failed(mock_repository, mock_email_channel, mock_result_publisher):
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record(retry_count=10, max_retries=10)])
    mock_email_channel.send = AsyncMock(
        return_value=DeliveryResult(channel=ChannelType.EMAIL, success=False, error="timeout", retryable=True)
    )

    await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

    mock_repository.mark_failed.assert_awaited_once()
    mock_repository.mark_retry.assert_not_awaited()


async def test_unexpected_channel_exception_is_retried(mock_repository, mock_email_channel, mock_result_publisher):
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record()])
    mock_email_channel.send = AsyncMock(side_effect=RuntimeError("boom"))

    await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

    mock_repository.mark_retry.assert_awaited_once()


async def test_unknown_trigger_event_marks_failed(mock_repository, mock_email_channel, mock_result_publisher):
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record(trigger_event="bogus")])

    await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

    mock_email_channel.send.assert_not_awaited()
    mock_repository.mark_failed.assert_awaited_once()


async def test_unknown_channel_marks_failed(mock_repository, mock_email_channel, mock_result_publisher):
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record(channel="pigeon")])

    await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

    mock_repository.mark_failed.assert_awaited_once()


async def test_unregistered_channel_marks_failed(mock_repository, mock_email_channel, mock_result_publisher):
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record(channel="push")])

    await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

    mock_repository.mark_failed.assert_awaited_once()


def test_retry_delay_is_capped_exponential():
    assert _retry_delay_seconds(1) == 10
    assert _retry_delay_seconds(2) == 20
    assert _retry_delay_seconds(5) == 160
    assert _retry_delay_seconds(12) == 1800  # capped at 30 min


def test_idle_backoff_grows_and_resets(mock_repository, mock_email_channel, mock_result_publisher):
    sender = make_sender(mock_repository, mock_email_channel, mock_result_publisher)

    assert sender._next_idle_interval(processed=0) == 2.0  # noqa: SLF001
    sender._idle_interval = 16.0  # noqa: SLF001
    assert sender._next_idle_interval(processed=0) == 30.0  # noqa: SLF001 — capped
    assert sender._next_idle_interval(processed=3) == 1.0  # noqa: SLF001 — reset
