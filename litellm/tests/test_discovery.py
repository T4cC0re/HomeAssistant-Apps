"""Check service discovery and credential handling."""

from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

import run


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"SUPERVISOR_TOKEN": "test-supervisor-token"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.requests = []
        self.responses = deque()
        requests = self.requests
        responses = self.responses

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.respond()

            def respond(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                requests.append({
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "body": json.loads(body) if body else None,
                })
                status, response = responses.popleft() if responses else (500, {"result": "error"})
                encoded = json.dumps(response).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        endpoint = patch.object(run, "SUPERVISOR_URL", f"http://127.0.0.1:{self.server.server_port}")
        endpoint.start()
        self.addCleanup(endpoint.stop)
        hostname = patch.object(run.socket, "gethostname", return_value="local-litellm")
        hostname.start()
        self.addCleanup(hostname.stop)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def test_request_serializes_json_and_authenticates(self):
        self.responses.append((200, {"result": "ok", "data": {"uuid": "discovery-test"}}))
        payload = {"service": "litellm", "config": {"master_key": "selected-key", "path": "/v1"}}

        result = run.supervisor_request("POST", "/discovery", payload)

        self.assertEqual(result, {"uuid": "discovery-test"})
        self.assertEqual(self.requests, [{
            "method": "POST",
            "path": "/discovery",
            "authorization": "Bearer test-supervisor-token",
            "content_type": "application/json",
            "body": payload,
        }])

    def test_publish_uses_hostname_and_exact_selected_key_path_and_port(self):
        os.environ["LITELLM_MASTER_KEY"] = "fallback-key"
        cases = [
            ("/v1", "first-key", 4000),
            ("/proxy", "second-key", 4123),
            ("", "third-key", 4000),
        ]
        for root_path, key, port in cases:
            self.responses.append((200, {"result": "ok", "data": {"uuid": "discovery-test"}}))
            run.publish_discovery(key, root_path, port)

        self.assertEqual(len(self.requests), len(cases))
        for publication, (root_path, key, port) in zip(self.requests, cases):
            self.assertEqual((publication["method"], publication["path"]), ("POST", "/discovery"))
            self.assertEqual(publication["body"], {
                "service": "litellm",
                "config": {"host": "local-litellm", "port": port, "master_key": key, "path": root_path},
            })
            self.assertEqual(publication["authorization"], "Bearer test-supervisor-token")

    def test_publish_requires_confirmation(self):
        self.responses.append((200, {"result": "ok", "data": {"uuid": ""}}))
        with self.assertRaisesRegex(run.DiscoveryError, "confirm discovery"):
            run.publish_discovery("selected-key", "/v1", 4000)

    def test_http_and_supervisor_errors_do_not_expose_response_values(self):
        for status in [401, 200]:
            with self.subTest(status=status):
                self.responses.append((status, {"result": "error", "message": "private-server-value"}))
                with self.assertRaises(run.DiscoveryError) as caught:
                    run.publish_discovery("private-key", "/v1", 4000)
                self.assertNotIn("private-server-value", str(caught.exception))
                self.assertNotIn("test-supervisor-token", str(caught.exception))
                self.assertNotIn("private-key", str(caught.exception))

    def test_missing_token_does_not_make_a_request(self):
        del os.environ["SUPERVISOR_TOKEN"]
        with self.assertRaisesRegex(KeyError, "SUPERVISOR_TOKEN"):
            run.publish_discovery("selected-key", "/v1", 4000)
        self.assertEqual(self.requests, [])

    def test_local_requests_ignore_http_proxy_environment(self):
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        os.environ["no_proxy"] = ""
        self.responses.append((200, {"result": "ok", "data": {"uuid": "discovery-test"}}))

        run.publish_discovery("selected-key", "/v1", 4000)
        self.assertEqual(len(self.requests), 1)

if __name__ == "__main__":
    unittest.main()
