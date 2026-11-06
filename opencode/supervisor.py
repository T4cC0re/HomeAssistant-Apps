"""Read LiteLLM app settings and check ingress sessions."""

import json
import logging
import re

import aiohttp


LOGGER = logging.getLogger(__name__)


class SupervisorError(RuntimeError):
    """The Supervisor request failed."""


class SupervisorForbidden(SupervisorError):
    """The Supervisor did not permit the request."""


class SupervisorCoreOnly(SupervisorForbidden):
    """Only Home Assistant Core can use the endpoint."""


class SupervisorInvalidSession(SupervisorError):
    """The ingress session is not valid."""


def litellm_connection(info: dict) -> dict:
    """Get the connection from the installed LiteLLM app information."""
    host = info.get("hostname")
    options = info.get("options")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise SupervisorError("LiteLLM has an invalid host name.")
    if not isinstance(options, dict):
        raise SupervisorError("LiteLLM has no app options.")
    path, key = options.get("server_root_path"), options.get("master_key")
    if not isinstance(path, str) or not re.fullmatch(r"(?:/[A-Za-z0-9._~/-]*)?", path):
        raise SupervisorError("LiteLLM has an invalid API path.")
    if any(part in {".", ".."} for part in path.split("/")) or (len(path) > 1 and path.endswith("/")):
        raise SupervisorError("LiteLLM has an invalid API path.")
    if not isinstance(key, str) or not key.strip() or key.startswith(("!secret", "os.environ/")):
        raise SupervisorError("LiteLLM has no usable master key.")
    return {"base_url": f"http://{host}:4000{path}", "master_key": key}


class Supervisor:
    """Use a caller-owned HTTP session to read the Supervisor API."""

    def __init__(self, session: aiohttp.ClientSession, token: str, base_url: str = "http://supervisor"):
        if not isinstance(token, str) or not token:
            raise SupervisorError("SUPERVISOR_TOKEN is required.")
        self.session = session
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.api_verified = False

    async def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Send an authenticated request without logging response data."""
        try:
            async with self.session.request(
                method, self.base_url + path, json=payload,
                headers={"Authorization": f"Bearer {self.token}", "Accept-Encoding": "identity"},
                timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False,
            ) as response:
                body, status, content_type = await response.text(), response.status, response.content_type
        except (aiohttp.ClientError, TimeoutError, UnicodeError):
            raise SupervisorError(f"The Supervisor request to {path} failed.") from None
        if (path == "/ingress/validate_session" and self.api_verified and content_type == "text/plain"
                and (status, body.strip()) in {(401, "401: Unauthorized"), (403, "403: Forbidden")}):
            raise SupervisorCoreOnly("Only Home Assistant Core can use the session check.")
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

    async def validate_session(self, token: str) -> bool:
        """Check a session, or report a known Core-only API restriction."""
        if not token:
            return False
        try:
            await self.request("POST", "/ingress/validate_session", {"session": token})
        except SupervisorInvalidSession:
            return False
        return True

    async def startup(self, litellm_discovery: bool = True) -> dict:
        """Read the installed LiteLLM app settings when enabled."""
        try:
            addons = await self.request("GET", "/addons")
            self.api_verified = True
            installed = addons.get("addons")
            if not isinstance(installed, list):
                raise SupervisorError("The Supervisor returned an invalid installed app list.")
            if not litellm_discovery:
                return {}
            matches = [addon["slug"] for addon in installed if isinstance(addon, dict)
                       and isinstance(addon.get("slug"), str)
                       and re.fullmatch(r"(?:[a-zA-Z0-9]+_)?litellm", addon["slug"])]
            if len(matches) != 1:
                LOGGER.warning("Expected one installed LiteLLM app; found %s.", len(matches))
                return {}
            info = await self.request("GET", f"/addons/{matches[0]}/info")
            return litellm_connection(info)
        except SupervisorError as error:
            LOGGER.warning("Cannot configure LiteLLM: %s", error)
            return {}
