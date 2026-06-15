"""Admin-token auth dependency for the notifier admin API."""

import hmac

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Header, HTTPException, status

from event_notifier.config import Settings


@inject
async def require_admin_token(
    settings: FromDishka[Settings],
    authorization: str = Header(default=""),
) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, settings.notifier_admin_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid notifier admin token")
