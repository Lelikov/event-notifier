"""Domain models for notification dispatch — pure dataclasses, no infrastructure deps."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ChannelType(StrEnum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    PUSH = "push"


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Parsed incoming CloudEvent (domain event from booking service)."""

    event_id: str  # CloudEvent id (used for idempotency)
    event_type: str  # "booking.created" etc.
    source: str  # ce-source
    booking_id: str  # ce-booking_id attribute
    data: dict[str, Any]  # parsed JSON payload


@dataclass(frozen=True, slots=True)
class RoutingRule:
    """A single routing rule from the DB."""

    event_type: str
    recipient_field: str  # dot-notation path into DomainEvent.data → extracts UUID string
    recipient_role: str  # "volunteer" | "client"


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """A record from the notification_outbox table."""

    id: str  # UUID as string
    cloud_event_id: str
    booking_id: str
    user_id: str  # UUID of the recipient user
    recipient_address: str  # email / telegram chat_id / FCM token
    recipient_role: str
    channel: str  # "email" | "telegram" | "push"
    event_type: str
    template_context: dict[str, Any]
    retry_count: int
    max_retries: int


@dataclass(frozen=True, slots=True)
class ChannelContact:
    """A resolved channel contact for a recipient."""

    channel: ChannelType
    contact_id: str  # email addr / telegram chat_id / FCM device token
    user_id: str  # UUID of the user (from booking event data)
    role: str  # "volunteer" | "client"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Result of a single channel delivery attempt."""

    channel: ChannelType
    success: bool
    message_id: str | None = None
    error: str | None = None
