"""Merge the configuration, publish discovery information, then start LiteLLM."""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
from urllib.request import ProxyHandler, Request, build_opener

import yaml


CONTAINER_PORT = 4000
SUPERVISOR_URL = "http://supervisor"
HTTP = build_opener(ProxyHandler({}))
DEFAULT_CONFIG = """
model_list: []
general_settings:
  store_model_in_db: false
  disable_spend_logs: true
"""


class ConfigurationError(ValueError):
    """The app configuration is invalid."""


class DiscoveryError(Exception):
    """Discovery information could not be published."""


def deep_merge(first, second):
    """Merge mappings recursively. For all other values, use the second value."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        return deepcopy(second)
    merged = {}
    for key in dict.fromkeys([*first, *second]):
        if key not in first:
            merged[key] = deepcopy(second[key])
        elif key not in second:
            merged[key] = deepcopy(first[key])
        else:
            merged[key] = deep_merge(first[key], second[key])
    return merged


def parse_config(text: str, label: str) -> dict:
    """Read a YAML mapping without including its contents in errors."""
    try:
        config = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigurationError(f"{label} contains invalid YAML{location}.") from None
    if not isinstance(config, dict):
        raise ConfigurationError(f"{label} must contain a YAML mapping.")
    return config


def validate_root_path(value: str) -> str:
    """Accept an empty path, the root path, or a path with URL-safe segments."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?)?", value):
        raise ConfigurationError("SERVER_ROOT_PATH must be empty, /, or a path such as /v1 or /api/v1.")
    if any(segment in (".", "..") for segment in value.split("/")):
        raise ConfigurationError("SERVER_ROOT_PATH must not contain . or .. path segments.")
    return value


def read_options(options_path: Path) -> dict:
    """Read the options as a JSON mapping or raise an error."""
    try:
        options = json.loads(options_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ConfigurationError("The app options contain invalid JSON.") from None
    if not isinstance(options, dict):
        raise ConfigurationError("The app options must contain a mapping.")
    return options


def setup_variables(options: dict) -> tuple[str, str, str]:
    """Validate and return the master key, API path, and supplied YAML."""
    master_key = options.get("master_key")
    if not isinstance(master_key, str) or not master_key.strip():
        raise ConfigurationError("Set the master_key password option before you start the app.")
    if master_key.startswith("os.environ/"):
        raise ConfigurationError("master_key must contain the key itself, not an environment reference.")
    if "server_root_path" not in options:
        raise ConfigurationError("server_root_path is required.")
    root_path = validate_root_path(options["server_root_path"])
    supplied = options.get("litellm_config", "{}")
    if not isinstance(supplied, str):
        raise ConfigurationError("litellm_config must be YAML text. Use litellm_config: |.")
    return master_key, root_path, supplied


def merge_configs(supplied: str, master_key: str) -> dict:
    """Merge the defaults, supplied YAML, and required overrides in this order."""
    defaults = parse_config(DEFAULT_CONFIG, "The default configuration")
    supplied_config = parse_config(supplied, "litellm_config")
    overrides = {"general_settings": {"master_key": master_key}}
    return deep_merge(deep_merge(defaults, supplied_config), overrides)


def setup_environment(root_path: str) -> None:
    """Set the API path before LiteLLM loads its configuration."""
    os.environ["SERVER_ROOT_PATH"] = root_path


def prepare_startup(
    options_path: Path = Path("/data/options.json"),
    runtime_dir: Path = Path("/tmp"),
) -> tuple[list[str], str, str]:
    """Write the configuration and return the command, option key, and API path."""
    options = read_options(options_path)
    master_key, root_path, supplied = setup_variables(options)
    config = merge_configs(supplied, master_key)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="litellm-", suffix=".yaml", dir=runtime_dir, delete=False
    ) as output:
        yaml.safe_dump(config, output, sort_keys=False)
    setup_environment(root_path)
    command = ["litellm", "--host", "0.0.0.0", "--port", str(CONTAINER_PORT), "--config", output.name]
    return command, master_key, root_path


def supervisor_request(method: str, path: str, payload: dict | None = None) -> dict:
    """Call the local Supervisor without forwarding the request through a proxy."""
    token = os.environ["SUPERVISOR_TOKEN"]
    request = Request(
        SUPERVISOR_URL + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with HTTP.open(request, timeout=10) as response:
            result = json.load(response)
    except (OSError, ValueError):
        raise DiscoveryError("The Supervisor request failed.") from None
    if not isinstance(result, dict) or result.get("result") != "ok" or not isinstance(result.get("data"), dict):
        raise DiscoveryError("The Supervisor did not accept the request.")
    return result["data"]


def publish_discovery(master_key: str, root_path: str, port: int) -> None:
    """Publish the option key directly, without reading the LiteLLM configuration."""
    result = supervisor_request("POST", "/discovery", {
        "service": "litellm",
        "config": {
            "host": socket.gethostname(),
            "port": port,
            "master_key": master_key,
            "path": root_path,
        },
    })
    if not isinstance(result.get("uuid"), str) or not result["uuid"]:
        raise DiscoveryError("The Supervisor did not confirm discovery registration.")


def main() -> None:
    try:
        command, master_key, root_path = prepare_startup()
        try:
            publish_discovery(master_key, root_path, CONTAINER_PORT)
            print("LiteLLM: Discovery information registered with Home Assistant.", flush=True)
        except DiscoveryError:
            print("LiteLLM: Discovery registration failed. Starting the proxy.", file=sys.stderr)
        del os.environ["SUPERVISOR_TOKEN"]
        os.execvp(command[0], command)
    except ConfigurationError as error:
        print(f"LiteLLM: {error}", file=sys.stderr)
        sys.exit(1)
    except OSError:
        print("LiteLLM: Cannot read the options, write the configuration, or start the proxy.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
