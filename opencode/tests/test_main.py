"""Check user isolation, ingress authentication, and proxy transfers."""

import asyncio
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
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
import frontend
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
            manager = main.Instances(store, None, {"provider": {"litellm": {
                "options": {"baseURL": "http://discovered:4000/v1", "apiKey": "model-key"},
                "models": {"model-a": {"name": "Model A"}},
            }}})
            home = manager.home(1, "Alice")
            env = manager.environment(1, "Alice", home, PREFIX, "https://ha.example", "backend-key")
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            self.assertEqual(env["HOME"], str(home))
            self.assertEqual(env["OPENCODE_PORT"], "8081")
            self.assertNotIn("SUPERVISOR_TOKEN", env)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(config["server"]["cors"], ["https://ha.example"])
            provider = config["provider"]["litellm"]
            self.assertEqual(provider["name"], "homeassistant-litellm")
            self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
            self.assertEqual(provider["options"], {"baseURL": "http://aca25ede-litellm:4000/inference/v1"})
            self.assertNotIn("apiKey", provider["options"])
            self.assertEqual(provider["models"], {"model-a": {"name": "Model A"}})
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


class FrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required to run the ingress script")
    def test_worker_urls_use_each_page_ingress_path(self):
        harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const source = require("node:fs").readFileSync(0, "utf8");
