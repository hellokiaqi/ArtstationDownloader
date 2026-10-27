import gzip
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from artstation_downloader.http_session import HttpSession, HttpSessionError


class QuietServer(ThreadingHTTPServer):
    """Keeps expected client disconnects out of the test output."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        return None


PAYLOAD = b'{"hello": "world", "padding": "' + b"x" * 400 + b'"}'


class SessionHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections = 0
    requests = 0
    seen_cookies: list[str] = []
    seen_paths: list[str] = []
    lock = threading.Lock()

    def setup(self):
        super().setup()
        with self.lock:
            type(self).connections += 1

    def do_GET(self):  # noqa: N802 - http.server API
        with self.lock:
            type(self).requests += 1
            type(self).seen_paths.append(self.path)
            cookie = self.headers.get("Cookie")
            if cookie:
                type(self).seen_cookies.append(cookie)
        path = self.path
        if path == "/redirect":
            return self._send(302, b"", {"Location": "/json"})
        if path == "/gzip":
            body = gzip.compress(PAYLOAD)
            return self._send(200, body, {"Content-Type": "application/json", "Content-Encoding": "gzip"})
        if path == "/cookie":
            return self._send(200, b"ok", {"Set-Cookie": "session=abc123; Path=/"})
        if path == "/missing":
            return self._send(404, b"not found", {"Content-Type": "text/plain"})
        if path.startswith("http://"):
            return self._send(200, b"proxied", {})
        return self._send(200, PAYLOAD, {"Content-Type": "application/json", "Content-Encoding": "identity"})

    def _send(self, status, body, headers):
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *_args):
        return None


class HttpSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), SessionHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        SessionHandler.connections = 0
        SessionHandler.requests = 0
        SessionHandler.seen_cookies = []
        SessionHandler.seen_paths = []
        self.session = HttpSession(user_agent="artstation-dl-test", trust_env_proxy=False)

    def tearDown(self):
        self.session.close()

    def test_keep_alive_reuses_one_connection(self):
        for _ in range(3):
            with self.session.open(f"{self.base}/json") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read_all(), PAYLOAD)
        self.assertEqual(SessionHandler.requests, 3)
        self.assertEqual(SessionHandler.connections, 1)

    def test_gzip_body_is_decoded(self):
        with self.session.open(f"{self.base}/gzip", headers={"Accept-Encoding": "gzip"}) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read_all(), PAYLOAD)

    def test_gzip_bodies_still_reuse_the_connection(self):
        for _ in range(3):
            with self.session.open(f"{self.base}/gzip", headers={"Accept-Encoding": "gzip"}) as response:
                self.assertEqual(response.read_all(), PAYLOAD)
        self.assertEqual(SessionHandler.connections, 1)

    def test_redirects_are_followed(self):
        with self.session.open(f"{self.base}/redirect") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read_all(), PAYLOAD)
            self.assertTrue(response.url.endswith("/json"))

    def test_error_status_is_exposed_with_body(self):
        with self.session.open(f"{self.base}/missing") as response:
            self.assertEqual(response.status, 404)
            self.assertEqual(response.read_all(), b"not found")

    def test_cookies_round_trip(self):
        with self.session.open(f"{self.base}/cookie"):
            pass
        with self.session.open(f"{self.base}/json") as response:
            response.read_all()
        self.assertTrue(any("session=abc123" in value for value in SessionHandler.seen_cookies))

    def test_explicit_proxy_is_used_with_absolute_uri(self):
        proxy = HttpSession(user_agent="test", proxy_url=f"http://127.0.0.1:{self.server.server_port}", trust_env_proxy=False)
        try:
            with proxy.open("http://example.invalid/asset.jpg") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read_all(), b"proxied")
            self.assertIn("http://example.invalid/asset.jpg", SessionHandler.seen_paths)
        finally:
            proxy.close()

    def test_unsupported_scheme_is_rejected(self):
        with self.assertRaises(HttpSessionError):
            self.session.open("ftp://example.com/file.jpg")


if __name__ == "__main__":
    unittest.main()

