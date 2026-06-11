"""Dishka DI container for event-notifier."""

from collections.abc import AsyncGenerator

import httpx
import structlog
from dishka import Provider, Scope, provide
from faststream.rabbit import ExchangeType, RabbitBroker, RabbitExchange
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from event_notifier.adapters.consumer import NotificationConsumer
from event_notifier.adapters.outbox_sender import OutboxSender
from event_notifier.adapters.sql import SqlExecutor
from event_notifier.application.use_cases.process_domain_event import ProcessDomainEventUseCase
from event_notifier.config import Settings
from event_notifier.db.repository import NotificationRepository
from event_notifier.domain.models.notification import ChannelType
from event_notifier.infrastructure.channels.email import EmailChannel
from event_notifier.infrastructure.channels.telegram import TelegramChannel
from event_notifier.infrastructure.users_client import UsersClient
from event_notifier.interfaces.channels import INotificationChannel
from event_notifier.interfaces.sql import ISqlExecutor

logger = structlog.get_logger(__name__)


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
    async def provide_session(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> AsyncGenerator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    @provide(scope=Scope.APP)
    def provide_sql_executor(self, session: AsyncSession) -> ISqlExecutor:
        return SqlExecutor(session)

    @provide(scope=Scope.APP)
    def provide_repository(self, sql: ISqlExecutor) -> NotificationRepository:
        return NotificationRepository(sql=sql)

    @provide(scope=Scope.APP)
    def provide_exchange(self, settings: Settings) -> RabbitExchange:
        return RabbitExchange(name=settings.rabbit_exchange, type=ExchangeType.TOPIC, durable=True)

    @provide(scope=Scope.APP)
    def provide_broker(self, settings: Settings) -> RabbitBroker:
        return RabbitBroker(str(settings.rabbit_url))

    @provide(scope=Scope.APP)
    async def provide_users_client(self, settings: Settings) -> AsyncGenerator[UsersClient]:
        async with AsyncClient(
            base_url=str(settings.event_users_url),
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0),
        ) as client:
            yield UsersClient(http_client=client, api_token=settings.event_users_token)

    @provide(scope=Scope.APP)
    async def provide_email_channel(self, settings: Settings) -> AsyncGenerator[EmailChannel]:
        async with AsyncClient(
            base_url="https://go.unisender.ru",
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0),
        ) as client:
            yield EmailChannel(
                http_client=client,
                api_key=settings.unisender_api_key,
                from_email=settings.unisender_from_email,
                from_name=settings.unisender_from_name,
            )

    @provide(scope=Scope.APP)
    async def provide_telegram_channel(self, settings: Settings) -> AsyncGenerator[TelegramChannel]:
        async with AsyncClient(
            base_url="https://api.telegram.org",
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0),
        ) as client:
            yield TelegramChannel(http_client=client, bot_token=settings.telegram_bot_token)

    @provide(scope=Scope.APP)
    def provide_use_case(
        self,
        repository: NotificationRepository,
        users_client: UsersClient,
    ) -> ProcessDomainEventUseCase:
        return ProcessDomainEventUseCase(
            repository=repository,
            users_client=users_client,
        )

    @provide(scope=Scope.APP)
    def provide_outbox_sender(
        self,
        repository: NotificationRepository,
        email_channel: EmailChannel,
        telegram_channel: TelegramChannel,
    ) -> OutboxSender:
        channels: dict[ChannelType, INotificationChannel] = {
            ChannelType.EMAIL: email_channel,
            ChannelType.TELEGRAM: telegram_channel,
            # ChannelType.PUSH: push_channel  — включить после настройки FCM
        }
        return OutboxSender(repository=repository, channels=channels)

    @provide(scope=Scope.APP)
    def provide_consumer(
        self,
        broker: RabbitBroker,
        exchange: RabbitExchange,
        use_case: ProcessDomainEventUseCase,
    ) -> NotificationConsumer:
        return NotificationConsumer(
            broker=broker,
            exchange=exchange,
            use_case=use_case,
        )
