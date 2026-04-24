from unittest.mock import AsyncMock, MagicMock

import pytest

from event_notifier.application.use_cases.process_domain_event import ProcessDomainEventUseCase
from event_notifier.domain.models.notification import (
    ChannelContact,
    ChannelType,
    DomainEvent,
    RoutingRule,
)


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.get_routing_rules = AsyncMock(
        return_value=[
            RoutingRule(event_type="booking.created", recipient_field="organizer_id", recipient_role="organizer"),
            RoutingRule(event_type="booking.created", recipient_field="client_id", recipient_role="client"),
        ]
    )
    repo.is_processed = AsyncMock(return_value=False)
    repo.write_outbox_atomically = AsyncMock()
    return repo


@pytest.fixture
def mock_users_client():
    client = MagicMock()
    client.get_contacts_by_id = AsyncMock(
        side_effect=lambda *, user_id, role: [
            ChannelContact(channel=ChannelType.EMAIL, contact_id=f"{user_id}@example.com", user_id=user_id, role=role),
            ChannelContact(channel=ChannelType.TELEGRAM, contact_id="chat-123", user_id=user_id, role=role),
        ]
    )
    return client


@pytest.fixture
def event():
    return DomainEvent(
        event_id="evt-001",
        event_type="booking.created",
        source="booking",
        booking_id="booking-abc",
        data={"organizer_id": "uuid-org-001", "client_id": "uuid-cli-001"},
    )


@pytest.mark.asyncio
async def test_writes_outbox_records_for_all_contacts(mock_repository, mock_users_client, event):
    use_case = ProcessDomainEventUseCase(repository=mock_repository, users_client=mock_users_client)
    await use_case.execute(event)

    mock_repository.write_outbox_atomically.assert_awaited_once()
    _, call_kwargs = mock_repository.write_outbox_atomically.call_args
    records = call_kwargs["records"]
    # 2 recipients * 2 channels each = 4 outbox records
    assert len(records) == 4
    channels = {r["channel"] for r in records}
    assert "email" in channels
    assert "telegram" in channels


@pytest.mark.asyncio
async def test_skips_already_processed_events(mock_repository, mock_users_client, event):
    mock_repository.is_processed = AsyncMock(return_value=True)
    use_case = ProcessDomainEventUseCase(repository=mock_repository, users_client=mock_users_client)
    await use_case.execute(event)

    mock_repository.write_outbox_atomically.assert_not_awaited()
    mock_users_client.get_contacts_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_event_with_no_routing_rules(mock_repository, mock_users_client, event):
    mock_repository.get_routing_rules = AsyncMock(return_value=[])
    use_case = ProcessDomainEventUseCase(repository=mock_repository, users_client=mock_users_client)
    await use_case.execute(event)

    mock_repository.write_outbox_atomically.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotency_key_format(mock_repository, mock_users_client, event):
    use_case = ProcessDomainEventUseCase(repository=mock_repository, users_client=mock_users_client)
    await use_case.execute(event)

    _, call_kwargs = mock_repository.write_outbox_atomically.call_args
    records = call_kwargs["records"]
    keys = [r["idempotency_key"] for r in records]
    # format: "{event_id}:{user_id}:{channel}"
    assert any("evt-001:uuid-org-001:email" == k for k in keys)
    assert any("evt-001:uuid-org-001:telegram" == k for k in keys)
