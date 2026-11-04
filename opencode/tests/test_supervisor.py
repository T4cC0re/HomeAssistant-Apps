"""Check Supervisor access, discovery data, and session failures."""

from collections import deque
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import yaml

from supervisor import (
    Supervisor,
    SupervisorCoreOnly,
    SupervisorError,
    SupervisorForbidden,
    litellm_overlay,
    redact_discovery,
)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = deque()
        self.requests = []

        async def handle(request):
            self.requests.append({
                "method": request.method,
                "path": request.path,
                "authorization": request.headers.get("Authorization"),
                "body": await request.json() if request.can_read_body else None,
            })
            if not self.responses:
                raise web.HTTPInternalServerError
            status, body = self.responses.popleft()
            if isinstance(body, str):
                return web.Response(status=status, text=body)
            return web.json_response(body, status=status)

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = aiohttp.ClientSession(trust_env=False)
        url = str(self.server.make_url(""))
        self.supervisor = Supervisor(self.session, "test-supervisor-token", url, url, url)
        self.addAsyncCleanup(self.server.close)
        self.addAsyncCleanup(self.session.close)

    def respond(self, data=None):
        self.responses.append((200, {"result": "ok", "data": data}))

    def admin_info(self):
        self.respond({"hassio_api": True, "hassio_role": "admin", "ingress_url": "/api/hassio_ingress/test"})

    def startup_probes(self):
        self.respond({"version": "test"})
        self.responses.append((403, "403: Forbidden"))
        self.responses.append((401, "401: Unauthorized"))

    async def test_validator_posts_cookie_and_authenticates(self):
        self.respond()
        self.assertTrue(await self.supervisor.validate_session("test-cookie"))
        self.assertEqual(self.requests, [{
            "method": "POST",
            "path": "/ingress/validate_session",
            "authorization": "Bearer test-supervisor-token",
            "body": {"session": "test-cookie"},
        }])

    async def test_invalid_and_missing_sessions_return_false(self):
        self.assertFalse(await self.supervisor.validate_session(""))
        self.responses.append((401, {"result": "error", "message": "Ingress session is not valid"}))
        self.assertFalse(await self.supervisor.validate_session("expired-cookie"))
        self.assertEqual(len(self.requests), 1)

    async def test_core_only_requires_authenticated_admin_and_exact_bare_401(self):
        self.admin_info()
        await self.supervisor.self_info()
        self.responses.append((401, "401: Unauthorized"))
        with self.assertRaises(SupervisorCoreOnly):
            await self.supervisor.validate_session("test-cookie")
        self.responses.append((401, "unexpected-private-response"))
        with self.assertRaises(SupervisorError) as caught:
            await self.supervisor.validate_session("test-cookie")
        self.assertNotIsInstance(caught.exception, SupervisorCoreOnly)
        self.assertNotIn("unexpected-private-response", str(caught.exception))

    async def test_bare_401_without_admin_proof_does_not_enable_fallback(self):
        for info in ({}, {"hassio_api": True, "hassio_role": "manager"}, {"hassio_role": "admin"}):
            self.supervisor.info = info
            self.responses.append((401, "401: Unauthorized"))
            with self.assertRaises(SupervisorError) as caught:
                await self.supervisor.validate_session("test-cookie")
            self.assertNotIsInstance(caught.exception, SupervisorCoreOnly)

    async def test_generic_forbidden_is_distinct_and_does_not_enable_fallback(self):
        self.supervisor.info = {"hassio_api": True, "hassio_role": "admin"}
        self.responses.append((403, "403: Forbidden"))
        with self.assertRaises(SupervisorForbidden) as caught:
            await self.supervisor.validate_session("test-cookie")
        self.assertNotIsInstance(caught.exception, SupervisorCoreOnly)

    async def test_missing_litellm_warns_with_discovery_disabled(self):
        self.admin_info()
        self.startup_probes()
        self.respond({"discovery": [], "services": {}})
        self.respond({"addons": []})
        with self.assertLogs("supervisor", level="INFO") as logs:
            overlay = await self.supervisor.startup(litellm_discovery=False)
        self.assertEqual(overlay, {})
        self.assertTrue(any("not installed" in line for line in logs.output))
        self.assertEqual([item["path"] for item in self.requests], [
            "/addons/self/info", "/info", "/core/api/hassio/discovery",
            "/api/hassio/discover", "/discovery", "/addons",
        ])
        self.assertTrue(all(item["authorization"] == "Bearer test-supervisor-token" for item in self.requests))
        self.assertTrue(any("Probe " in line and "HTTP 403: 403: Forbidden" in line for line in logs.output))

    async def test_startup_uses_published_discovery_and_redacts_log(self):
        self.admin_info()
        self.startup_probes()
        self.respond({"discovery": [{
            "addon": "local_litellm", "service": "litellm", "uuid": "record-1",
            "config": {"host": "local-litellm", "port": 4000, "path": "/v1", "master_key": "private-key"},
        }], "services": {"litellm": ["local_litellm"]}})
        self.respond({"addons": [{"slug": "local_litellm", "state": "started"}]})
        with patch.object(self.supervisor, "_add_models", new=AsyncMock()) as add_models:
            with self.assertLogs("supervisor", level="INFO") as logs:
                overlay = await self.supervisor.startup()
        provider = overlay["provider"]["litellm"]
        self.assertEqual(provider["options"], {"baseURL": "http://local-litellm:4000/v1", "apiKey": "private-key"})
        self.assertNotIn("private-key", "\n".join(logs.output))
        self.assertIn("[redacted]", "\n".join(logs.output))
        add_models.assert_awaited_once_with(overlay)

    async def test_probe_logs_raw_bodies_and_uses_valid_discovery(self):
        self.admin_info()
        self.responses.append((200, '{"result":"ok","data":{"version":"private-info"}}'))
        self.respond({"discovery": [{
            "addon": "local_litellm", "service": "litellm",
            "config": {"host": "local-litellm", "port": 4000, "master_key": "private-probe-key"},
        }]})
        self.responses.append((403, "homeassistant-denied"))
        self.responses.append((401, "401: Unauthorized"))
        self.respond({"addons": [{"slug": "local_litellm", "state": "started"}]})
        with patch.object(self.supervisor, "_add_models", new=AsyncMock()):
            with self.assertLogs("supervisor", level="INFO") as logs:
                overlay = await self.supervisor.startup()
        output = "\n".join(logs.output)
        self.assertIn('"ingress_url": "/api/hassio_ingress/test"', output)
        self.assertIn('"version":"private-info"', output)
        self.assertIn("private-probe-key", output)
        self.assertIn("homeassistant-denied", output)
        self.assertNotIn("test-supervisor-token", output)
        self.assertEqual(overlay["provider"]["litellm"]["options"]["apiKey"], "private-probe-key")
        self.assertTrue(all(item["authorization"] == "Bearer test-supervisor-token" for item in self.requests))
        self.assertEqual([item["path"] for item in self.requests], [
            "/addons/self/info", "/info", "/core/api/hassio/discovery",
            "/api/hassio/discover", "/discovery", "/addons",
        ])

    async def test_probe_does_not_follow_redirect_or_log_echoed_token(self):
        self.responses.append((302, "test-supervisor-token"))
        with self.assertLogs("supervisor", level="INFO") as logs:
            self.assertIsNone(await self.supervisor.probe_url(self.supervisor.base_url + "/info"))
        self.assertEqual(len(self.requests), 1)
        self.assertIn("HTTP 302: [redacted SUPERVISOR_TOKEN]", "\n".join(logs.output))
        self.assertNotIn("test-supervisor-token", "\n".join(logs.output))

    async def test_probe_connection_failure_does_not_stop_startup(self):
        self.supervisor.homeassistant_url = "http://127.0.0.1:1"
        self.admin_info()
        self.respond({"version": "test"})
        self.responses.append((403, "403: Forbidden"))
        self.respond({"discovery": []})
        self.respond({"addons": []})
        with self.assertLogs("supervisor", level="WARNING") as logs:
            self.assertEqual(await self.supervisor.startup(), {})
        self.assertTrue(any("could not be reached" in line and "ClientConnectorError" in line for line in logs.output))

    async def test_self_info_denial_does_not_stop_probe_startup(self):
        self.responses.append((403, "self-info-denied"))
        self.startup_probes()
        self.respond({"discovery": []})
        self.respond({"addons": []})
        with self.assertLogs("supervisor", level="INFO") as logs:
            self.assertEqual(await self.supervisor.startup(), {})
        self.assertIn("self-info-denied", "\n".join(logs.output))

    async def test_core_only_discovery_checks_core_proxy_without_reading_litellm_options(self):
        self.admin_info()
        self.startup_probes()
        self.responses.append((401, "401: Unauthorized"))
        self.responses.append((401, {"result": "error", "message": "private-proxy-reason"}))
        self.responses.append((401, "401: Unauthorized"))
        self.respond({"addons": [{"slug": "local_litellm", "state": "started"}]})
        with patch.object(self.supervisor, "_add_models", new=AsyncMock()):
            with self.assertLogs("supervisor", level="INFO") as logs:
                overlay = await self.supervisor.startup()
        self.assertEqual(overlay, {})
        self.assertEqual([item["path"] for item in self.requests], [
            "/addons/self/info", "/info", "/core/api/hassio/discovery",
            "/api/hassio/discover", "/discovery", "/api/hassio/discover",
            "/api/hassio/discovery", "/addons",
        ])
        self.assertTrue(all(item["authorization"] == "Bearer test-supervisor-token" for item in self.requests))
        self.assertTrue(any('"body": "[redacted]"' in line for line in logs.output))
        self.assertTrue(any('"message": "[redacted]"' in line for line in logs.output))
        self.assertNotIn("private-proxy-reason", "\n".join(logs.output))

    async def test_valid_core_proxy_discovery_is_used_and_logged_without_secret(self):
        self.admin_info()
        self.startup_probes()
        self.responses.append((401, "401: Unauthorized"))
        self.responses.append((401, "401: Unauthorized"))
        self.respond({"discovery": [{
            "addon": "local_litellm", "service": "litellm",
            "config": {"host": "local-litellm", "port": 4000, "master_key": "private-key"},
        }]})
        self.respond({"addons": [{"slug": "local_litellm", "state": "started"}]})
        with patch.object(self.supervisor, "_add_models", new=AsyncMock()):
            with self.assertLogs("supervisor", level="INFO") as logs:
                overlay = await self.supervisor.startup()
        self.assertEqual(overlay["provider"]["litellm"]["options"]["apiKey"], "private-key")
        self.assertNotIn("private-key", "\n".join(logs.output))
        self.assertTrue(any("Core proxy /api/hassio/discovery HTTP 200" in line for line in logs.output))

    async def test_disabled_discovery_does_not_read_litellm_options(self):
        self.admin_info()
        self.startup_probes()
        self.responses.append((401, "401: Unauthorized"))
        self.responses.append((401, "401: Unauthorized"))
        self.responses.append((401, "401: Unauthorized"))
        self.respond({"addons": [{"slug": "local_litellm", "state": "started"}]})
        with self.assertLogs("supervisor", level="WARNING"):
            self.assertEqual(await self.supervisor.startup(litellm_discovery=False), {})
        self.assertEqual(len(self.requests), 8)

    async def test_other_service_cannot_supply_litellm_credentials(self):
        self.admin_info()
        self.startup_probes()
        self.respond({"discovery": [{
            "addon": "different_litellm", "service": "litellm", "config": {"master_key": "private-key"},
        }], "services": {}})
        self.respond({"addons": [{"slug": "local_litellm", "state": "started"}]})
        with self.assertLogs("supervisor", level="WARNING"):
            self.assertEqual(await self.supervisor.startup(), {})

    async def test_read_models_uses_litellm_key_and_populates_config(self):
        overlay = litellm_overlay({"host": "local-litellm", "port": 4000, "master_key": "private-key"})
        overlay["provider"]["litellm"]["options"]["baseURL"] = str(self.server.make_url("/v1"))
        self.responses.append((200, {"data": [{"id": "model-a"}, {"id": "model-b"}, {"bad": "entry"}]}))
        await self.supervisor._add_models(overlay)
        self.assertEqual(overlay["provider"]["litellm"]["models"], {"model-a": {"name": "model-a"}, "model-b": {"name": "model-b"}})
        self.assertEqual(self.requests[-1]["authorization"], "Bearer private-key")
        self.assertEqual(self.requests[-1]["path"], "/v1/models")

    async def test_redirect_does_not_forward_supervisor_token(self):
        self.responses.append((302, "redirect-private-value"))
        with self.assertRaises(SupervisorError) as caught:
            await self.supervisor.request("GET", "/addons")
        self.assertNotIn("redirect-private-value", str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_invalid_response_data_is_rejected_without_values(self):
        self.responses.append((200, {"result": "ok", "data": ["private-value"]}))
        with self.assertRaises(SupervisorError) as caught:
            await self.supervisor.request("GET", "/addons")
        self.assertNotIn("private-value", str(caught.exception))


class ConfigurationTests(unittest.TestCase):
    def test_manifest_enables_core_api_for_version_0_0_4(self):
        manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
        self.assertEqual(manifest["version"], "0.0.4")
        self.assertTrue(manifest["hassio_api"])
        self.assertEqual(manifest["hassio_role"], "admin")
        self.assertTrue(manifest["homeassistant_api"])

    def test_redaction_removes_all_nonpublic_config_fields(self):
        data = {"discovery": [{"service": "litellm", "config": {
            "host": "local-litellm", "port": 4000, "master_key": "private-master-key",
            "password": "private-password", "other": {"value": "private-other-value"},
        }}], "services": {"litellm": ["local_litellm"]}, "private": "private-top-level"}
        encoded = json.dumps(redact_discovery(data))
        for secret in ("private-master-key", "private-password", "private-other-value", "private-top-level"):
            self.assertNotIn(secret, encoded)
        self.assertIn("local-litellm", encoded)
        self.assertEqual(data["discovery"][0]["config"]["master_key"], "private-master-key")

    def test_invalid_discovery_fields_are_rejected(self):
        valid = {"host": "local-litellm", "port": 4000, "path": "/v1", "master_key": "private-key"}
        for key, value in (("host", "other@host"), ("port", True), ("port", 65536), ("path", "/../v1"), ("master_key", "!secret key")):
            with self.subTest(key=key, value=value):
                with self.assertRaises(SupervisorError):
                    litellm_overlay({**valid, key: value})


if __name__ == "__main__":
    unittest.main()
