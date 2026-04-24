from event_notifier.domain.models.notification import (
    ChannelContact,
    ChannelType,
    DeliveryResult,
)


def test_channel_contact_email():
    c = ChannelContact(
        channel=ChannelType.EMAIL,
        contact_id="user@example.com",
        user_id="uuid-user-001",
        role="organizer",
    )
    assert c.channel == ChannelType.EMAIL
    assert c.contact_id == "user@example.com"


def test_delivery_result_success():
    r = DeliveryResult(channel=ChannelType.TELEGRAM, success=True, message_id="tg-123")
    assert r.success is True
    assert r.error is None


def test_delivery_result_failure():
    r = DeliveryResult(channel=ChannelType.PUSH, success=False, error="Device not registered")
    assert r.success is False
    assert r.error == "Device not registered"
