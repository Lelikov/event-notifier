from event_notifier.domain.models.notification import RoutingRule
from event_notifier.domain.services.routing import apply_routing_rules, extract_field_value


def test_extract_top_level_field():
    data = {"organizer_id": "uuid-org-001"}
    assert extract_field_value(data, "organizer_id") == "uuid-org-001"


def test_extract_nested_field():
    data = {"user": {"id": "uuid-org-001"}}
    assert extract_field_value(data, "user.id") == "uuid-org-001"


def test_extract_missing_field_returns_none():
    data = {"user": {"name": "Bob"}}
    assert extract_field_value(data, "user.id") is None


def test_extract_non_string_returns_none():
    data = {"count": 42}
    assert extract_field_value(data, "count") is None


def test_apply_routing_rules_booking_created():
    rules = [
        RoutingRule(event_type="booking.created", recipient_field="organizer_id", recipient_role="organizer"),
        RoutingRule(event_type="booking.created", recipient_field="client_id", recipient_role="client"),
    ]
    data = {"organizer_id": "uuid-org-001", "client_id": "uuid-cli-001"}
    recipients = apply_routing_rules(event_type="booking.created", event_data=data, routing_rules=rules)
    assert len(recipients) == 2
    assert ("uuid-org-001", "organizer") in recipients
    assert ("uuid-cli-001", "client") in recipients


def test_apply_routing_rules_skips_missing_fields():
    rules = [
        RoutingRule(event_type="booking.cancelled", recipient_field="organizer_id", recipient_role="organizer"),
        RoutingRule(event_type="booking.cancelled", recipient_field="client_id", recipient_role="client"),
    ]
    # client_id absent — should be skipped
    data = {"organizer_id": "uuid-org-001", "cancellation_reason": "test"}
    recipients = apply_routing_rules(event_type="booking.cancelled", event_data=data, routing_rules=rules)
    assert recipients == [("uuid-org-001", "organizer")]
