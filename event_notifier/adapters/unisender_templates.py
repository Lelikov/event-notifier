"""In-memory TTL cache of UniSender Go transactional templates."""

import time

import httpx


class UnisenderTemplateList:
    """Fetches the template list from UniSender Go and caches it in memory.

    UniSender Go: POST /ru/transactional/api/v1/template/list.json
    Response shape: {"templates": [{"id": <int>, "title": "<str>"}, ...]}
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        ttl_seconds: int = 3600,
    ) -> None:
        self._client = http_client
        self._url = f"{base_url.rstrip('/')}/ru/transactional/api/v1/template/list.json"
        self._api_key = api_key
        self._ttl = ttl_seconds
        self._cache: list[dict] | None = None
        self._expires_at = 0.0

    async def get(self, *, refresh: bool = False) -> list[dict]:
        if refresh or self._cache is None or time.monotonic() >= self._expires_at:
            resp = await self._client.post(
                self._url,
                headers={"X-API-KEY": self._api_key},
                json={"limit": 100, "offset": 0},
            )
            resp.raise_for_status()
            body = resp.json()
            templates = body.get("templates", body.get("data", []))
            self._cache = [
                {
                    "id": str(t.get("id")),
                    "name": t.get("title") or t.get("name") or str(t.get("id")),
                }
                for t in templates
            ]
            self._expires_at = time.monotonic() + self._ttl
        return self._cache
