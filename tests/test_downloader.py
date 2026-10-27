import hashlib
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from artstation_downloader.client import ArtStationClient
from artstation_downloader.core import ImageAsset
from artstation_downloader.downloader import LocalDownloader


class QuietServer(ThreadingHTTPServer):
    """Keeps expected client disconnects out of the test output."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        return None



class ImageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = b"\x89PNG\r\n\x1a\n" + b"local-test-image"
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return None


class FallbackClient:
    media_hosts = ("artstation.com",)

    def __init__(self):
        self.calls = []

    def stream_to_file(self, url, part_path, *, referer=None, max_bytes=None):
        del referer, max_bytes
        self.calls.append(url)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        if "/8k/" in url:
            part_path.write_bytes(b"partial candidate")
            raise RuntimeError("8k candidate unavailable")
        payload = b"\x89PNG\r\n\x1a\n" + b"fallback-image"
        part_path.write_bytes(payload)
        return "image/png", len(payload), "a" * 64


class DownloaderTests(unittest.TestCase):
    def test_download_writes_manifest_state_and_skips_second_run(self):
        server = QuietServer(("127.0.0.1", 0), ImageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = ArtStationClient(delay=0, retries=0, trust_env_proxy=False)
                downloader = LocalDownloader(client, Path(directory), dry_run=False)
                downloader._allowed_url = lambda _url: True
                asset = ImageAsset(
                    profile="artist",
                    project_hash_id="project",
                    project_position=1,
                    project_title="A project",
                    project_url="https://www.artstation.com/artwork/project",
                    asset_id="asset",
                    position=1,
                    source_url=f"http://127.0.0.1:{server.server_port}/large/image.jpg",
                    original_name="image.jpg",
                )
                first = downloader.download(asset)
                self.assertEqual(first["status"], "completed")
                self.assertTrue((Path(directory) / first["path"]).exists())
                self.assertFalse(Path(first["path"]).is_absolute())
                self.assertTrue(first["path"].endswith("image.png"))
                second = downloader.download(asset)
                self.assertEqual(second["status"], "completed")
                self.assertEqual(second["path"], first["path"])
                downloader.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_quality_fallback_does_not_reuse_failed_candidate_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FallbackClient()
            downloader = LocalDownloader(client, Path(directory), dry_run=False)
            asset = ImageAsset(
                profile="artist",
                project_hash_id="project",
                project_position=1,
                project_title="A project",
                project_url="https://www.artstation.com/artwork/project",
                asset_id="asset",
                position=1,
                source_url="https://cdna.artstation.com/x/large/image.jpg",
                original_name="image.jpg",
            )

            result = downloader.download(asset)

            self.assertEqual(result["status"], "completed")
            self.assertIn("/8k/", client.calls[0])
            self.assertIn("/4k/", client.calls[1])
            self.assertEqual(list(Path(directory).rglob("*.part")), [])
            downloader.close()

    def test_verify_detects_corruption_and_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = LocalDownloader(FallbackClient(), root, dry_run=False)
            target = root / "artist/0001_A project/001_image.jpg"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"hello image")
            digest = hashlib.sha256(b"hello image").hexdigest()
            downloader.state.upsert(
                {
                    "asset_key": "asset-key",
                    "profile": "artist",
                    "path": "artist/0001_A project/001_image.jpg",
                    "status": "completed",
                    "bytes": len(b"hello image"),
                    "sha256": digest,
                }
            )

            clean = downloader.verify_library("artist")
            self.assertTrue(clean.ok)
            self.assertEqual(clean.checked, 1)
            self.assertEqual(clean.bytes_checked, len(b"hello image"))

            target.write_bytes(b"hello imAge")  # same size, different bytes
            broken = downloader.verify_library("artist")
            self.assertFalse(broken.ok)
            self.assertEqual(broken.corrupt, 1)
            self.assertEqual(broken.invalidated, 1)
            self.assertIn("checksum", broken.problems[0][1])
            self.assertEqual(downloader.state.get("asset-key")["status"], "failed")
            downloader.close()

    def test_verify_reports_missing_files_without_changing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = LocalDownloader(FallbackClient(), root, dry_run=False)
            downloader.state.upsert(
                {
                    "asset_key": "gone",
                    "profile": "artist",
                    "path": "artist/0001_A project/001_missing.jpg",
                    "status": "completed",
                    "bytes": 10,
                }
            )

            report = downloader.verify_library("artist")

            self.assertEqual(report.missing, 1)
            self.assertEqual(report.invalidated, 0)
            self.assertEqual(downloader.state.get("gone")["status"], "completed")
            downloader.close()

    def test_verify_dry_run_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloader = LocalDownloader(FallbackClient(), root, dry_run=True)
            target = root / "artist/0001_A project/001_image.jpg"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"changed")
            downloader.state.upsert(
                {
                    "asset_key": "asset-key",
                    "profile": "artist",
                    "path": "artist/0001_A project/001_image.jpg",
                    "status": "completed",
                    "bytes": 999,
                }
            )

            report = downloader.verify_library("artist")

            self.assertEqual(report.corrupt, 1)
            self.assertEqual(report.invalidated, 0)
            self.assertEqual(downloader.state.get("asset-key")["status"], "completed")
            downloader.close()

    def test_moved_library_is_recognised_and_paths_are_healed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            asset = ImageAsset(
                profile="artist",
                project_hash_id="project",
                project_position=1,
                project_title="A project",
                project_url="https://www.artstation.com/artwork/project",
                asset_id="asset",
                position=1,
                source_url="https://cdna.artstation.com/x/large/image.jpg",
                original_name="image.jpg",
            )
            relative = Path("artist/0001_A project/001_image.jpg")
            (output / relative).parent.mkdir(parents=True)
            (output / relative).write_bytes(b"\x89PNG\r\n\x1a\n" + b"moved")

            class NoDownloadClient:
                media_hosts = ("artstation.com",)

                def stream_to_file(self, *args, **kwargs):
                    raise AssertionError("a moved file must not be downloaded again")

            downloader = LocalDownloader(NoDownloadClient(), output, dry_run=False)
            foreign = (
                r"\\wsl.localhost\Ubuntu-22.04\home\someone\downloads\artist\0001_A project\001_image.jpg"
                if os.name == "nt"
                else "/mnt/other-machine/downloads/artist/0001_A project/001_image.jpg"
            )
            downloader.state.upsert(
                {
                    "asset_key": asset.key,
                    "profile": "artist",
                    "status": "completed",
                    "path": foreign,
                }
            )

            result = downloader.download(asset)

            self.assertTrue(result["cached"])
            self.assertEqual(result["path"], relative.as_posix())
            self.assertEqual(downloader.state.get(asset.key)["path"], relative.as_posix())
            downloader.close()


if __name__ == "__main__":
    unittest.main()

