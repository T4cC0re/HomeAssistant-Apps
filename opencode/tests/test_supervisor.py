"""Check Supervisor access and LiteLLM settings."""
from collections import deque
from pathlib import Path
import unittest
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import yaml
from supervisor import Supervisor, SupervisorCoreOnly, SupervisorError, SupervisorForbidden, litellm_connection


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses, self.requests = deque(), []
        async def handle(request):
            self.requests.append((request.method, request.path, request.headers.get("Authorization")))
            status, body = self.responses.popleft()
            return web.Response(status=status, text=body) if isinstance(body, str) else web.json_response(body, status=status)
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.session = aiohttp.ClientSession(trust_env=False)
        self.supervisor = Supervisor(self.session, "test-supervisor-token", str(self.server.make_url("")))
        self.addAsyncCleanup(self.server.close)
        self.addAsyncCleanup(self.session.close)

    def respond(self, data=None):
        self.responses.append((200, {"result": "ok", "data": data}))

    async def test_session_validation_and_fallback(self):
        self.respond({"addons": []})
        with self.assertLogs("supervisor", level="WARNING"):
            await self.supervisor.startup()
        self.respond()
        self.assertTrue(await self.supervisor.validate_session("valid"))
        self.responses.append((401, {"result": "error"}))
        self.assertFalse(await self.supervisor.validate_session("invalid"))
        self.assertFalse(await self.supervisor.validate_session(""))
        self.responses.append((401, "401: Unauthorized"))
        with self.assertRaises(SupervisorCoreOnly):
            await self.supervisor.validate_session("valid")
        self.responses.append((403, "403: Forbidden"))
        with self.assertRaises(SupervisorCoreOnly):
            await self.supervisor.validate_session("valid")
        self.responses.append((403, "different denial"))
        with self.assertRaises(SupervisorForbidden):
            await self.supervisor.validate_session("valid")

    async def test_unverified_access_fails_closed(self):
        self.responses.append((401, "401: Unauthorized"))
        with self.assertRaises(SupervisorError) as caught:
            await self.supervisor.validate_session("valid")
        self.assertNotIsInstance(caught.exception, SupervisorCoreOnly)

    async def test_startup_reads_addon_information(self):
        self.respond({"addons": [{"slug": "local_litellm"}]})
        self.respond({"hostname": "local-litellm", "options": {"server_root_path": "/inference/v1", "master_key": "private-key"}})
        result = await self.supervisor.startup()
        self.assertEqual(result, {"base_url": "http://local-litellm:4000/inference/v1", "master_key": "private-key"})
        self.assertEqual([request[1] for request in self.requests], ["/addons", "/addons/local_litellm/info"])
        self.assertTrue(all(request[2] == "Bearer test-supervisor-token" for request in self.requests))

    async def test_missing_ambiguous_and_disabled(self):
        for matches, enabled in (([], True), ([{"slug": "local_litellm"}, {"slug": "abc_litellm"}], True), ([{"slug": "local_litellm"}], False)):
            with self.subTest(matches=matches, enabled=enabled):
                self.requests.clear()
                self.respond({"addons": matches})
                self.assertEqual(await self.supervisor.startup(enabled), {})
                self.assertEqual([x[1] for x in self.requests], ["/addons"])

    async def test_failure_does_not_stop_startup_or_log_response(self):
        for response in ((403, "private-key"), (200, {"result": "ok", "data": {"addons": "private-key"}})):
            self.responses.append(response)
            with self.assertLogs("supervisor", level="WARNING") as logs:
                self.assertEqual(await self.supervisor.startup(), {})
            self.assertNotIn("private-key", " ".join(logs.output))
        self.respond({"addons": [{"slug": "local_litellm"}]})
        self.respond({"hostname": "local-litellm", "options": {"master_key": "private-key", "server_root_path": "/../private"}})
        with self.assertLogs("supervisor", level="WARNING") as logs:
            self.assertEqual(await self.supervisor.startup(), {})
        self.assertNotIn("private-key", " ".join(logs.output))

    async def test_redirect_and_invalid_data_are_rejected(self):
        for response in ((302, "private-value"), (200, {"result": "ok", "data": ["private-value"]})):
            self.responses.append(response)
            with self.assertRaises(SupervisorError) as caught:
                await self.supervisor.request("GET", "/addons")
            self.assertNotIn("private-value", str(caught.exception))


class ConfigurationTests(unittest.TestCase):
    def test_manifest_permissions(self):
        manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text())
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertTrue(manifest["hassio_api"])
        self.assertEqual(manifest["hassio_role"], "manager")
        self.assertFalse(manifest.get("homeassistant_api", False))

    def test_root_paths_are_preserved(self):
        for path in ("", "/", "/v1", "/api/v1"):
            info = {"hostname": "local-litellm", "options": {"server_root_path": path, "master_key": "private-key"}}
            self.assertEqual(litellm_connection(info)["base_url"], "http://local-litellm:4000" + path)

    def test_invalid_fields_are_rejected(self):
        valid = {"hostname": "local-litellm", "options": {"server_root_path": "/v1", "master_key": "private-key"}}
        invalid = [{**valid, "hostname": "user@host"}, {"hostname": "local-litellm", "options": None}]
        invalid += [{**valid, "options": {**valid["options"], k: v}} for k, v in (("server_root_path", "/../v1"), ("server_root_path", "/v1/"), ("master_key", ""), ("master_key", "os.environ/KEY"))]
        for info in invalid:
            with self.subTest(info=info), self.assertRaises(SupervisorError):
                litellm_connection(info)
