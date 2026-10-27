import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from artstation_downloader.client import ArtStationClient
from artstation_downloader.core import ImageAsset, Profile
from artstation_downloader.crawl import Crawler
from artstation_downloader.downloader import LocalDownloader
from artstation_downloader.store import DownloadState, default_state_path
from artstation_downloader.ui import Console, Progress

IMAGE = b"\x89PNG\r\n\x1a\n" + b"crawl-image" * 32


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        return None


class CrawlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cancel: threading.Event | None = None
    malformed: str = ""
    images_served = 0
    lock = threading.Lock()

    def do_GET(self):  # noqa: N802
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path.startswith("/users/artist/quick.json"):
            return self._json({"full_name": "Crawl Artist"})
        if self.path.startswith("/users/artist/projects.json"):
            items = [
                {
                    "hash_id": f"p{index}",
                    "title": f"Project {index}",
                    "permalink": f"{base}/artwork/p{index}",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "assets_count": 1,
                }
                for index in range(4)
            ]
            return self._json({"data": items, "total_count": 4})
        if self.path.startswith("/projects/"):
            hash_id = self.path.split("/projects/")[1].split(".json")[0]
            if hash_id == self.malformed:
                body = b'{"assets": "not-a-list"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            return self._json(
                {
                    "title": f"Project {hash_id}",
                    "permalink": f"{base}/artwork/{hash_id}",
                    "tags": [],
                    "assets": [
                        {
                            "id": hash_id,
                            "has_image": True,
                            "image_url": f"{base}/assets/large/{hash_id}.jpg",
                        }
                    ],
                }
            )
        if self.path.startswith("/assets/"):
            with self.lock:
                type(self).images_served += 1
                if self.cancel is not None:
                    self.cancel.set()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(IMAGE)))
            self.end_headers()
            self.wfile.write(IMAGE)
            return None
        return self._json({"error": "missing"}, status=404)

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return None

    def log_message(self, *_args):
        return None


class CrawlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), CrawlHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        CrawlHandler.cancel = None
        CrawlHandler.malformed = ""
        CrawlHandler.images_served = 0
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name) / "out"
        self.output.mkdir()
        self.state = DownloadState(default_state_path(self.output))
        self.client = ArtStationClient(base_url=self.base, delay=0, retries=0, trust_env_proxy=False)
        self.downloader = LocalDownloader(self.client, self.output, state=self.state)
        self.console = Console(io.StringIO(), color=False, quiet=True)

    def tearDown(self):
        self.downloader.close()
        self.client.close()
        self.directory.cleanup()

    def _crawler(self, cancel=None, jobs=2) -> Crawler:
        self.progress = Progress(self.console, enabled=False)
        return Crawler(
            self.client,
            self.downloader,
            self.state,
            profile=Profile(username="artist", canonical_url=f"{self.base}/artist"),
            console=self.console,
            progress=self.progress,
            jobs=jobs,
            max_projects=4,
            cancel=cancel,
        )

    def test_progress_counters_track_bytes_and_capped_total(self):
        result = self._crawler().run()
        self.assertEqual(self.progress.bytes_downloaded, result.bytes_downloaded)
        self.assertGreater(result.bytes_downloaded, 0)
        self.assertEqual(self.progress.projects_total, 4)
        self.assertEqual(self.progress.projects_done, 4)

    def test_submitting_drains_finished_downloads(self):
        crawler = self._crawler(jobs=2)
        asset = ImageAsset(
            profile="artist",
            project_hash_id="p0",
            project_position=1,
            project_title="Project 0",
            project_url=f"{self.base}/artwork/p0",
            asset_id="0",
            position=1,
            source_url=f"{self.base}/assets/large/p0.jpg",
            original_name="p0.jpg",
        )
        crawler._events.put(("start", asset))
        crawler._events.put(("done", asset, {"status": "completed", "bytes": 2048, "path": "a/b.jpg"}))
        self.assertTrue(crawler._submit(asset))
        self.assertEqual(crawler.result.downloaded, 1)
        self.assertEqual(crawler.result.bytes_downloaded, 2048)
        self.assertEqual(self.progress.downloaded, 1)

    def test_cancellation_stops_cleanly_and_keeps_progress(self):
        cancel = threading.Event()
        CrawlHandler.cancel = cancel
        result = self._crawler(cancel=cancel, jobs=1).run()
        self.assertTrue(result.interrupted)
        self.assertEqual(result.downloaded, 1)
        self.assertEqual(self.state.counts("artist").get("completed"), 1)
        self.assertEqual(CrawlHandler.images_served, 1)

    def test_broken_project_does_not_abort_the_crawl(self):
        CrawlHandler.malformed = "p1"
        self.console.quiet = False
        result = self._crawler().run()
        self.assertEqual(result.downloaded, 3)
        self.assertEqual(result.projects, 4)
        self.assertFalse(result.interrupted)
        self.assertIn("skipping project p1", self.console.stream.getvalue())

    def test_second_run_skips_cached_projects(self):
        first = self._crawler().run()
        self.assertEqual(first.downloaded, 4)
        CrawlHandler.images_served = 0
        second = self._crawler().run()
        self.assertEqual(second.downloaded, 0)
        self.assertEqual(second.cached, 4)
        self.assertEqual(second.projects_cached, 4)
        self.assertEqual(CrawlHandler.images_served, 0)


if __name__ == "__main__":
    unittest.main()
