"""Tests for /metrics exposition, consumer RED, delivery counters and outbox gauges."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from faststream.exceptions import NackMessage, RejectMessage
from prometheus_client import REGISTRY

from event_notifier import main
from event_notifier.domain.models.notification import ChannelType, DeliveryResult, OutboxStats
from event_notifier.interfaces.users_client import UsersServiceError
from tests.adapters.test_consumer import FakeMessage, make_command_body, make_consumer, make_headers
from tests.infrastructure.test_outbox_sender import make_record, make_sender

QUEUE = "events.notification.commands"
EVENT_TYPE = "notification.send_requested"


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


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


class TestMetricsEndpoint:
    def test_metrics_route_registered(self) -> None:
        paths = {route.path for route in main.app.routes}

        assert "/metrics" in paths

    async def test_metrics_returns_prometheus_exposition(self) -> None:
        response = await main.metrics_endpoint()

        assert response.status_code == 200  # noqa: PLR2004
        assert response.media_type.startswith("text/plain")
        assert b"messages_processed_total" in response.body
        assert b"notifier_outbox_depth" in response.body


class TestConsumerRedMetrics:
    async def test_processed_message_counts_ok_and_duration(self) -> None:
        consumer, _use_case = make_consumer()
        labels = {"queue": QUEUE, "event_type": EVENT_TYPE, "outcome": "ok"}
        before = _sample("messages_processed_total", labels)
        duration_before = _sample("message_processing_seconds_count", {"queue": QUEUE})

        await consumer._consume_message(FakeMessage(make_headers(), make_command_body()))  # noqa: SLF001

        assert _sample("messages_processed_total", labels) == before + 1
        assert _sample("message_processing_seconds_count", {"queue": QUEUE}) == duration_before + 1

    async def test_transient_exhaustion_counts_retried(self) -> None:
        consumer, use_case = make_consumer()
        use_case.execute.side_effect = UsersServiceError("event-users down")
        labels = {"queue": QUEUE, "event_type": EVENT_TYPE, "outcome": "retried"}
        before = _sample("messages_processed_total", labels)

        with pytest.raises(NackMessage):
            await consumer._consume_message(FakeMessage(make_headers(), make_command_body()))  # noqa: SLF001

        assert _sample("messages_processed_total", labels) == before + 1

    async def test_poison_message_counts_rejected_unknown(self) -> None:
        consumer, _use_case = make_consumer()
        labels = {"queue": QUEUE, "event_type": "unknown", "outcome": "rejected"}
        before = _sample("messages_processed_total", labels)

        with pytest.raises(RejectMessage):
            await consumer._consume_message(  # noqa: SLF001
                FakeMessage({"content-type": "application/json"}, b"not-a-cloudevent"),
            )

        assert _sample("messages_processed_total", labels) == before + 1


class TestDeliveryCounters:
    async def test_delivered(self, mock_repository, mock_email_channel, mock_result_publisher) -> None:
        mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record()])
        labels = {"channel": "email", "trigger": "BOOKING_CREATED", "outcome": "delivered"}
        before = _sample("notifier_deliveries_total", labels)

        await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

        assert _sample("notifier_deliveries_total", labels) == before + 1

    async def test_retryable_failure_counts_retried(
        self,
        mock_repository,
        mock_email_channel,
        mock_result_publisher,
    ) -> None:
        mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record()])
        mock_email_channel.send = AsyncMock(
            return_value=DeliveryResult(channel=ChannelType.EMAIL, success=False, retryable=True, error="503"),
        )
        labels = {"channel": "email", "trigger": "BOOKING_CREATED", "outcome": "retried"}
        before = _sample("notifier_deliveries_total", labels)

        await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

        assert _sample("notifier_deliveries_total", labels) == before + 1

    async def test_permanent_failure_counts_failed(
        self,
        mock_repository,
        mock_email_channel,
        mock_result_publisher,
    ) -> None:
        mock_repository.fetch_pending_outbox = AsyncMock(return_value=[make_record()])
        mock_email_channel.send = AsyncMock(
            return_value=DeliveryResult(channel=ChannelType.EMAIL, success=False, retryable=False, error="400"),
        )
        labels = {"channel": "email", "trigger": "BOOKING_CREATED", "outcome": "failed"}
        before = _sample("notifier_deliveries_total", labels)

        await make_sender(mock_repository, mock_email_channel, mock_result_publisher).run_once()

        assert _sample("notifier_deliveries_total", labels) == before + 1


class TestOutboxGauges:
    async def test_refresh_sets_depth_and_oldest_pending_age(
        self,
        mock_repository,
        mock_email_channel,
        mock_result_publisher,
    ) -> None:
        mock_repository.outbox_stats = AsyncMock(
            return_value=OutboxStats(
                counts_by_status={"pending": 7, "failed": 2},
                oldest_pending_age_seconds=42.5,
            ),
        )
        sender = make_sender(mock_repository, mock_email_channel, mock_result_publisher)

        await sender.refresh_outbox_gauges()

        assert _sample("notifier_outbox_depth", {"status": "pending"}) == 7.0  # noqa: PLR2004
        assert _sample("notifier_outbox_depth", {"status": "failed"}) == 2.0  # noqa: PLR2004
        assert _sample("notifier_outbox_depth", {"status": "delivered"}) == 0.0
        assert _sample("notifier_outbox_depth", {"status": "processing"}) == 0.0
        assert _sample("notifier_outbox_oldest_pending_age_seconds") == 42.5  # noqa: PLR2004


class TestOutboxStatsQuery:
    async def test_repository_outbox_stats_maps_rows(self) -> None:
        from event_notifier.db.repository import NotificationRepository

        sql = MagicMock()
        sql.fetch_all = AsyncMock(
            return_value=[
                {"status": "pending", "count": 3, "oldest_age_seconds": 17.2},
                {"status": "delivered", "count": 11, "oldest_age_seconds": 900.0},
            ],
        )
        repo = NotificationRepository(sql)

        stats = await repo.outbox_stats()

        assert stats.counts_by_status == {"pending": 3, "delivered": 11}
        assert stats.oldest_pending_age_seconds == 17.2  # noqa: PLR2004
