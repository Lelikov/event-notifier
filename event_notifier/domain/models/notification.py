"""Domain models for notification dispatch — pure dataclasses, no infrastructure deps."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ChannelType(StrEnum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    PUSH = "push"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CommandRecipient:
    """A recipient of a notification command: email+role from the producer, user_id from the receiver."""

    email: str
    role: str
    user_id: str | None = None  # event-users UUID resolved from normalized.participants


@dataclass(frozen=True, slots=True)
class NotificationCommand:
    """Parsed notification.send_requested CloudEvent (the only event this service consumes)."""

    event_id: str  # CloudEvent id (used for idempotency)
    booking_id: str  # ce-bookingid extension or payload booking_id
    trigger_event: str  # TriggerEvent value, e.g. "BOOKING_CREATED"
    recipients: tuple[CommandRecipient, ...]
    template_context: dict[str, Any]  # template_data merged over original (D6)


@dataclass(frozen=True, slots=True)
class UserContacts:
    """Channel contacts of one user as resolved from event-users."""

    email: str | None = None
    telegram_chat_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """A record from the notification_outbox table."""

    id: str  # UUID as string
    cloud_event_id: str
    booking_id: str
    user_id: str  # event-users UUID if resolved, otherwise ""
    recipient_email: str  # always the recipient's email (used in delivery-result events)
    recipient_address: str  # email addr / telegram chat_id / FCM token
    recipient_role: str
    channel: str  # "email" | "telegram" | "push"
    trigger_event: str  # TriggerEvent value selecting the template
    template_context: dict[str, Any]
    retry_count: int
    max_retries: int


@dataclass(frozen=True, slots=True)
class ChannelContact:
    """A resolved channel contact for a recipient."""

    channel: ChannelType
    contact_id: str  # email addr / telegram chat_id / FCM device token
    user_id: str  # event-users UUID if resolved, otherwise ""
    email: str  # recipient email (always known from the command)
    role: str  # "organizer" | "client"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Result of a single channel delivery attempt.

    ``retryable`` only matters when ``success`` is False:
    True for transient provider failures (408/429/5xx/transport),
    False for permanent ones (other 4xx, missing template).
    """

    channel: ChannelType
    success: bool
    message_id: str | None = None
    error: str | None = None
    retryable: bool = True
