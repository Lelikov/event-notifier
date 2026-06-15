"""Tests for the notifier admin API (/api/notifications/*)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from dishka import Provider, Scope, make_async_container, provide
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.adapters.unisender_templates import UnisenderTemplateList
from event_notifier.config import Settings
from event_notifier.db.repository import NotificationRepository
from event_notifier.routes_admin import router as admin_router

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

ADMIN_TOKEN = "test-admin-token-abc"

_FAKE_SETTINGS_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://postgres:password@localhost:5432/event_notifier",
    "EVENT_USERS_URL": "http://localhost:8001",
    "EVENT_USERS_TOKEN": "token",
    "UNISENDER_API_KEY": "key",
    "UNISENDER_FROM_EMAIL": "noreply@example.com",
    "TELEGRAM_BOT_TOKEN": "token",
    "NOTIFIER_ADMIN_TOKEN": ADMIN_TOKEN,
}


class _FakeSql:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    async def fetch_one(self, query: str, values: dict) -> Any:
        return None

    async def fetch_all(self, query: str, values: dict) -> list[Any]:
        return self.rows

    async def execute(self, query: str, values: dict) -> None:
        pass

    def transaction(self) -> Any:
        raise NotImplementedError


class _FakeRepo:
    def __init__(self) -> None:
        self.bindings: list[dict] = []
        self.upserted: list[dict] = []

    async def list_bindings(self) -> list[dict]:
        return self.bindings

    async def upsert_binding(
        self,
        *,
        trigger_event: str,
        channel: str,
        enabled: bool,
        unisender_template_id: str | None,
        telegram_body: str | None,
    ) -> None:
        self.upserted.append(
            {
                "trigger_event": trigger_event,
                "channel": channel,
                "enabled": enabled,
                "unisender_template_id": unisender_template_id,
                "telegram_body": telegram_body,
            }
        )


class _FakeBindings:
    def __init__(self) -> None:
        self.invalidated = False

    def invalidate(self) -> None:
        self.invalidated = True

    async def get(self, trigger_event: str, channel: Any) -> None:
        return None


class _FakeTemplateList:
    def __init__(self, templates: list[dict] | None = None) -> None:
        self._templates = templates or [{"id": "1", "name": "Booking created (dev)"}]

    async def get(self, *, refresh: bool = False) -> list[dict]:
        return self._templates


class FakeProvider(Provider):
    """Minimal DI provider for admin route tests."""

    def __init__(
        self,
        settings: Settings,
        repo: _FakeRepo,
        bindings: _FakeBindings,
        template_list: _FakeTemplateList,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._repo = repo
        self._bindings = bindings
        self._template_list = template_list

    @provide(scope=Scope.APP)
    def provide_settings(self) -> Settings:
        return self._settings

    @provide(scope=Scope.APP)
    def provide_repo(self) -> NotificationRepository:
        return self._repo  # type: ignore[return-value]

    @provide(scope=Scope.APP)
    def provide_bindings(self) -> BindingsProvider:
        return self._bindings  # type: ignore[return-value]

    @provide(scope=Scope.APP)
    def provide_templates(self) -> UnisenderTemplateList:
        return self._template_list  # type: ignore[return-value]


def _make_settings(**overrides: Any) -> Settings:
    env = dict(_FAKE_SETTINGS_ENV, **overrides)
    return Settings(_env_file=None, **{k.lower(): v for k, v in env.items()})


def _make_app(
    settings: Settings,
    repo: _FakeRepo,
    bindings: _FakeBindings,
    template_list: _FakeTemplateList,
) -> FastAPI:
    container = make_async_container(FakeProvider(settings, repo, bindings, template_list), FastapiProvider())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        await container.close()

    application = FastAPI(lifespan=lifespan)
    setup_dishka(container=container, app=application)
    application.include_router(admin_router)
    return application


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return _make_settings()


@pytest.fixture
def repo() -> _FakeRepo:
    return _FakeRepo()


@pytest.fixture
def bindings() -> _FakeBindings:
    return _FakeBindings()


@pytest.fixture
def template_list() -> _FakeTemplateList:
    return _FakeTemplateList()


@pytest.fixture
async def client(settings, repo, bindings, template_list) -> AsyncGenerator[AsyncClient]:
    app = _make_app(settings, repo, bindings, template_list)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdminAuth:
    async def test_no_token_returns_401(self, client: AsyncClient) -> None:
        response = await client.get("/api/notifications/config")
        assert response.status_code == 401

    async def test_wrong_token_returns_401(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/notifications/config",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    async def test_wrong_scheme_returns_401(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/notifications/config",
            headers={"Authorization": f"Basic {ADMIN_TOKEN}"},
        )
        assert response.status_code == 401


class TestGetConfig:
    async def test_returns_bindings(self, client: AsyncClient, auth_headers: dict, repo: _FakeRepo) -> None:
        repo.bindings = [
            {
                "trigger_event": "BOOKING_CREATED",
                "channel": "email",
                "enabled": True,
                "unisender_template_id": "tmpl-1",
                "telegram_body": None,
                "updated_at": "2026-06-15T10:00:00",
            }
        ]
        response = await client.get("/api/notifications/config", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data["bindings"]) == 1
        assert data["bindings"][0]["trigger_event"] == "BOOKING_CREATED"

    async def test_empty_bindings(self, client: AsyncClient, auth_headers: dict) -> None:
        response = await client.get("/api/notifications/config", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"bindings": []}


class TestPutConfig:
    async def test_valid_email_binding(
        self,
        client: AsyncClient,
        auth_headers: dict,
        repo: _FakeRepo,
        bindings: _FakeBindings,
    ) -> None:
        response = await client.put(
            "/api/notifications/config/BOOKING_CREATED/email",
            json={"enabled": True, "unisender_template_id": "tmpl-uuid-1"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert len(repo.upserted) == 1
        assert repo.upserted[0]["trigger_event"] == "BOOKING_CREATED"
        assert repo.upserted[0]["unisender_template_id"] == "tmpl-uuid-1"
        assert bindings.invalidated is True

    async def test_valid_telegram_binding(
        self,
        client: AsyncClient,
        auth_headers: dict,
        repo: _FakeRepo,
        bindings: _FakeBindings,
    ) -> None:
        response = await client.put(
            "/api/notifications/config/BOOKING_CREATED/telegram",
            json={"enabled": True, "telegram_body": "Привет, {{ client_name }}!"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert bindings.invalidated is True
        assert repo.upserted[0]["telegram_body"] == "Привет, {{ client_name }}!"

    async def test_invalid_jinja_returns_400(self, client: AsyncClient, auth_headers: dict) -> None:
        response = await client.put(
            "/api/notifications/config/BOOKING_CREATED/telegram",
            json={"enabled": True, "telegram_body": "{{ unclosed"},
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "invalid jinja" in response.json()["detail"]

    async def test_unknown_channel_returns_400(self, client: AsyncClient, auth_headers: dict) -> None:
        response = await client.put(
            "/api/notifications/config/BOOKING_CREATED/push",
            json={"enabled": True},
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "unknown channel" in response.json()["detail"]


class TestUnisenderTemplates:
    async def test_returns_templates(
        self,
        client: AsyncClient,
        auth_headers: dict,
        template_list: _FakeTemplateList,
    ) -> None:
        template_list._templates = [
            {"id": "1", "name": "Booking created (dev)"},
            {"id": "2", "name": "Booking cancelled (dev)"},
        ]
        response = await client.get("/api/notifications/unisender-templates", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data["templates"]) == 2
        assert data["templates"][0]["id"] == "1"


class TestTelegramPreview:
    async def test_renders_template(self, client: AsyncClient, auth_headers: dict) -> None:
        response = await client.post(
            "/api/notifications/telegram/preview",
            json={"telegram_body": "Привет, {{ client_name }}!", "sample_data": {"client_name": "Анна"}},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["rendered"] == "Привет, Анна!"

    async def test_uses_default_sample_data(self, client: AsyncClient, auth_headers: dict) -> None:
        response = await client.post(
            "/api/notifications/telegram/preview",
            json={"telegram_body": "Клиент: {{ client_name }}"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert "Иван" in response.json()["rendered"]

    async def test_invalid_template_returns_400(self, client: AsyncClient, auth_headers: dict) -> None:
        response = await client.post(
            "/api/notifications/telegram/preview",
            json={"telegram_body": "{% for x in %}broken{% endfor %}"},
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "render error" in response.json()["detail"]
