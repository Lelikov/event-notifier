"""Tests for ProcessNotificationCommandUseCase contact resolution and outbox writes."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.application.use_cases.process_notification_command import ProcessNotificationCommandUseCase
from event_notifier.domain.models.notification import CommandRecipient, NotificationCommand, UserContacts
from event_notifier.interfaces.users_client import UsersServiceError


class _FakeSql:
    """Fake SQL executor that returns configurable rows for fetch_all."""

    def __init__(self, rows=None):
        self._rows = rows or []

    async def fetch_all(self, query, values):
        return self._rows

    async def fetch_one(self, query, values):
        return None

    async def execute(self, query, values):
        pass

    def transaction(self):
        raise NotImplementedError


def _all_enabled_bindings() -> BindingsProvider:
    """Returns a BindingsProvider that reports all channels as enabled for all triggers."""
    rows = []
    for trigger in [
        "BOOKING_CREATED", "BOOKING_RESCHEDULED", "BOOKING_REASSIGNED",
        "BOOKING_CANCELLED", "BOOKING_REMINDER", "BOOKING_REJECTED", "BOOKING_REJECTED_BLACKLISTED",
    ]:
        rows.append({
            "trigger_event": trigger, "channel": "email", "enabled": True,
            "unisender_template_id": "tmpl-id", "telegram_body": None,
        })
        rows.append({
            "trigger_event": trigger, "channel": "telegram", "enabled": True,
            "unisender_template_id": None, "telegram_body": "Hi!",
        })
    return BindingsProvider(sql=_FakeSql(rows), ttl_seconds=60)


@pytest.fixture
def mock_repository():
    repo = MagicMock()
    repo.is_processed = AsyncMock(return_value=False)
    repo.write_outbox_atomically = AsyncMock(return_value=True)
    return repo


@pytest.fixture
def mock_users_client():
    client = MagicMock()
    client.get_user_contacts = AsyncMock(
        return_value=UserContacts(email="org@example.com", telegram_chat_id="chat-123")
    )
    return client


def make_command(
    recipients: tuple[CommandRecipient, ...] | None = None,
    trigger_event: str = "BOOKING_CREATED",
) -> NotificationCommand:
    if recipients is None:
        recipients = (
            CommandRecipient(email="org@example.com", role="organizer", user_id="uuid-org"),
            CommandRecipient(email="cli@example.com", role="client", user_id="uuid-cli"),
        )
    return NotificationCommand(
        event_id="evt-001",
        booking_id="booking-abc",
        trigger_event=trigger_event,
        recipients=recipients,
        template_context={"start_time": "2026-06-12T10:00:00Z"},
    )


def make_use_case(repo, users, bindings=None) -> ProcessNotificationCommandUseCase:
    return ProcessNotificationCommandUseCase(
        repository=repo,
        users_client=users,
        bindings=bindings or _all_enabled_bindings(),
    )


async def test_writes_email_and_telegram_records(mock_repository, mock_users_client):
    await make_use_case(mock_repository, mock_users_client).execute(make_command())

    mock_repository.write_outbox_atomically.assert_awaited_once()
    records = mock_repository.write_outbox_atomically.call_args.kwargs["records"]
    # 2 recipients x (email + telegram) = 4
    assert len(records) == 4
    assert {r["channel"] for r in records} == {"email", "telegram"}
    assert all(r["trigger_event"] == "BOOKING_CREATED" for r in records)
    assert all(r["booking_id"] == "booking-abc" for r in records)


async def test_idempotency_key_is_email_based(mock_repository, mock_users_client):
    await make_use_case(mock_repository, mock_users_client).execute(make_command())

    keys = {r["idempotency_key"] for r in mock_repository.write_outbox_atomically.call_args.kwargs["records"]}
    assert "evt-001:org@example.com:email" in keys
    assert "evt-001:org@example.com:telegram" in keys
    assert "evt-001:cli@example.com:email" in keys


async def test_recipient_without_user_id_gets_email_only(mock_repository, mock_users_client):
    command = make_command(recipients=(CommandRecipient(email="cli@example.com", role="client", user_id=None),))

    await make_use_case(mock_repository, mock_users_client).execute(command)

    mock_users_client.get_user_contacts.assert_not_awaited()
    records = mock_repository.write_outbox_atomically.call_args.kwargs["records"]
    assert [r["channel"] for r in records] == ["email"]
    assert records[0]["recipient_address"] == "cli@example.com"
    assert records[0]["recipient_email"] == "cli@example.com"


async def test_user_404_degrades_to_email_only(mock_repository, mock_users_client):
    mock_users_client.get_user_contacts = AsyncMock(return_value=None)
    command = make_command(recipients=(CommandRecipient(email="org@example.com", role="organizer", user_id="u-1"),))

    await make_use_case(mock_repository, mock_users_client).execute(command)

    records = mock_repository.write_outbox_atomically.call_args.kwargs["records"]
    assert [r["channel"] for r in records] == ["email"]


async def test_user_without_telegram_gets_email_only(mock_repository, mock_users_client):
    mock_users_client.get_user_contacts = AsyncMock(return_value=UserContacts(email="org@example.com"))
    command = make_command(recipients=(CommandRecipient(email="org@example.com", role="organizer", user_id="u-1"),))

    await make_use_case(mock_repository, mock_users_client).execute(command)

    records = mock_repository.write_outbox_atomically.call_args.kwargs["records"]
    assert [r["channel"] for r in records] == ["email"]


async def test_transport_error_propagates_for_nack(mock_repository, mock_users_client):
    """event-users outage must NOT be ACKed: the exception bubbles to the consumer."""
    mock_users_client.get_user_contacts = AsyncMock(side_effect=UsersServiceError("503"))

    with pytest.raises(UsersServiceError):
        await make_use_case(mock_repository, mock_users_client).execute(make_command())

    mock_repository.write_outbox_atomically.assert_not_awaited()


async def test_skips_already_processed_event(mock_repository, mock_users_client):
    mock_repository.is_processed = AsyncMock(return_value=True)

    await make_use_case(mock_repository, mock_users_client).execute(make_command())

    mock_repository.write_outbox_atomically.assert_not_awaited()
    mock_users_client.get_user_contacts.assert_not_awaited()


async def test_no_recipients_marks_processed_with_no_contacts_outcome(mock_repository, mock_users_client):
    command = make_command(recipients=())

    await make_use_case(mock_repository, mock_users_client).execute(command)

    # Event is claimed (no redelivery loop) but with zero records — explicit no_contacts outcome.
    mock_repository.write_outbox_atomically.assert_awaited_once_with(cloud_event_id="evt-001", records=[])


async def test_concurrent_duplicate_is_tolerated(mock_repository, mock_users_client):
    mock_repository.write_outbox_atomically = AsyncMock(return_value=False)

    await make_use_case(mock_repository, mock_users_client).execute(make_command())

    mock_repository.write_outbox_atomically.assert_awaited_once()


async def test_recipient_locale_lands_in_template_context(mock_repository, mock_users_client):
    recipients = (
        CommandRecipient(email="org@example.com", role="organizer", user_id="uuid-org", locale="ru"),
        CommandRecipient(email="cli@example.com", role="client", user_id="uuid-cli", locale=None),
    )
    await make_use_case(mock_repository, mock_users_client).execute(make_command(recipients))

    records = mock_repository.write_outbox_atomically.call_args.kwargs["records"]
    by_email = {r["recipient_email"]: r["template_context"] for r in records}
    assert by_email["org@example.com"]["locale"] == "ru"
    assert "locale" not in by_email["cli@example.com"]


async def test_template_context_is_localized_per_recipient(mock_repository, mock_users_client):
    recipients = (
        CommandRecipient(email="org@example.com", role="organizer", user_id="uuid-org", time_zone="Europe/Moscow"),
        CommandRecipient(email="cli@example.com", role="client", user_id="uuid-cli", time_zone=None),
    )
    await make_use_case(mock_repository, mock_users_client).execute(make_command(recipients))

    records = mock_repository.write_outbox_atomically.call_args.kwargs["records"]
    by_email = {r["recipient_email"]: r["template_context"] for r in records}
    assert by_email["org@example.com"]["start_time_local"] == "12.06.2026 13:00"  # UTC+3
    assert by_email["org@example.com"]["time_zone"] == "Europe/Moscow"
    assert by_email["org@example.com"]["start_time"] == "2026-06-12T10:00:00Z"  # original untouched
    assert "start_time_local" not in by_email["cli@example.com"]


async def test_blacklisted_rejection_trigger_reaches_the_outbox(mock_repository, mock_users_client):
    """BOOKING_REJECTED_BLACKLISTED commands take the same outbox path as any other trigger."""
    command = make_command(
        recipients=(CommandRecipient(email="cli@example.com", role="client", user_id=None),),
        trigger_event="BOOKING_REJECTED_BLACKLISTED",
    )

    await make_use_case(mock_repository, mock_users_client).execute(command)

    mock_repository.write_outbox_atomically.assert_awaited_once()
    records = mock_repository.write_outbox_atomically.call_args.kwargs["records"]
    assert [r["channel"] for r in records] == ["email"]
    assert all(r["trigger_event"] == "BOOKING_REJECTED_BLACKLISTED" for r in records)
    assert records[0]["idempotency_key"] == "evt-001:cli@example.com:email"


async def test_email_disabled_binding_skips_email_channel(mock_repository, mock_users_client):
    """When the binding disables email for a trigger, no email contacts are resolved."""
    rows = [
        {"trigger_event": "BOOKING_CREATED", "channel": "email", "enabled": False,
         "unisender_template_id": None, "telegram_body": None},
        {"trigger_event": "BOOKING_CREATED", "channel": "telegram", "enabled": False,
         "unisender_template_id": None, "telegram_body": None},
    ]
    bindings = BindingsProvider(sql=_FakeSql(rows), ttl_seconds=60)
    command = make_command(
        recipients=(CommandRecipient(email="cli@example.com", role="client", user_id=None),),
    )

    await make_use_case(mock_repository, mock_users_client, bindings=bindings).execute(command)

    # No contacts → write_outbox_atomically is called with empty records
    mock_repository.write_outbox_atomically.assert_awaited_once_with(cloud_event_id="evt-001", records=[])


async def test_telegram_disabled_binding_skips_telegram_channel(mock_repository, mock_users_client):
    """When the telegram binding is disabled, only email contact is resolved."""
    rows = [
        {"trigger_event": "BOOKING_CREATED", "channel": "email", "enabled": True,
         "unisender_template_id": "tmpl-id", "telegram_body": None},
        {"trigger_event": "BOOKING_CREATED", "channel": "telegram", "enabled": False,
         "unisender_template_id": None, "telegram_body": None},
    ]
    bindings = BindingsProvider(sql=_FakeSql(rows), ttl_seconds=60)
    command = make_command(
        recipients=(CommandRecipient(email="cli@example.com", role="client", user_id="u-1"),),
    )

    await make_use_case(mock_repository, mock_users_client, bindings=bindings).execute(command)

    records = mock_repository.write_outbox_atomically.call_args.kwargs["records"]
    assert [r["channel"] for r in records] == ["email"]
