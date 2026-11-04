"""Check user isolation, ingress authentication, and proxy transfers."""

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

import main
from supervisor import SupervisorCoreOnly, SupervisorError


PREFIX = "/api/hassio_ingress/test-token"


class StoreTests(unittest.TestCase):
    def test_ids_survive_restart_and_repeated_requests_do_not_consume_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = main.UserStore(path)
            self.assertEqual(store.upsert("Alice"), (1, "Alice"))
            for _ in range(100):
                self.assertEqual(store.upsert("Alice"), (1, "Alice"))
            self.assertEqual(store.upsert("Bob"), (2, "Bob"))
            store.close()
            store = main.UserStore(path)
            self.assertEqual(store.all(), [(1, "Alice"), (2, "Bob")])
            self.assertEqual((path / "users.db").stat().st_mode & 0o777, 0o600)
            store.close()

    def test_directory_traversal_and_reserved_files_are_rejected(self):
        for name in ("", " ", ".", "..", "../Bob", "/Bob", "Alice/Bob", "a\\b", "users.db", "options.json", "a\nb"):
            with self.subTest(name=name), self.assertRaises(web.HTTPException):
                main.check_username(name)
        self.assertEqual(main.check_username("Jörg Example"), "Jörg Example")

    def test_home_and_environment_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            store = main.UserStore(Path(directory))
            manager = main.Instances(store, None, {"provider": {"test": {"options": {"apiKey": "model-key"}}}})
            home = manager.home(1, "Alice")
            env = manager.environment(1, "Alice", home, PREFIX, "https://ha.example", "backend-key")
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            self.assertEqual(env["HOME"], str(home))
            self.assertEqual(env["OPENCODE_PORT"], "8081")
            self.assertNotIn("SUPERVISOR_TOKEN", env)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["server"]["cors"], ["https://ha.example"])
            other = Path(directory) / "Bob"
            other.symlink_to(home, target_is_directory=True)
            with self.assertRaises(ValueError):
                manager.home(2, "Bob")
            store.close()

    def test_options_reject_non_booleans(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_text('{}')
            self.assertEqual(main.read_options(path), {"lazy_start": True, "litellm_discovery": True})
            path.write_text('{"lazy_start":"false"}')
            with self.assertRaises(ValueError):
                main.read_options(path)

    def test_mise_task_keeps_user_directory_without_landlock(self):
        config = tomllib.loads((main.APP_DIR / "mise.toml").read_text())
        task = config["tasks"]["opencode"]
        self.assertEqual(task["dir"], "{{ env.HOME }}")
        self.assertTrue(task["deny_env"])
        self.assertFalse({"deny_read", "deny_write", "allow_read", "allow_write"} & task.keys())


class GatewayLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_routine_ingress_requests_are_not_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "options.json").write_text("{}")
            supervisor = SimpleNamespace(startup=AsyncMock(return_value={}),
                                         validate_session=AsyncMock(return_value=True))
            managers = []

            def make_manager(store, *_args):
                manager = SimpleNamespace(
                    store=store, seen_users={},
                    ensure=AsyncMock(return_value=SimpleNamespace(port=8081, password="backend-key")),
                    close=AsyncMock(),
                )
                managers.append(manager)
                return manager

            with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "gateway-only"}), \
                    patch.object(main, "Supervisor", return_value=supervisor), \
                    patch.object(main, "Instances", side_effect=make_manager), \
                    patch.object(main, "TRUSTED_INGRESS", ipaddress.ip_address("127.0.0.1")):
                app = await main.create_app(path / "options.json", path)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    headers = {
                        "X-Remote-User-Name": "Alice", "X-Remote-User-Id": "ha-user-id",
                        "X-Ingress-Path": PREFIX, "X-Forwarded-Proto": "https",
                        "X-Forwarded-Host": "ha.example", "Cookie": "ingress_session=one",
                    }
                    with self.assertNoLogs("opencode", level="INFO"):
                        accepted = await client.get(PREFIX, headers=headers, allow_redirects=False)
                        rejected = await client.get(PREFIX, headers={**headers, "X-Remote-User-Name": ""},
                                                    allow_redirects=False)
                    self.assertEqual((accepted.status, rejected.status), (308, 500))
                    self.assertEqual(managers[0].ensure.await_count, 1)
                finally:
                    await client.close()


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_arms_only_after_first_request_and_recovers(self):
        monitor = main.WatchdogMonitor()
        self.addCleanup(monitor.close)
        with self.assertNoLogs("opencode", level="WARNING"):
            monitor.missed()
        self.assertIsNone(monitor.timer)
        with self.assertLogs("opencode", level="INFO") as received:
            monitor.received()
        self.assertIn("watchdog request received", received.output[0])
        self.assertAlmostEqual(monitor.timer.when() - asyncio.get_running_loop().time(), 300, delta=1)
        with self.assertLogs("opencode", level="WARNING") as missed:
            monitor.missed()
            monitor.missed()
        self.assertEqual(len(missed.output), 1)
        with self.assertLogs("opencode", level="INFO") as resumed:
            monitor.received()
        self.assertTrue(any("resumed" in line for line in resumed.output))
        self.assertFalse(monitor.warned)

    async def test_watchdog_request_never_creates_a_user(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "options.json").write_text("{}")
            supervisor = SimpleNamespace(startup=AsyncMock(return_value={}))
            manager = SimpleNamespace(ensure=AsyncMock(), close=AsyncMock())
            with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "gateway-only"}), \
                    patch.object(main, "Supervisor", return_value=supervisor), \
                    patch.object(main, "Instances", return_value=manager), \
                    patch.object(main, "TRUSTED_INGRESS", ipaddress.ip_address("127.0.0.1")):
                app = await main.create_app(path / "options.json", path)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    with self.assertLogs("opencode", level="INFO") as logs:
                        response = await client.get(main.WATCHDOG_PATH)
                    self.assertEqual(response.status, 200)
                    self.assertTrue(any("watchdog request received" in line for line in logs.output))
                    self.assertFalse(any("X-Remote-User-Id" in line for line in logs.output))
                    store = main.UserStore(path)
                    try:
                        self.assertEqual(store.all(), [])
                    finally:
                        store.close()
                    manager.ensure.assert_not_awaited()
                    blocked = await client.get(main.WATCHDOG_PATH, headers={"X-Ingress-Path": PREFIX})
                    self.assertEqual(blocked.status, 403)
                    with patch.object(main, "TRUSTED_INGRESS", ipaddress.ip_address("172.30.32.2")):
                        direct = await client.get(main.WATCHDOG_PATH)
                    self.assertEqual(direct.status, 403)
                    wrong_method = await client.post(main.WATCHDOG_PATH)
                    self.assertEqual(wrong_method.status, 405)
                finally:
                    await client.close()


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = main.UserStore(self.root)
        self.addCleanup(self.store.close)
        self.http_client = aiohttp.ClientSession(auto_decompress=False)
        self.addAsyncCleanup(self.http_client.close)
        self.supervisor = SimpleNamespace(validate_session=AsyncMock(return_value=True))
        self.ensure = AsyncMock()
        self.manager = SimpleNamespace(store=self.store, ensure=self.ensure, seen_users={})
        self.gateway = main.Gateway(self.supervisor, self.manager, self.http_client, self.root / "web")
        app = web.Application(client_max_size=0)
        app.router.add_route("*", "/{path:.*}", self.gateway.handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.peer = patch.object(main, "TRUSTED_INGRESS", ipaddress.ip_address("127.0.0.1"))
        self.peer.start()
        self.addCleanup(self.peer.stop)
        self.headers = {"X-Remote-User-Name": "Alice", "X-Ingress-Path": PREFIX,
                        "X-Forwarded-Proto": "https", "X-Forwarded-Host": "ha.example",
                        "Cookie": "ingress_session=valid-session"}

    async def upstream(self, handler):
        app = web.Application(client_max_size=0)
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        self.ensure.return_value = SimpleNamespace(port=server.port, password="private-backend-password")
        return server

    async def test_spoofed_headers_from_direct_port_are_rejected(self):
        with patch.object(main, "TRUSTED_INGRESS", ipaddress.ip_address("172.30.32.2")):
            response = await self.client.get("/", headers={**self.headers, "X-Forwarded-For": "172.30.32.2"})
        self.assertEqual(response.status, 403)
        self.supervisor.validate_session.assert_not_awaited()
        self.assertEqual(self.store.all(), [])

    async def test_empty_username_returns_500(self):
        response = await self.client.get("/", headers={**self.headers, "X-Remote-User-Name": ""})
        self.assertEqual(response.status, 500)
        self.assertIn("X-Remote-User-Name", await response.text())
        self.ensure.assert_not_awaited()

    async def test_missing_session_and_invalid_prefix_do_not_create_users(self):
        for changes, expected in (({"Cookie": ""}, 401), ({"X-Ingress-Path": "/other"}, 400)):
            with self.subTest(changes=changes):
                response = await self.client.get("/", headers={**self.headers, **changes})
                self.assertEqual(response.status, expected)
        self.assertEqual(self.store.all(), [])
        self.ensure.assert_not_awaited()

    async def test_invalid_session_and_failed_checks_do_not_start_a_process(self):
        for result, expected in ((False, 401), (SupervisorError("unavailable"), 503)):
            with self.subTest(result=result):
                self.supervisor.validate_session.side_effect = result if isinstance(result, Exception) else None
                self.supervisor.validate_session.return_value = result
                response = await self.client.get("/", headers=self.headers)
                self.assertEqual(response.status, expected)
        self.ensure.assert_not_awaited()
        self.assertEqual(self.store.all(), [])

    async def test_known_core_only_restriction_uses_trusted_ingress(self):
        self.supervisor.validate_session.side_effect = SupervisorCoreOnly("Core only")
        async def health(request):
            return web.Response(text="ok")
        await self.upstream(health)
        response = await self.client.get("/global/health", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.manager.seen_users, {"Alice": 1})

    async def test_large_upload_download_and_header_filter(self):
        payload = b"0123456789abcdef" * (512 * 1024)
        received = {}

        async def echo(request):
            received["path"] = request.raw_path
            received["headers"] = request.headers
            body = bytearray()
            async for chunk in request.content.iter_chunked(65536):
                body.extend(chunk)
            return web.Response(body=body, headers={"Set-Cookie": "private=secret", "X-Result": "ok"})

        await self.upstream(echo)

        async def chunks():
            for offset in range(0, len(payload), 65536):
                yield payload[offset:offset + 65536]

        response = await self.client.post(URL(PREFIX + "/upload?name=a%2Fb&value=%25", encoded=True), data=chunks(),
                                         headers={**self.headers, "Authorization": "Bearer unwanted"})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), payload)
        self.assertEqual(received["path"], "/upload?name=a%2Fb&value=%25")
        self.assertEqual(received["headers"]["Authorization"], aiohttp.encode_basic_auth("opencode", "private-backend-password"))
        self.assertNotIn("Cookie", received["headers"])
        self.assertNotIn("X-Remote-User-Name", received["headers"])
        self.assertNotIn("Set-Cookie", response.headers)

    async def test_event_stream_is_not_buffered(self):
        release = asyncio.Event()

        async def events(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            await release.wait()
            await response.write(b"data: second\n\n")
            return response

        await self.upstream(events)
        response = await self.client.get("/event", headers=self.headers)
        self.assertEqual(await asyncio.wait_for(response.content.readexactly(13), 2), b"data: first\n\n")
        release.set()
        self.assertEqual(await response.read(), b"data: second\n\n")

    async def test_websocket_text_binary_and_subprotocol(self):
        async def echo(request):
            ws = web.WebSocketResponse(protocols=["test"], max_msg_size=0)
            await ws.prepare(request)
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_str(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(message.data)
            return ws

        await self.upstream(echo)
        async with self.client.ws_connect("/pty/1/connect", headers=self.headers, protocols=["test"], max_msg_size=0) as ws:
            self.assertEqual(ws.protocol, "test")
            await ws.send_str("hello")
            self.assertEqual((await ws.receive()).data, "hello")
            payload = b"x" * (6 * 1024 * 1024)
            await ws.send_bytes(payload)
            self.assertEqual((await ws.receive()).data, payload)

    async def test_failed_browser_upgrade_closes_backend_websocket(self):
        upstream = SimpleNamespace(protocol=None, close=AsyncMock())
        downstream = SimpleNamespace(prepare=AsyncMock(side_effect=ConnectionResetError()))
        request = SimpleNamespace(headers={})
        instance = SimpleNamespace(password="private")
        with patch.object(self.http_client, "ws_connect", AsyncMock(return_value=upstream)), \
                patch.object(main.web, "WebSocketResponse", return_value=downstream):
            with self.assertRaises(ConnectionResetError):
                await self.gateway.websocket(request, "http://127.0.0.1:8081/pty", {}, instance, "session")
        upstream.close.assert_awaited_once()

    async def test_static_frontend_is_authenticated_and_uses_prefix(self):
        self.gateway.web_dir.mkdir()
        (self.gateway.web_dir / "index.html").write_text('<html><head></head><body><div id="root"></div></body></html>')
        self.ensure.return_value = SimpleNamespace(port=8081, password="private")
        response = await self.client.get("/", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertIn(PREFIX, await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-store")


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_different_sessions_and_urls_share_one_user_process(self):
        with tempfile.TemporaryDirectory() as directory:
            store = main.UserStore(Path(directory))
            self.addCleanup(store.close)
            async with aiohttp.ClientSession() as upstream:
                manager = main.Instances(store, upstream, {})
                manager.ready = AsyncMock()
                manager.stop = AsyncMock()
                supervisor = SimpleNamespace(validate_session=AsyncMock(return_value=True))
                gateway = main.Gateway(supervisor, manager, upstream)
                app = web.Application()
                app.router.add_route("*", "/{path:.*}", gateway.handle)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    def request_headers(prefix, host, session):
                        return {
                            "X-Remote-User-Name": "Alice", "X-Remote-User-Id": "ha-user-id",
                            "X-Ingress-Path": prefix, "X-Forwarded-Proto": "https",
                            "X-Forwarded-Host": host, "Cookie": f"ingress_session={session}",
                        }

                    process = SimpleNamespace(returncode=None, pid=999)
                    with patch.object(main, "TRUSTED_INGRESS", ipaddress.ip_address("127.0.0.1")), \
                            patch.object(main.asyncio, "create_subprocess_exec",
                                         AsyncMock(return_value=process)) as start:
                        paths = (PREFIX, PREFIX + "-other")
                        responses = await asyncio.gather(
                            client.get(paths[0], headers=request_headers(paths[0], "ha.example", "session-a"),
                                       allow_redirects=False),
                            client.get(paths[1], headers=request_headers(paths[1], "other.example", "session-b"),
                                       allow_redirects=False),
                        )
                    self.assertEqual([response.status for response in responses], [308, 308])
                    self.assertEqual([response.headers["Location"] for response in responses],
                                     [path + "/" for path in paths])
                    self.assertEqual([call.args[0] for call in supervisor.validate_session.await_args_list],
                                     ["session-a", "session-b"])
                    self.assertEqual(store.all(), [(1, "Alice")])
                    self.assertEqual(start.await_count, 1)
                    self.assertEqual(list(manager.running), [1])
                    manager.stop.assert_not_awaited()
                finally:
                    await client.close()

    async def test_concurrent_requests_reuse_one_process_across_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = main.UserStore(Path(directory))
            self.addCleanup(store.close)
            manager = main.Instances(store, None, {})
            manager.ready = AsyncMock()
            manager.stop = AsyncMock()
            process = SimpleNamespace(returncode=None, pid=999)
            with patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as start:
                first, second = await asyncio.gather(
                    manager.ensure(1, "Alice", PREFIX, "https://ha.example"),
                    manager.ensure(1, "Alice", PREFIX, "https://ha.example"),
                )
                self.assertIs(first, second)
                self.assertEqual(start.await_count, 1)
                self.assertEqual(start.call_args.args, ("mise", "-C", str(main.APP_DIR), "run", "opencode"))
                self.assertNotIn("SUPERVISOR_TOKEN", start.call_args.kwargs["env"])
                third = await manager.ensure(1, "Alice", PREFIX + "-new", "https://other.example")
                self.assertIs(third, first)
                self.assertEqual(start.await_count, 1)
                manager.stop.assert_not_awaited()

    async def test_exited_process_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            store = main.UserStore(Path(directory))
            self.addCleanup(store.close)
            manager = main.Instances(store, None, {})
            manager.ready = AsyncMock()
            manager.stop = AsyncMock()
            old_process = SimpleNamespace(returncode=None, pid=999)
            new_process = SimpleNamespace(returncode=None, pid=1000)
            with patch.object(main.asyncio, "create_subprocess_exec",
                              AsyncMock(side_effect=[old_process, new_process])) as start:
                first = await manager.ensure(1, "Alice", PREFIX, "https://ha.example")
                old_process.returncode = 0
                second, third = await asyncio.gather(
                    manager.ensure(1, "Alice", PREFIX + "-new", "https://other.example"),
                    manager.ensure(1, "Alice", PREFIX, "https://ha.example"),
                )
            self.assertIs(second, third)
            self.assertIsNot(second, first)
            self.assertEqual(start.await_count, 2)
            self.assertIs(manager.running[1], second)
            manager.stop.assert_not_awaited()

    async def test_failed_start_is_stopped_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = main.UserStore(Path(directory))
            self.addCleanup(store.close)
            manager = main.Instances(store, None, {})
            manager.ready = AsyncMock(side_effect=RuntimeError("not ready"))
            manager.stop = AsyncMock()
            with patch.object(main.asyncio, "create_subprocess_exec", AsyncMock(return_value=SimpleNamespace(returncode=None, pid=999))):
                with self.assertRaises(RuntimeError):
                    await manager.ensure(1, "Alice", PREFIX, "https://ha.example")
            manager.stop.assert_awaited_once()
            self.assertEqual(manager.running, {})


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_and_eager_start_use_saved_users(self):
        for lazy in (True, False):
            with self.subTest(lazy=lazy), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                store = main.UserStore(path)
                store.upsert("Alice")
                store.upsert("Bob")
                store.close()
                options = path / "options.json"
                options.write_text(json.dumps({"lazy_start": lazy, "litellm_discovery": False}))
                supervisor = SimpleNamespace(startup=AsyncMock(return_value={}))
                manager = SimpleNamespace(ensure=AsyncMock(), close=AsyncMock())
                with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "gateway-only"}), \
                        patch.object(main, "Supervisor", return_value=supervisor), \
                        patch.object(main, "Instances", return_value=manager):
                    app = await main.create_app(options, path)
                    client = TestClient(TestServer(app))
                    await client.start_server()
                    try:
                        supervisor.startup.assert_awaited_once_with(False)
                        self.assertNotIn("SUPERVISOR_TOKEN", os.environ)
                        self.assertEqual(manager.ensure.await_count, 0 if lazy else 2)
                        if not lazy:
                            self.assertEqual(manager.ensure.await_args_list[0].args, (1, "Alice", "", ""))
                            self.assertEqual(manager.ensure.await_args_list[1].args, (2, "Bob", "", ""))
                    finally:
                        await client.close()
                    manager.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
