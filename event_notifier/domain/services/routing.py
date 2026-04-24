"""Pure routing functions: extract recipients from domain event using routing rules."""

from event_notifier.domain.models.notification import RoutingRule


def extract_field_value(data: dict, field_path: str) -> str | None:
    """Extract a string value from a nested dict using dot-notation path.

    Example: extract_field_value({"volunteer_id": "uuid-001"}, "volunteer_id") == "uuid-001"
    Example: extract_field_value({"user": {"id": "uuid-001"}}, "user.id") == "uuid-001"
    Returns None if the path doesn't exist or the value is not a string.
    """
    parts = field_path.split(".")
    current: object = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current if isinstance(current, str) else None


def apply_routing_rules(
    *,
    event_type: str,
    event_data: dict,
    routing_rules: list[RoutingRule],
) -> list[tuple[str, str]]:
    """Return list of (user_id, role) pairs for the given event type.

    Extracts UUID values from event_data using routing_rules.recipient_field.
    Only includes rules matching event_type where the field resolves to a non-empty string.
    """
    recipients: list[tuple[str, str]] = []
    for rule in routing_rules:
        if rule.event_type != event_type:
            continue
        user_id = extract_field_value(event_data, rule.recipient_field)
        if user_id:
            recipients.append((user_id, rule.recipient_role))
    return recipients
