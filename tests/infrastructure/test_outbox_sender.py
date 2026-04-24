from unittest.mock import AsyncMock, MagicMock

import pytest

from event_notifier.adapters.outbox_sender import OutboxSender
from event_notifier.domain.models.notification import ChannelType, DeliveryResult, OutboxRecord


def make_record(**kwargs) -> OutboxRecord:
    defaults = {
        "id": "record-uuid-1",
        "cloud_event_id": "evt-001",
        "booking_id": "booking-abc",
        "user_id": "uuid-user-001",
        "recipient_address": "user@example.com",
        "recipient_role": "volunteer",
        "channel": "email",
        "event_type": "booking.created",
        "template_context": {"volunteer_id": "uuid-user-001"},
        "retry_count": 0,
        "max_retries": 5,
    }
    defaults.update(kwargs)
    return OutboxRecord(**defaults)


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.fetch_pending_outbox = AsyncMock(return_value=[])
    repo.mark_delivered = AsyncMock()
    repo.mark_retry = AsyncMock()
    repo.mark_failed = AsyncMock()
    return repo


@pytest.fixture
def mock_email_channel():
    ch = MagicMock()
    ch.send = AsyncMock(return_value=DeliveryResult(channel=ChannelType.EMAIL, success=True, message_id="job-1"))
    return ch


@pytest.mark.asyncio
async def test_successful_send_marks_delivered(mock_repository, mock_email_channel):
    record = make_record()
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[record])
    sender = OutboxSender(
        repository=mock_repository,
        channels={ChannelType.EMAIL: mock_email_channel},
    )
    await sender.run_once()

    mock_email_channel.send.assert_awaited_once()
    mock_repository.mark_delivered.assert_awaited_once_with("record-uuid-1")
    mock_repository.mark_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_send_marks_retry(mock_repository, mock_email_channel):
    record = make_record(retry_count=0)
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[record])
    mock_email_channel.send = AsyncMock(
        return_value=DeliveryResult(channel=ChannelType.EMAIL, success=False, error="timeout")
    )
    sender = OutboxSender(
        repository=mock_repository,
        channels={ChannelType.EMAIL: mock_email_channel},
    )
    await sender.run_once()

    mock_repository.mark_retry.assert_awaited_once()
    call_kwargs = mock_repository.mark_retry.call_args.kwargs
    assert call_kwargs["record_id"] == "record-uuid-1"
    assert call_kwargs["retry_count"] == 1
    assert call_kwargs["delay_seconds"] == 10  # retry 1: 10 * 1^2 = 10


@pytest.mark.asyncio
async def test_max_retries_exceeded_marks_failed(mock_repository, mock_email_channel):
    record = make_record(retry_count=5, max_retries=5)
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[record])
    mock_email_channel.send = AsyncMock(
        return_value=DeliveryResult(channel=ChannelType.EMAIL, success=False, error="timeout")
    )
    sender = OutboxSender(
        repository=mock_repository,
        channels={ChannelType.EMAIL: mock_email_channel},
    )
    await sender.run_once()

    mock_repository.mark_failed.assert_awaited_once_with("record-uuid-1")
    mock_repository.mark_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_channel_marks_failed(mock_repository):
    record = make_record(channel="push")  # no push adapter registered
    mock_repository.fetch_pending_outbox = AsyncMock(return_value=[record])
    sender = OutboxSender(
        repository=mock_repository,
        channels={ChannelType.EMAIL: MagicMock()},  # only email registered
    )
    await sender.run_once()

    mock_repository.mark_failed.assert_awaited_once_with("record-uuid-1")
