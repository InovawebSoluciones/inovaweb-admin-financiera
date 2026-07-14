"""Cliente HTTP base async con retries y manejo de errores comun."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("http_client")


class CoreClientError(Exception):
    def __init__(self, core: str, status_code: int, body: str):
        self.core = core
        self.status_code = status_code
        self.body = body
        super().__init__(f"{core} respondio {status_code}: {body[:200]}")


class CoreClientUnreachable(Exception):
    def __init__(self, core: str, msg: str):
        self.core = core
        super().__init__(f"{core} no responde: {msg}")


class CoreClient:
    """Wrapper httpx con API key, retries y logging estructurado."""

    def __init__(
        self,
        core_name: str,
        base_url: str,
        api_key: str,
        timeout_sec: float = 10.0,
        retries: int = 3,
        api_key_header: str = "X-API-Key",
    ):
        self.core = core_name
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={api_key_header: api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(timeout_sec),
        )
        self.retries = retries

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = await self._client.request(method, path, json=json, params=params)
                if resp.status_code >= 500:
                    raise CoreClientError(self.core, resp.status_code, resp.text)
                if resp.status_code >= 400:
                    # 4xx no se reintenta
                    raise CoreClientError(self.core, resp.status_code, resp.text)
                return resp.json() if resp.content else None
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = CoreClientUnreachable(self.core, str(e))
            except CoreClientError as e:
                if e.status_code < 500:
                    raise
                last_exc = e
            if attempt < self.retries:
                await asyncio.sleep(0.3 * attempt)
        assert last_exc is not None
        raise last_exc

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def patch(self, path: str, **kw: Any) -> Any:
        return await self.request("PATCH", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def close(self) -> None:
        await self._client.aclose()
