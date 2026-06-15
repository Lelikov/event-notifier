"""Admin API routes for notification binding management."""

from typing import Any

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, HTTPException, status
from jinja2 import TemplateError
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel

from event_notifier.adapters.bindings_provider import BindingsProvider
from event_notifier.adapters.unisender_templates import UnisenderTemplateList
from event_notifier.admin_auth import require_admin_token
from event_notifier.db.repository import NotificationRepository

router = APIRouter(
    prefix="/api/notifications",
    route_class=DishkaRoute,
    dependencies=[Depends(require_admin_token)],
)

_CHANNELS = {"email", "telegram"}

_SAMPLE = {
    "client_name": "Иван",
    "organizer_name": "Пётр",
    "start_time_local": "15 июн 13:00",
    "end_time_local": "15 июн 14:00",
    "time_zone": "Europe/Moscow",
    "meeting_url": "https://example/x",
}


class BindingIn(BaseModel):
    enabled: bool
    unisender_template_id: str | None = None
    telegram_body: str | None = None


class PreviewIn(BaseModel):
    telegram_body: str
    sample_data: dict[str, Any] | None = None


@router.get("/config")
async def get_config(repo: FromDishka[NotificationRepository]) -> dict[str, Any]:
    """Return all notification bindings."""
    return {"bindings": await repo.list_bindings()}


@router.put("/config/{trigger_event}/{channel}")
async def put_config(
    trigger_event: str,
    channel: str,
    body: BindingIn,
    repo: FromDishka[NotificationRepository],
    bindings: FromDishka[BindingsProvider],
) -> dict[str, str]:
    """Upsert a notification binding; invalidates the in-process bindings cache."""
    if channel not in _CHANNELS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown channel")
    if channel == "telegram" and body.telegram_body:
        try:
            SandboxedEnvironment(autoescape=False).from_string(body.telegram_body)
        except TemplateError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid jinja: {exc}") from exc
    await repo.upsert_binding(
        trigger_event=trigger_event,
        channel=channel,
        enabled=body.enabled,
        unisender_template_id=body.unisender_template_id,
        telegram_body=body.telegram_body,
    )
    bindings.invalidate()
    return {"status": "ok"}


@router.get("/unisender-templates")
async def unisender_templates(
    templates: FromDishka[UnisenderTemplateList],
    refresh: bool = False,
) -> dict[str, Any]:
    """Return the cached UniSender Go template list; pass ?refresh=true to force a reload."""
    return {"templates": await templates.get(refresh=refresh)}


@router.post("/telegram/preview")
async def telegram_preview(body: PreviewIn) -> dict[str, str]:
    """Render a Telegram Jinja body with sample data and return the rendered text."""
    try:
        rendered = (
            SandboxedEnvironment(autoescape=False)
            .from_string(body.telegram_body)
            .render(**(body.sample_data or _SAMPLE))
        )
    except TemplateError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"render error: {exc}") from exc
    return {"rendered": rendered.strip()}
