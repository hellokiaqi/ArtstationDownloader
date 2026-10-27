import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from artstation_downloader import cli

IMAGE = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 64


class QuietServer(ThreadingHTTPServer):
    """Keeps expected client disconnects out of the test output."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        return None


class MockArtStation(BaseHTTPRequestHandler):
    """A tiny stand-in for the public ArtStation JSON surface."""

    protocol_version = "HTTP/1.1"
    requests: list[str] = []
    lock = threading.Lock()

    def do_GET(self):  # noqa: N802 - http.server API
        with self.lock:
            type(self).requests.append(self.path)
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path.startswith("/users/artist/quick.json"):
            return self._json({"username": "artist", "full_name": "Test Artist"})
        if self.path.startswith("/users/artist/projects.json"):
            if "page=2" in self.path:
                return self._json({"data": [], "total_count": 2})
            return self._json(
                {
                    "total_count": 2,
                    "data": [
                        {
                            "hash_id": "p1",
                            "title": "First Project",
                            "permalink": f"{base}/artwork/p1",
                            "updated_at": "2026-01-01T00:00:00Z",
                            "assets_count": 2,
                        },
                        {
                            "hash_id": "p2",
                            "title": "Second Project",
                            "permalink": f"{base}/artwork/p2",
                            "updated_at": "2026-01-02T00:00:00Z",
                            "assets_count": 1,
                        },
                    ],
                }
            )
        if self.path.startswith("/projects/p1.json"):
            return self._json(
                {
                    "hash_id": "p1",
                    "title": "First Project",
                    "permalink": f"{base}/artwork/p1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "tags": [],
                    "assets": [
                        {"id": 11, "has_image": True, "image_url": f"{base}/assets/large/001.jpg"},
                        {"id": 12, "has_image": True, "image_url": f"{base}/assets/large/002.jpg"},
                    ],
                }
            )
        if self.path.startswith("/projects/p2.json"):
            return self._json(
                {
                    "hash_id": "p2",
                    "title": "Second Project",
                    "permalink": f"{base}/artwork/p2",
                    "created_at": "2026-01-02T00:00:00Z",
                    "tags": ["NoAI"],
                    "assets": [
                        {"id": 21, "has_image": True, "image_url": f"{base}/assets/large/003.jpg"},
                    ],
                }
            )
        if self.path.startswith("/assets/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(IMAGE)))
            self.end_headers()
            self.wfile.write(IMAGE)
            return None
        return self._json({"error": "not found"}, status=404)

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return None

    def log_message(self, *_args):
        return None


class CliEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), MockArtStation)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        MockArtStation.requests = []
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name) / "downloads"
        self._env = os.environ.get("ARTSTATION_API_BASE")
        os.environ["ARTSTATION_API_BASE"] = self.base

    def tearDown(self):
        if self._env is None:
            os.environ.pop("ARTSTATION_API_BASE", None)
        else:
            os.environ["ARTSTATION_API_BASE"] = self._env
        self.directory.cleanup()

    def _run(self, *extra: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli.main(["artist", "-o", str(self.output), "--delay", "0", *extra])
        return code, buffer.getvalue()

    def test_first_run_downloads_and_writes_manifest(self):
        code, output = self._run("--jobs", "2")
        self.assertEqual(code, 0, output)
        self.assertIn("Test Artist", output)
        self.assertIn("Done", output)
        self.assertIn("Downloaded  2 file(s)", output)
        self.assertIn("Skipped     1 file(s)", output)  # NoAI project is skipped by default

        first = self.output / "artist" / "0001_First Project" / "001_001.jpg"
        second = self.output / "artist" / "0001_First Project" / "002_002.jpg"
        self.assertTrue(first.is_file(), list(self.output.rglob("*")))
        self.assertTrue(second.is_file())
        self.assertEqual(first.read_bytes(), IMAGE)

        manifest = self.output / "artist" / "manifest.jsonl"
        rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 3)
        completed = [row for row in rows if row["status"] == "completed"]
        self.assertEqual(len(completed), 2)
        self.assertTrue(all(not Path(row["path"]).is_absolute() for row in completed))
        self.assertEqual({row["width"] for row in completed}, {None})

    def test_second_run_uses_cache_and_downloads_nothing(self):
        first_code, _ = self._run("--jobs", "2")
        self.assertEqual(first_code, 0)
        project_requests = [path for path in MockArtStation.requests if path.startswith("/projects/")]
        self.assertEqual(len(project_requests), 2)

        MockArtStation.requests = []
        second_code, output = self._run("--jobs", "2")
        self.assertEqual(second_code, 0, output)
        self.assertIn("Already had 2 file(s)", output)
        self.assertEqual([path for path in MockArtStation.requests if path.startswith("/projects/")], [])
        self.assertEqual([path for path in MockArtStation.requests if path.startswith("/assets/")], [])

    def test_dry_run_writes_nothing(self):
        code, output = self._run("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertIn("Dry run complete", output)
        self.assertIn("Planned     2 file(s)", output)
        self.assertFalse((self.output / "artist").exists())
        self.assertFalse((self.output / "state.sqlite3").exists())

    def test_ignore_noai_downloads_everything(self):
        code, output = self._run("--ignore-noai")
        self.assertEqual(code, 0, output)
        self.assertIn("Downloaded  3 file(s)", output)
        self.assertTrue((self.output / "artist" / "0002_Second Project" / "001_003.jpg").is_file())

    def test_max_files_limits_the_run(self):
        code, output = self._run("--max-files", "1", "--jobs", "1")
        self.assertEqual(code, 0, output)
        self.assertIn("Downloaded  1 file(s)", output)

    def test_gallery_flag_writes_browsable_index(self):
        code, output = self._run("--jobs", "2", "--gallery")
        self.assertEqual(code, 0, output)
        index = self.output / "artist" / "index.html"
        self.assertTrue(index.is_file())
        page = index.read_text(encoding="utf-8")
        self.assertIn("Test Artist", page)
        self.assertIn("2 张图片", page)
        self.assertIn('href="0001_First%20Project/001_001.jpg"', page)
        self.assertIn("Gallery", output)

    def test_gallery_only_rebuilds_without_network(self):
        first_code, _ = self._run("--jobs", "2")
        self.assertEqual(first_code, 0)
        index = self.output / "artist" / "index.html"
        index.unlink(missing_ok=True)

        MockArtStation.requests = []
        code, output = self._run("--gallery-only")

        self.assertEqual(code, 0, output)
        self.assertTrue(index.is_file())
        self.assertEqual(MockArtStation.requests, [])

    def test_verify_reports_corrupted_files(self):
        first_code, _ = self._run("--jobs", "2")
        self.assertEqual(first_code, 0)
        target = self.output / "artist" / "0001_First Project" / "001_001.jpg"
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"tampered")

        code, output = self._run("--verify")

        self.assertEqual(code, 1, output)
        self.assertIn("corrupt", output.lower())
        manifest = self.output / "artist" / "manifest.jsonl"
        rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
        broken = [row for row in rows if row["path"].endswith("001_001.jpg")]
        self.assertEqual(broken[0]["status"], "failed")

    def test_offline_modes_need_existing_state(self):
        code, output = self._run("--verify")
        self.assertEqual(code, 2)
        self.assertIn("no saved download state", output)

    def test_unreachable_server_reports_a_clean_error(self):
        os.environ["ARTSTATION_API_BASE"] = "http://127.0.0.1:1"
        code, output = self._run("--retries", "0")
        self.assertEqual(code, 2)
        self.assertIn("request failed for", output)
        self.assertNotIn("Traceback", output)


if __name__ == "__main__":
    unittest.main()