for (const token of ["first-token", "second-token"]) {
  const prefix = "/api/hassio_ingress/" + token;
  const seen = [];
  class NativeWorker {
    constructor(...args) { seen.push(args); }
  }
  class Link {}
  Object.defineProperty(Link.prototype, "href", {
    configurable: true, get() { return this.value; }, set(value) { this.value = value; }
  });
  const context = {
    window: { Worker: NativeWorker, fetch() {} },
    document: { baseURI: `https://ha.example${prefix}/`, addEventListener() {} },
    location: { host: "ha.example", origin: "https://ha.example" },
    URL, Request, XMLHttpRequest: class { open() {} }, HTMLLinkElement: Link,
    CSSRule: { FONT_FACE_RULE: 5 }
  };
  vm.runInNewContext(source, context);
  const options = { type: "module", name: "markdown" };
  const worker = new context.window.Worker(new URL("/_assets/markdown.worker.js", context.document.baseURI), options);
  assert.ok(worker instanceof NativeWorker);
  assert.equal(seen[0][0], `https://ha.example${prefix}/_assets/markdown.worker.js`);
  assert.equal(seen[0][1], options);
  new context.window.Worker(`${prefix}/_assets/ready.js`, options);
  assert.equal(seen[1][0], `${prefix}/_assets/ready.js`);
  for (const url of ["https://other.example/worker.js", "blob:https://ha.example/abc", "data:text/javascript,0"]) {
    new context.window.Worker(url, options);
    assert.equal(seen.at(-1)[0], url);
  }
  new context.window.Worker("/_assets/plain.js");
  assert.equal(seen.at(-1).length, 1);
  assert.equal(seen.at(-1)[0], `https://ha.example${prefix}/_assets/plain.js`);
}
"""
        result = subprocess.run(["node", "-e", harness], input=frontend.INGRESS_SCRIPT,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_bundle_replacement_count_must_match(self):
        path = "/_assets/current-CS21lgrW.js"
        for original, replacement in frontend._BUNDLES[path][1]:
            for body in (b"no route anchor", original + original):
                with self.subTest(anchor=original, body=body):
                    checked = {path: (hashlib.sha256(body).hexdigest(), ((original, replacement),))}
                    with patch.dict(frontend._BUNDLES, checked):
                        with self.assertRaisesRegex(ValueError, "structure changed"):
                            frontend.adapt_bundle(path, body)

    def test_queue_adapter_passes_original_fetch_arguments(self):
        path = "/_assets/current-CS21lgrW.js"
        anchors = frontend._BUNDLES[path][1]
        body = b";".join(original for original, _ in anchors)
        checked = {path: (hashlib.sha256(body).hexdigest(), anchors)}
        with patch.dict(frontend._BUNDLES, checked):
            adapted = frontend.adapt_bundle(path, body)
        self.assertIn(b"o(e,{...t,signal:p.signal})", adapted)
        self.assertNotIn(b"o(new Request(n,{signal:p.signal}))", adapted)


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
                    app[main.GATEWAY_KEY].http = AsyncMock(side_effect=lambda *_: web.Response(text="ok"))
                    headers = {
                        "X-Remote-User-Name": "Alice", "X-Remote-User-Id": "ha-user-id",
                        "X-Ingress-Path": PREFIX, "X-Forwarded-Proto": "https",
                        "X-Forwarded-Host": "ha.example", "Cookie": "ingress_session=one",
                    }
                    with self.assertNoLogs("opencode", level="INFO"):
                        accepted = await client.get(PREFIX, headers=headers, allow_redirects=False)
                        rejected = await client.get(PREFIX, headers={**headers, "X-Remote-User-Name": ""},
                                                    allow_redirects=False)
                    self.assertEqual((accepted.status, rejected.status), (200, 500))
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
        self.gateway = main.Gateway(self.supervisor, self.manager, self.http_client)
        app = web.Application(client_max_size=0)
        app.router.add_route("*", "/{path:.*}", self.gateway.handle)
        self.client = TestClient(TestServer(app), auto_decompress=False)
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
        self.assertEqual(response.headers["Set-Cookie"], "private=secret")

    async def test_native_ui_uses_each_ingress_path_and_assets_stream(self):
        html = (b'<html><head></head><body><script src="/assets/app.js"></script>'
                b'<a href="/settings">Settings</a><form action="/submit"></form>'
                b'<video poster="/poster.png"></video>'
                b'<a href="https://docs.example/help">Help</a>'
                b'<script src="//cdn.example/app.js"></script>'
                b'<a href="#section">Section</a></body></html>')
        asset = gzip.compress(b"export const app = true;")
        received = []

        async def native_ui(request):
            received.append((request.raw_path, request.headers.get("Accept-Encoding")))
            if request.path == "/":
                return web.Response(body=html, content_type="text/html",
                                    headers={"Cache-Control": "public, max-age=60", "ETag": '"original"'})
            return web.Response(body=asset, content_type="application/javascript",
                                headers={"Content-Encoding": "gzip", "X-Asset": "original"})

        await self.upstream(native_ui)
        page = await self.client.get(URL(PREFIX + "?source=a%2Fb", encoded=True),
                                     headers={**self.headers, "Accept-Encoding": "gzip"})
        self.assertEqual(page.status, 200)
        expected = (f'<html><head><base href="{PREFIX}/">'
                    f'<script src="{PREFIX}{frontend.SHIM_PATH}"></script></head><body>'
                    f'<script src="{PREFIX}/assets/app.js"></script>'
                    f'<a href="{PREFIX}/settings">Settings</a>'
                    f'<form action="{PREFIX}/submit"></form>'
                    f'<video poster="{PREFIX}/poster.png"></video>'
                    '<a href="https://docs.example/help">Help</a>'
                    '<script src="//cdn.example/app.js"></script>'
                    '<a href="#section">Section</a></body></html>').encode()
        self.assertEqual(await page.read(), expected)
        self.assertEqual(page.headers["Cache-Control"], "private, no-store")
        self.assertNotIn("ETag", page.headers)

        other_prefix = "/api/hassio_ingress/other-token"
        other = await self.client.get(other_prefix, headers={**self.headers, "X-Ingress-Path": other_prefix})
        self.assertEqual(other.status, 200)
        self.assertIn(f'<base href="{other_prefix}/">', await other.text())
        self.assertIn(f'src="{other_prefix}{frontend.SHIM_PATH}"', await other.text())
        self.assertIn(f'src="{other_prefix}/assets/app.js"', await other.text())

        script = await self.client.get(PREFIX + "/assets/app.js?tag=%25",
                                       headers={**self.headers, "Accept-Encoding": "gzip"})
        self.assertEqual(script.status, 200)
        self.assertEqual(await script.read(), asset)
        self.assertEqual(script.headers["Content-Encoding"], "gzip")
        self.assertEqual(script.headers["X-Asset"], "original")
        self.assertEqual(received, [("/?source=a%2Fb", "identity"), ("/", "identity"),
                                    ("/assets/app.js?tag=%25", "identity")])

    async def test_shim_requires_ingress_authentication_and_has_no_user_state(self):
        path = PREFIX + frontend.SHIM_PATH
        with patch.object(main, "TRUSTED_INGRESS", ipaddress.ip_address("172.30.32.2")):
            denied = await self.client.get(path, headers=self.headers)
        self.assertEqual(denied.status, 403)
        self.supervisor.validate_session.return_value = False
        invalid = await self.client.get(path, headers=self.headers)
        self.assertEqual(invalid.status, 401)
        self.supervisor.validate_session.return_value = True
        accepted = await self.client.get(path, headers=self.headers)
        self.assertEqual(accepted.status, 200)
        self.assertEqual(await accepted.read(), frontend.INGRESS_SCRIPT)
        self.assertEqual(accepted.headers["Cache-Control"], "private, no-store")
        self.assertEqual(self.store.all(), [])
        self.ensure.assert_not_awaited()

    async def test_markdown_worker_script_is_proxied_as_javascript(self):
        source = b"self.onmessage = event => self.postMessage(event.data);"
        received = []

        async def worker(request):
            received.append(request.raw_path)
            return web.Response(body=source, content_type="text/javascript")

        await self.upstream(worker)
        for prefix in (PREFIX, "/api/hassio_ingress/other-token"):
            with self.subTest(prefix=prefix):
                response = await self.client.get(prefix + "/_assets/markdown.worker.js",
                                                 headers={**self.headers, "X-Ingress-Path": prefix})
                self.assertEqual(response.status, 200)
                self.assertEqual(response.content_type, "text/javascript")
                self.assertEqual(await response.read(), source)
        self.assertEqual(received, ["/_assets/markdown.worker.js"] * 2)

    async def test_checked_ui_bundles_change_only_after_hash_and_anchor_checks(self):
        fixture = b"get component(){return e.router??ss},root:t,navigator.serviceWorker.register(`/sw.js`)"
        path = "/_assets/index-BDq5uAY5.js"
        expected_replacements = frontend._BUNDLES[path][1]
        checks = {path: (hashlib.sha256(fixture).hexdigest(), expected_replacements)}

        async def assets(request):
            if request.path == path:
                return web.Response(body=fixture, content_type="text/javascript",
                                    headers={"ETag": '"original"', "Cache-Control": "public, max-age=60"})
            return web.Response(body=b"other asset", content_type="text/javascript")

        await self.upstream(assets)
        with patch.dict(frontend._BUNDLES, checks):
            adapted = await self.client.get(PREFIX + path, headers=self.headers)
            self.assertEqual(adapted.status, 200)
            self.assertIn(b"get base(){return new URL(document.baseURI)", await adapted.read())
            self.assertEqual(adapted.headers["Cache-Control"], "private, no-store")
            self.assertNotIn("ETag", adapted.headers)
            self.assertNotIn("Last-Modified", adapted.headers)
        mismatch = await self.client.get(PREFIX + path, headers=self.headers)
        self.assertEqual(mismatch.status, 502)
        self.assertIn("hash changed", await mismatch.text())
        unknown = await self.client.get(PREFIX + "/_assets/index-new.js", headers=self.headers)
        self.assertEqual(unknown.status, 502)
        self.assertIn("bundle name", await unknown.text())
        other = await self.client.get(PREFIX + "/_assets/current-tool-state-Brzthqsm.js",
                                      headers=self.headers)
        self.assertEqual(other.status, 200)
        self.assertEqual(await other.read(), b"other asset")

    async def test_html_rejects_an_unknown_ui_entry(self):
        async def page(request):
            return web.Response(body=b'<html><head><script type="module" src="/_assets/index-new.js"></script></head></html>',
                                content_type="text/html")

        await self.upstream(page)
        response = await self.client.get(PREFIX, headers=self.headers)
        self.assertEqual(response.status, 502)
        self.assertIn("entry bundle changed", await response.text())

    async def test_app_redirects_use_ingress_path_and_external_urls_stay(self):
        async def redirect(request):
            if request.path == "/loopback":
                location = f"http://127.0.0.1:{server.port}/session?draft=%25#editor"
            elif request.path == "/public":
                location = "https://ha.example/settings"
            elif request.path == "/external":
                location = "https://docs.example/help"
            elif request.path == "/already-prefixed":
                location = PREFIX + "/login"
            else:
                location = "/login?next=%2Fsession#form"
            return web.Response(status=302, headers={
                "Location": location,
                "WWW-Authenticate": 'Basic realm="OpenCode"',
            })

        server = await self.upstream(redirect)
        response = await self.client.get(PREFIX + "/go", headers=self.headers, allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], PREFIX + "/login?next=%2Fsession#form")
        self.assertEqual(response.headers["WWW-Authenticate"], 'Basic realm="OpenCode"')
        other_prefix = "/api/hassio_ingress/other-token"
        other = await self.client.get(other_prefix + "/go",
                                      headers={**self.headers, "X-Ingress-Path": other_prefix},
                                      allow_redirects=False)
        self.assertEqual(other.headers["Location"], other_prefix + "/login?next=%2Fsession#form")
        loopback = await self.client.get(PREFIX + "/loopback", headers=self.headers, allow_redirects=False)
        self.assertEqual(loopback.headers["Location"],
                         "https://ha.example" + PREFIX + "/session?draft=%25#editor")
        public = await self.client.get(PREFIX + "/public", headers=self.headers, allow_redirects=False)
        self.assertEqual(public.headers["Location"], "https://ha.example" + PREFIX + "/settings")
        external = await self.client.get(PREFIX + "/external", headers=self.headers, allow_redirects=False)
        self.assertEqual(external.headers["Location"], "https://docs.example/help")
        already = await self.client.get(PREFIX + "/already-prefixed", headers=self.headers,
                                        allow_redirects=False)
        self.assertEqual(already.headers["Location"], PREFIX + "/login")

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
                                         AsyncMock(return_value=process)) as start, \
                            patch.object(gateway, "http", AsyncMock(side_effect=lambda *_: web.Response(text="ok"))):
                        paths = (PREFIX, PREFIX + "-other")
                        responses = await asyncio.gather(
                            client.get(paths[0], headers=request_headers(paths[0], "ha.example", "session-a"),
                                       allow_redirects=False),
                            client.get(paths[1], headers=request_headers(paths[1], "other.example", "session-b"),
                                       allow_redirects=False),
                        )
                    self.assertEqual([response.status for response in responses], [200, 200])
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
