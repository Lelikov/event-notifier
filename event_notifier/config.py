from pydantic import AmqpDsn, AnyHttpUrl, Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    debug: bool = False
    log_level: str = "INFO"

    # Queue name/args come from event_schemas.queues.NOTIFICATION_COMMANDS_QUEUE
    rabbit_url: AmqpDsn = "amqp://guest:guest@localhost:5672/"
    rabbit_exchange: str = "events"

    database_url: PostgresDsn = Field(strict=True)

    event_users_url: AnyHttpUrl = Field(strict=True)
    event_users_token: str = Field(strict=True)

    unisender_api_key: str = Field(strict=True)
    unisender_from_email: str = Field(strict=True)
    unisender_from_name: str = "Notifications"

    telegram_bot_token: str = Field(strict=True)

    fcm_project_id: str | None = None
    fcm_service_account_json: str | None = None
