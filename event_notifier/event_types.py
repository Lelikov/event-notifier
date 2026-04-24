"""Event type constants for event-notifier."""

from event_schemas.types import EventType, TriggerEvent

NOTIFIER_SOURCE = "event-notifier"

# Mapping from CloudEvent type to trigger_event string used by channel adapters
DOMAIN_EVENT_TO_TRIGGER: dict[str, str] = {
    EventType.BOOKING_CREATED: TriggerEvent.BOOKING_CREATED,
    EventType.BOOKING_CANCELLED: TriggerEvent.BOOKING_CANCELLED,
    EventType.BOOKING_RESCHEDULED: TriggerEvent.BOOKING_RESCHEDULED,
    EventType.BOOKING_REASSIGNED: TriggerEvent.BOOKING_REASSIGNED,
    EventType.BOOKING_REMINDER_SENT: TriggerEvent.BOOKING_REMINDER,
}
