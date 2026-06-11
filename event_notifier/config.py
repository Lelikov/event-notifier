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
    consumer_prefetch_count: int = 10
    graceful_timeout: float = 30.0

    database_url: PostgresDsn = Field(strict=True)

    event_users_url: AnyHttpUrl = Field(strict=True)
    event_users_token: str = Field(strict=True)

    # Delivery-result events (notification.*.message_sent) are POSTed to event-receiver.
    # When events_endpoint_url is unset, result publishing is disabled (logged at startup).
    events_endpoint_url: AnyHttpUrl | None = None
    events_api_key: str = ""

    # Default template language: recipients without a known locale get this one.
    default_locale: str = "ru"

    unisender_api_key: str = Field(strict=True)
    unisender_from_email: str = Field(strict=True)
    unisender_from_name: str = "Notifications"
    # UniSender Go template UUIDs, either flat (legacy, treated as the default locale)
    #   UNISENDER_TEMPLATE_IDS={"BOOKING_CREATED": "aaaa-...", "BOOKING_CANCELLED": "..."}
    # or locale-keyed (preferred):
    #   UNISENDER_TEMPLATE_IDS={"ru": {"BOOKING_CREATED": "aaaa-..."}, "en": {"BOOKING_CREATED": "bbbb-..."}}
    unisender_template_ids: dict[str, str | dict[str, str]] = Field(default_factory=dict)

    def unisender_template_ids_by_locale(self) -> dict[str, dict[str, str]]:
        """Normalize UNISENDER_TEMPLATE_IDS to {locale: {TRIGGER_EVENT: template_id}}.

        Flat trigger->id entries are attributed to the default locale, so existing
        deployments keep working unchanged; mixed forms are allowed.
        """
        by_locale: dict[str, dict[str, str]] = {}
        for key, value in self.unisender_template_ids.items():
            if isinstance(value, dict):
                by_locale.setdefault(key.strip().lower(), {}).update(value)
                continue
            by_locale.setdefault(self.default_locale, {})[key] = value
        return by_locale

    telegram_bot_token: str = Field(strict=True)

    fcm_project_id: str | None = None
    fcm_service_account_json: str | None = None
