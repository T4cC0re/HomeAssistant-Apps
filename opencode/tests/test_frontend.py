"""Check the ingress URL configuration for the web interface."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from frontend import MAX_HTML_BYTES, transform_html
from prepare_frontend import LOCATION_FILES, VERSION, prepare_frontend


HTML = b'<html><head><script type="module" src="./assets/main.js"></script><link href="/favicon.png"></head><body></body></html>'
PREFIX = "/api/hassio_ingress/session-token"


class FrontendTests(unittest.TestCase):
    def test_config_precedes_module_and_keeps_relative_assets(self):
        html = transform_html(HTML, PREFIX, "https://ha.example:8123" + PREFIX).decode()
        self.assertLess(html.index('id="opencode-ingress"'), html.index('type="module"'))
        self.assertIn('<base href="' + PREFIX + '/">', html)
        self.assertIn('href="' + PREFIX + '/favicon.png"', html)
        self.assertIn('src="./assets/main.js"', html)
        self.assertIn('"url":"https://ha.example:8123' + PREFIX + '"', html)

    def test_external_urls_and_existing_prefix_are_kept(self):
        body = b'<html><head><link href="//cdn.example/font"><link href="https://cdn.example/style"><link href="/api/hassio_ingress/session-token/icon"></head></html>'
        html = transform_html(body, PREFIX, "https://ha.example").decode()
        self.assertIn('href="//cdn.example/font"', html)
        self.assertIn('href="https://cdn.example/style"', html)
        self.assertIn('href="' + PREFIX + '/icon"', html)
        self.assertNotIn(PREFIX + PREFIX, html)

    def test_replaces_existing_base_and_supports_direct_root(self):
        body = b'<html><head><BASE HREF="https://wrong.example/"></head></html>'
        html = transform_html(body, "/", "http://ha.example:8080").decode()
        self.assertEqual(html.lower().count("<base "), 1)
        self.assertIn('<base href="/">', html)
        self.assertIn('"prefix":""', html)

    def test_rejects_invalid_documents_and_paths(self):
        for path in ("", "//outside.example", "/prefix?query", "/a/../b", "/a\\b", "/a\nb"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                transform_html(HTML, path, "https://ha.example")
        with self.assertRaises(ValueError):
            transform_html(b"x" * (MAX_HTML_BYTES + 1), PREFIX, "https://ha.example")
        with self.assertRaises(ValueError):
            transform_html(b"<html></html>", PREFIX, "https://ha.example")
        with self.assertRaises(ValueError):
            transform_html(HTML, PREFIX, "https://secret@ha.example")

    def test_configuration_cannot_close_script_element(self):
        html = transform_html(HTML, '/prefix</script>', "https://ha.example").decode()
        script = re.search(r'<script id="opencode-ingress">(.*?)</script>', html, re.S)[1]
        self.assertNotIn("</script>", script)
        self.assertIn("\\u003c", script)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_browser_requests_keep_ingress_and_streamed_request_body(self):
        html = transform_html(HTML, PREFIX, "https://ha.example").decode()
        script = re.search(r'<script id="opencode-ingress">(.*?)</script>', html, re.S)[1]
        fixture = r'''
const assert = require("node:assert/strict");
global.window = global;
global.location = {host: "ha.example", origin: "https://ha.example"};
global.document = {baseURI: "https://ha.example/api/hassio_ingress/session-token/"};
const requests = [];
global.fetch = async (input, init) => {requests.push({input, init}); return new Response("ok");};
global.WebSocket = class {constructor(url, options) {this.url = url; this.options = options;}};
global.EventSource = class {constructor(url, options) {this.url = url; this.options = options;}};
global.XMLHttpRequest = class {open(...args) {this.args = args;}};
'''
        verification = r'''
(async () => {
  const prefix = "/api/hassio_ingress/session-token";
  await fetch("https://ha.example/api/health");
  assert.equal(requests[0].input, "https://ha.example" + prefix + "/api/health");
  const upload = new Request("https://ha.example/file", {method: "POST", body: "large-body", headers: {"x-test": "kept"}});
  await fetch(upload);
  assert.equal(requests[1].input.url, "https://ha.example" + prefix + "/file");
  assert.equal(requests[1].input.headers.get("x-test"), "kept");
  assert.equal(await requests[1].input.text(), "large-body");
  await fetch("https://ha.example" + prefix + "/event");
  assert.equal(requests[2].input, "https://ha.example" + prefix + "/event");
  await fetch("https://other.example/api");
  assert.equal(requests[3].input, "https://other.example/api");
  assert.equal(new WebSocket("wss://ha.example/pty/1/connect").url, "wss://ha.example" + prefix + "/pty/1/connect");
  assert.equal(new EventSource("/event").url, "https://ha.example" + prefix + "/event");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/upload", false);
  assert.deepEqual(xhr.args, ["POST", "https://ha.example" + prefix + "/upload", false]);
})().catch((error) => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run([shutil.which("node"), "-e", fixture + script + verification], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class PrepareFrontendTests(unittest.TestCase):
    def test_patches_supported_source_and_fails_on_version_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "packages/app/package.json": json.dumps({"version": VERSION}),
                "packages/app/src/entry.tsx": 'const DEFAULT_SERVER_URL_KEY = "opencode.settings.dat:defaultServerUrl"\n  return location.origin\n',
                "packages/app/src/app.tsx": "                component={props.router ?? Router}\n",
                "packages/ui/src/components/favicon.tsx": '<link href="/a"><link href="/b"><link href="/c"><link href="/d">',
                "packages/app/public/site.webmanifest": json.dumps({"icons": [{"src": "/icon.png"}]}),
            }
            for name, value in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            for name in LOCATION_FILES:
                path = root / "packages/app/src" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('import { useNavigate, useLocation } from "@solidjs/router"\n')
            (root / "packages/app/src/utils").mkdir(parents=True, exist_ok=True)
            prepare_frontend(root)
            self.assertIn("window.__OPENCODE_INGRESS__?.url", (root / "packages/app/src/entry.tsx").read_text())
            self.assertIn('base={window.__OPENCODE_INGRESS__?.prefix ?? ""}', (root / "packages/app/src/app.tsx").read_text())
            manifest = json.loads((root / "packages/app/public/site.webmanifest").read_text())
            self.assertEqual(manifest["start_url"], "./")
            self.assertEqual(manifest["icons"][0]["src"], "./icon.png")
            self.assertIn('from "@/utils/ingress-location"', (root / "packages/app/src/context/tabs.tsx").read_text())
            (root / "packages/app/package.json").write_text(json.dumps({"version": "different"}))
            with self.assertRaisesRegex(ValueError, "must have version"):
                prepare_frontend(root)
