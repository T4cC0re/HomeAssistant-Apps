"""Read Supervisor data and check ingress sessions."""

import json
import logging
import re

import aiohttp


LOGGER = logging.getLogger(__name__)
CORE_ONLY_ENDPOINTS = {"/discovery", "/ingress/validate_session"}
PUBLIC_DISCOVERY_FIELDS = {"host", "port", "path", "ssl", "protocol"}
CORE_PROXY_URL = "http://192.168.178.147:8123"


class SupervisorError(RuntimeError):
    """The Supervisor request failed."""


class SupervisorForbidden(SupervisorError):
    """The Supervisor did not permit the request."""


class SupervisorCoreOnly(SupervisorForbidden):
    """Only Home Assistant Core can use the endpoint."""


class SupervisorInvalidSession(SupervisorError):
    """The ingress session is not valid."""


def redact_discovery(data: dict) -> dict:
    """Remove private configuration values from a discovery log record."""
    result = dict(data)
    messages = data.get("discovery")
    if isinstance(messages, list):
        result["discovery"] = []
        for message in messages:
            if not isinstance(message, dict):
                result["discovery"].append("[redacted]")
                continue
            safe = {key: value for key, value in message.items() if key in {"addon", "app", "service", "uuid"}}
            config = message.get("config")
            if isinstance(config, dict):
                safe["config"] = {
                    key: value if key in PUBLIC_DISCOVERY_FIELDS and isinstance(value, (str, int, bool)) else "[redacted]"
                    for key, value in config.items()
                }
            result["discovery"].append(safe)
    return {key: value for key, value in result.items() if key in {"discovery", "services"}}


def litellm_overlay(config: dict) -> dict:
    """Convert the LiteLLM discovery fields to an OpenCode provider."""
    host, port = config.get("host"), config.get("port")
    path, key = config.get("path", ""), config.get("master_key")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise SupervisorError("LiteLLM has an invalid host name.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise SupervisorError("LiteLLM has an invalid port.")
    if not isinstance(path, str) or not re.fullmatch(r"(?:/[A-Za-z0-9._~/-]*)?", path):
        raise SupervisorError("LiteLLM has an invalid API path.")
    if any(part in {".", ".."} for part in path.split("/")):
        raise SupervisorError("LiteLLM has an invalid API path.")
    if not isinstance(key, str) or not key.strip() or key.startswith("!secret"):
        raise SupervisorError("LiteLLM has no usable master key.")
    base_url = f"http://{host}:{port}{path.rstrip('/') or '/v1'}"
    return {
        "provider": {
            "litellm": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "LiteLLM",
                "options": {"baseURL": base_url, "apiKey": key},
                "models": {},
            }
        }
    }


