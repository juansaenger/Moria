from __future__ import annotations

import datetime
from typing import Any

import httpx


class HomeAssistantError(RuntimeError):
    pass


class HomeAssistantClient:
    """Thin async wrapper around the Home Assistant REST API."""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HomeAssistantClient":
        self._http = httpx.AsyncClient(
            base_url=f"{self._base_url}/api",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._http is None:
            raise HomeAssistantError("HomeAssistantClient must be used as an async context manager")
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise HomeAssistantError(f"Could not reach Home Assistant at {self._base_url}: {exc}") from exc
        if response.status_code == 401:
            raise HomeAssistantError("Home Assistant rejected the access token (401). Check HOMEASSISTANT_TOKEN.")
        if response.status_code >= 400:
            raise HomeAssistantError(
                f"Home Assistant returned {response.status_code} for {method} {path}: {response.text}"
            )
        if not response.content:
            return None
        return response.json()

    async def get_states(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/states")

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/states/{entity_id}")

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = dict(data or {})
        if entity_id:
            payload["entity_id"] = entity_id
        return await self._request("POST", f"/services/{domain}/{service}", json=payload)

    async def get_automation_config(self, automation_id: str) -> dict[str, Any]:
        """The editable config behind an automation, as stored in automations.yaml."""
        return await self._request("GET", f"/config/automation/config/{automation_id}")

    async def save_automation_config(self, automation_id: str, config: dict[str, Any]) -> Any:
        """Create or replace an automation. Home Assistant reloads them itself."""
        return await self._request("POST", f"/config/automation/config/{automation_id}", json=config)

    async def delete_automation_config(self, automation_id: str) -> Any:
        return await self._request("DELETE", f"/config/automation/config/{automation_id}")

    async def get_history(self, entity_id: str, hours: float) -> Any:
        start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
        return await self._request(
            "GET",
            f"/history/period/{start.isoformat()}",
            params={"filter_entity_id": entity_id, "minimal_response": ""},
        )

    async def get_services(self) -> Any:
        return await self._request("GET", "/services")

    async def get_config(self) -> Any:
        return await self._request("GET", "/config")

    async def get_error_log(self) -> str:
        if self._http is None:
            raise HomeAssistantError("HomeAssistantClient must be used as an async context manager")
        response = await self._http.get("/error_log")
        return response.text
