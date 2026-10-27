import json
import tempfile
import threading
import unittest
from pathlib import Path

from artstation_downloader.store import DownloadState, default_state_path


def asset_row(key: str, profile: str = "artist", status: str = "completed", position: int = 1) -> dict:
    return {
        "asset_key": key,
        "profile": profile,
        "project_hash_id": "project",
        "project_position": 1,
        "project_title": "Project",
        "asset_id": key,
        "asset_position": position,
        "source_url": "https://cdna.artstation.com/file.jpg",
        "path": f"artist/0001_Project/00{position}_{key}.jpg",
        "status": status,
        "bytes": 4,
        "sha256": "hash",
        "width": None,
        "height": None,
        "content_type": "image/jpeg",
        "selected_url": "https://cdna.artstation.com/file.jpg",
        "error": None,
        "updated_at": 1.0,
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = DownloadState(default_state_path(self.root))

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_round_trip_and_counts(self):
        self.state.upsert(asset_row("a"))
        self.state.upsert(asset_row("b", status="failed", position=2))
        self.assertEqual(self.state.get("a")["status"], "completed")
        self.assertEqual(len(self.state.rows_for_profile("artist")), 2)
        self.assertEqual(self.state.counts("artist"), {"completed": 1, "failed": 1})
        self.assertEqual(self.state.total_bytes("artist"), 4)

    def test_upsert_updates_in_place(self):
        self.state.upsert(asset_row("a"))
        row = asset_row("a", status="failed")
        row["error"] = "boom"
        self.state.upsert(row)
        stored = self.state.get("a")
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "boom")
        self.assertEqual(len(self.state.rows_for_profile("artist")), 1)

    def test_project_cache_round_trip(self):
        snapshot = {"title": "P", "assets": [{"id": "1", "image_url": "u"}]}
        self.state.upsert_project(
            profile="artist",
            hash_id="abc",
            position=1,
            title="P",
            updated_at="2026-01-01T00:00:00Z",
            assets_count=1,
            snapshot=snapshot,
        )
        cached = self.state.get_project("artist", "abc")
        self.assertEqual(cached["updated_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(json.loads(cached["snapshot"]), snapshot)

    def test_legacy_state_json_is_imported_once(self):
        legacy = self.root / "state.json"
        legacy.write_text(
            json.dumps(
                {
                    "version": 1,
                    "downloads": {"key-1": asset_row("key-1"), "key-2": asset_row("key-2")},
                }
            ),
            encoding="utf-8",
        )
        migrated = DownloadState(default_state_path(self.root))
        try:
            self.assertEqual(len(migrated.rows_for_profile("artist")), 2)
            self.assertTrue(legacy.exists(), "the legacy file must be left untouched")
        finally:
            migrated.close()
        again = DownloadState(default_state_path(self.root))
        try:
            self.assertEqual(len(again.rows_for_profile("artist")), 2)
        finally:
            again.close()

    def test_corrupt_database_is_replaced(self):
        corrupt_root = Path(self.directory.name) / "corrupt"
        corrupt_root.mkdir()
        path = default_state_path(corrupt_root)
        path.write_bytes(b"this is not a sqlite database" * 40)
        state = DownloadState(path)
        try:
            state.upsert(asset_row("a"))
            self.assertEqual(state.get("a")["status"], "completed")
            self.assertTrue(list(corrupt_root.glob("state.sqlite3.corrupt-*")))
        finally:
            state.close()

    def test_concurrent_writers(self):
        def write(worker: int) -> None:
            for index in range(20):
                self.state.upsert(asset_row(f"w{worker}-{index}", position=index + 1))

        threads = [threading.Thread(target=write, args=(worker,)) for worker in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.state.rows_for_profile("artist")), 80)


if __name__ == "__main__":
    unittest.main()
