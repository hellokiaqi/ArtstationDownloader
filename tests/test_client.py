import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from artstation_downloader.client import ArtStationClient, _has_noai_tag
from artstation_downloader.core import ProjectRef


class QuietServer(ThreadingHTTPServer):
    """Keeps expected client disconnects out of the test output."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        return None


IMAGE = b"\x89PNG\r\n\x1a\n" + b"test-image-payload"


class RangeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        start = 0
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            start = int(header.removeprefix("bytes=").split("-", 1)[0])
        payload = IMAGE[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(IMAGE) - 1}/{len(IMAGE)}")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return None


class ClientTests(unittest.TestCase):
    def test_noai_tag_detection(self):
        self.assertTrue(_has_noai_tag({"tags": ["NoAI"]}))
        self.assertTrue(_has_noai_tag({"tags": [{"name": "no-ai"}]}))
        self.assertFalse(_has_noai_tag({"tags": ["concept art"]}))

    def test_project_pagination_and_asset_filtering(self):
        client = ArtStationClient(delay=0, retries=0)

        def fake_json(url, referer=None):
            if "/projects.json?" in url:
                page = "page=2" in url
                return {
                    "data": ([{"hash_id": "b", "title": "Second"}] if page else [
                        {"hash_id": "a", "title": "First"}
                    ]),
                    "total_count": 2,
                }
            return {
                "title": "First",
                "created_at": "2026-01-01T00:00:00Z",
                "assets": [
                    {"id": 10, "has_image": True, "image_url": "https://cdna.artstation.com/x/large/a.jpg"},
                    {"id": 11, "has_image": False, "image_url": "https://cdna.artstation.com/x/large/b.jpg"},
                ],
            }

        client.get_json = fake_json
        projects = list(client.iter_projects("artist"))
        self.assertEqual([project.hash_id for project in projects], ["a", "b"])
        assets = list(client.iter_project_assets("artist", ProjectRef("a", 1, "First")))
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].original_name, "a.jpg")

    def test_stream_to_file_resumes_range(self):
        server = QuietServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                part = Path(directory) / "asset.jpg.part"
                part.write_bytes(IMAGE[:8])
                client = ArtStationClient(delay=0, retries=0, trust_env_proxy=False)
                content_type, byte_count, digest = client.stream_to_file(
                    f"http://127.0.0.1:{server.server_port}/asset.jpg",
                    part,
                    max_bytes=1024,
                )
                self.assertEqual(content_type, "image/png")
                self.assertEqual(part.read_bytes(), IMAGE)
                self.assertEqual(byte_count, len(IMAGE))
                self.assertEqual(len(digest), 64)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

