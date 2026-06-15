"""Dishka DI container for event-notifier."""

from collections.abc import AsyncGenerator

import httpx
import structlog
from dishka import Provider, Scope, provide
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.adapters.consumer import NotificationConsumer
from event_notifier.adapters.outbox_sender import OutboxSender
from event_notifier.adapters.result_publisher import DeliveryResultPublisher
from event_notifier.adapters.sql import SqlExecutor
from event_notifier.adapters.unisender_templates import UnisenderTemplateList
from event_notifier.application.use_cases.process_notification_command import ProcessNotificationCommandUseCase
from event_notifier.config import Settings
from event_notifier.db.repository import NotificationRepository
from event_notifier.domain.models.notification import ChannelType
from event_notifier.infrastructure.channels.email import EmailChannel
from event_notifier.infrastructure.channels.telegram import TelegramChannel
from event_notifier.infrastructure.users_client import UsersClient
from event_notifier.interfaces.channels import INotificationChannel
from event_notifier.interfaces.sql import ISqlExecutor
from event_notifier.telemetry import rabbit_telemetry_middlewares

logger = structlog.get_logger(__name__)

# All four parameters must be set explicitly: httpx.Timeout raises ValueError otherwise.
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


class AppProvider(Provider):
    @provide(scope=Scope.APP)
    def provide_settings(self) -> Settings:
        return Settings()

    @provide(scope=Scope.APP)
    async def provide_sessionmaker(self, settings: Settings) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
        engine = create_async_engine(
            str(settings.database_url),
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
        yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        await engine.dispose()

    @provide(scope=Scope.APP)
    def provide_sql_executor(self, sessionmaker: async_sessionmaker[AsyncSession]) -> ISqlExecutor:
        # Safe to share across tasks: every operation opens its own session.
        return SqlExecutor(sessionmaker)

    @provide(scope=Scope.APP)
    def provide_repository(self, sql: ISqlExecutor) -> NotificationRepository:
        return NotificationRepository(sql=sql)

    @provide(scope=Scope.APP)
    def provide_bindings_provider(self, sql: ISqlExecutor, settings: Settings) -> BindingsProvider:
        return BindingsProvider(sql=sql, ttl_seconds=settings.bindings_cache_ttl_seconds)

    @provide(scope=Scope.APP)
    async def provide_unisender_template_list(self, settings: Settings) -> AsyncGenerator[UnisenderTemplateList]:
        async with AsyncClient(
            timeout=_HTTP_TIMEOUT,
            headers={"X-API-KEY": settings.unisender_api_key},
        ) as client:
            yield UnisenderTemplateList(
                http_client=client,
                base_url=settings.unisender_base_url,
                api_key=settings.unisender_api_key,
                ttl_seconds=settings.unisender_template_list_ttl_seconds,
            )

    @provide(scope=Scope.APP)
    def provide_exchange(self, settings: Settings) -> RabbitExchange:
        return RabbitExchange(name=settings.rabbit_exchange, type=ExchangeType.TOPIC, durable=True)

    @provide(scope=Scope.APP)
    def provide_broker(self, settings: Settings) -> RabbitBroker:
        return RabbitBroker(
            str(settings.rabbit_url),
            graceful_timeout=settings.graceful_timeout,
            middlewares=[*rabbit_telemetry_middlewares()],
        )

    @provide(scope=Scope.APP)
    async def provide_users_client(self, settings: Settings) -> AsyncGenerator[UsersClient]:
        async with AsyncClient(base_url=str(settings.event_users_url), timeout=_HTTP_TIMEOUT) as client:
            yield UsersClient(http_client=client, api_token=settings.event_users_token)

    @provide(scope=Scope.APP)
    async def provide_email_channel(
        self, settings: Settings, bindings: BindingsProvider
    ) -> AsyncGenerator[EmailChannel]:
        async with AsyncClient(
            base_url=settings.unisender_base_url,
            timeout=_HTTP_TIMEOUT,
            headers={"X-API-KEY": settings.unisender_api_key},
        ) as client:
            yield EmailChannel(
                http_client=client,
                bindings=bindings,
                from_email=settings.unisender_from_email,
                from_name=settings.unisender_from_name,
            )

    @provide(scope=Scope.APP)
    async def provide_telegram_channel(
        self, settings: Settings, bindings: BindingsProvider
    ) -> AsyncGenerator[TelegramChannel]:
        async with AsyncClient(base_url=settings.telegram_base_url, timeout=_HTTP_TIMEOUT) as client:
            yield TelegramChannel(
                http_client=client,
                bot_token=settings.telegram_bot_token,
                bindings=bindings,
            )

    @provide(scope=Scope.APP)
    async def provide_result_publisher(self, settings: Settings) -> AsyncGenerator[DeliveryResultPublisher]:
        if settings.events_endpoint_url is None:
            yield DeliveryResultPublisher(http_client=None)
            return
        async with AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            yield DeliveryResultPublisher(
                http_client=client,
                endpoint_url=str(settings.events_endpoint_url),
                api_key=settings.events_api_key,
            )

    @provide(scope=Scope.APP)
    def provide_use_case(
        self,
        repository: NotificationRepository,
        users_client: UsersClient,
        bindings: BindingsProvider,
    ) -> ProcessNotificationCommandUseCase:
        return ProcessNotificationCommandUseCase(
            repository=repository,
            users_client=users_client,
            bindings=bindings,
        )

    @provide(scope=Scope.APP)
    def provide_outbox_sender(
        self,
        repository: NotificationRepository,
        email_channel: EmailChannel,
        telegram_channel: TelegramChannel,
        result_publisher: DeliveryResultPublisher,
    ) -> OutboxSender:
        channels: dict[ChannelType, INotificationChannel] = {
            ChannelType.EMAIL: email_channel,
            ChannelType.TELEGRAM: telegram_channel,
            # ChannelType.PUSH: push_channel  — включить после настройки FCM
        }
        return OutboxSender(repository=repository, channels=channels, result_publisher=result_publisher)

    @provide(scope=Scope.APP)
    def provide_consumer(
        self,
        settings: Settings,
        broker: RabbitBroker,
        exchange: RabbitExchange,
        use_case: ProcessNotificationCommandUseCase,
    ) -> NotificationConsumer:
        return NotificationConsumer(
            broker=broker,
            exchange=exchange,
            use_case=use_case,
            prefetch_count=settings.consumer_prefetch_count,
        )