class Supervisor:
    """Use a caller-owned HTTP session to read the Supervisor API."""

    def __init__(self, session: aiohttp.ClientSession, token: str, base_url: str = "http://supervisor",
                 core_proxy_url: str = CORE_PROXY_URL):
        if not isinstance(token, str) or not token:
            raise SupervisorError("SUPERVISOR_TOKEN is required.")
        self.session = session
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.core_proxy_url = core_proxy_url.rstrip("/")
        self.info: dict = {}

    async def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Send an authenticated request without logging private response data."""
        try:
            async with self.session.request(
                method,
                self.base_url + path,
                json=payload,
                headers={"Authorization": f"Bearer {self.token}", "Accept-Encoding": "identity"},
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=False,
            ) as response:
                body = await response.text()
                status = response.status
                content_type = response.content_type
        except (aiohttp.ClientError, TimeoutError, UnicodeError):
            raise SupervisorError(f"The Supervisor request to {path} failed.") from None
        if (
            path in CORE_ONLY_ENDPOINTS
            and self.info.get("hassio_role") == "admin"
            and self.info.get("hassio_api") is True
            and status == 401
            and content_type == "text/plain"
            and body.strip() == "401: Unauthorized"
        ):
            raise SupervisorCoreOnly(f"Only Home Assistant Core can use {path}.")
        if status == 403:
            raise SupervisorForbidden(f"The Supervisor denied {path} (HTTP 403).")
        try:
            result = json.loads(body)
        except (ValueError, TypeError):
            raise SupervisorError(f"The Supervisor returned an invalid response for {path} (HTTP {status}).") from None
        if status == 401 and path == "/ingress/validate_session" and isinstance(result, dict) and result.get("result") == "error":
            raise SupervisorInvalidSession("The ingress session is not valid.")
        if not 200 <= status < 300 or not isinstance(result, dict) or result.get("result") != "ok":
            raise SupervisorError(f"The Supervisor did not accept {path} (HTTP {status}).")
        data = result.get("data", {})
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise SupervisorError(f"The Supervisor returned invalid data for {path}.")
        return data

    async def self_info(self) -> dict:
        """Read the app information and retain proof of API access."""
        self.info = await self.request("GET", "/addons/self/info")
        return self.info

    async def validate_session(self, token: str) -> bool:
        """Check a session, or report a known Core-only API restriction."""
        if not token:
            return False
        try:
            await self.request("POST", "/ingress/validate_session", {"session": token})
        except SupervisorCoreOnly:
            raise
        except SupervisorInvalidSession:
            return False
        return True

    async def probe_core_proxy(self) -> dict | None:
        """Try the requested Core proxy paths with this app's token."""
        for path in ("discover", "discovery"):
            try:
                async with self.session.get(
                    f"{self.core_proxy_url}/api/hassio/{path}",
                    headers={"Authorization": f"Bearer {self.token}", "Accept-Encoding": "identity"},
                    timeout=aiohttp.ClientTimeout(total=5),
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    body = await response.text()
                    content_type = response.content_type
            except (aiohttp.ClientError, TimeoutError, UnicodeError):
                LOGGER.warning("Core proxy /api/hassio/%s could not be reached.", path)
                continue
            data = None
            payload = None
            if content_type == "application/json":
                try:
                    payload = json.loads(body)
                except ValueError:
                    pass
            if status == 200 and isinstance(payload, dict):
                if payload.get("result") == "ok":
                    candidate = payload.get("data")
                    if isinstance(candidate, dict) and isinstance(candidate.get("discovery"), list):
                        data = candidate
            if data is not None:
                LOGGER.info("Core proxy /api/hassio/%s HTTP %s: %s", path, status,
                            json.dumps(redact_discovery(data), sort_keys=True))
                return data
            summary = {"body": "[redacted]"}
            if isinstance(payload, dict):
                result = payload.get("result")
                summary = {"result": result if isinstance(result, str) and result in {"ok", "error"} else "[redacted]"}
                if "message" in payload:
                    summary["message"] = "[redacted]"
            LOGGER.warning("Core proxy /api/hassio/%s returned HTTP %s: %s", path, status,
                           json.dumps(summary, sort_keys=True))
        return None

    async def startup(self, litellm_discovery: bool = True) -> dict:
        """Log discovery data, check LiteLLM, and return its provider config."""
        await self.self_info()
        discovery = None
        try:
            discovery = await self.request("GET", "/discovery")
            LOGGER.info("Supervisor /discovery: %s", json.dumps(redact_discovery(discovery), sort_keys=True))
        except SupervisorError as error:
            LOGGER.warning("Cannot read Supervisor /discovery: %s", error)
            discovery = await self.probe_core_proxy()
        addons = await self.request("GET", "/addons")
        installed = addons.get("addons")
        if not isinstance(installed, list):
            raise SupervisorError("The Supervisor returned an invalid installed app list.")
        matches = [
            addon for addon in installed
            if isinstance(addon, dict) and isinstance(addon.get("slug"), str)
            and (addon["slug"] == "litellm" or addon["slug"].endswith("_litellm"))
        ]
        if not matches:
            LOGGER.warning("The LiteLLM app is not installed.")
            return {}
        if not litellm_discovery:
            return {}
        config = self._discovered_config(discovery, matches)
        if config is None:
            LOGGER.warning("No running LiteLLM discovery record is available.")
            return {}
        try:
            overlay = litellm_overlay(config)
        except SupervisorError as error:
            LOGGER.warning("Cannot configure the LiteLLM provider: %s", error)
            return {}
        await self._add_models(overlay)
        return overlay

    @staticmethod
    def _discovered_config(discovery: dict | None, installed: list[dict]) -> dict | None:
        if discovery is None:
            return None
        messages = discovery.get("discovery")
        if not isinstance(messages, list):
            LOGGER.warning("The Supervisor discovery record list is invalid.")
            return None
        slugs = {addon["slug"] for addon in installed}
        for message in messages:
            if not isinstance(message, dict) or message.get("service") != "litellm":
                continue
            if message.get("addon", message.get("app")) not in slugs:
                continue
            if isinstance(message.get("config"), dict):
                return message["config"]
        return None

    async def _add_models(self, overlay: dict) -> None:
        provider = overlay["provider"]["litellm"]
        options = provider["options"]
        try:
            async with self.session.get(
                options["baseURL"] + "/models",
                headers={"Authorization": f"Bearer {options['apiKey']}", "Accept-Encoding": "identity"},
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=False,
            ) as response:
                response.raise_for_status()
                result = await response.json()
            models = result.get("data") if isinstance(result, dict) else None
            if not isinstance(models, list):
                raise ValueError
            provider["models"] = {
                model["id"]: {"name": model["id"]}
                for model in models
                if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"]
            }
        except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError):
            LOGGER.warning("Cannot read the LiteLLM model list. Set provider.litellm.models in the OpenCode configuration.")
