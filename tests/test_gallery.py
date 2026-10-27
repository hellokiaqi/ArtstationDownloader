import tempfile
import unittest
from pathlib import Path

from artstation_downloader.gallery import write_gallery

PNG = b"\x89PNG\r\n\x1a\n" + b"gallery-image"


def row(name: str, *, profile: str = "artist", project: str = "Project", position: int = 1,
        asset_position: int = 1, status: str = "completed", gallery_height: int | None = 1000) -> dict:
    return {
        "asset_key": f"{project}-{name}",
        "profile": profile,
        "project_hash_id": "hash",
        "project_position": position,
        "project_title": project,
        "asset_id": name,
        "asset_position": asset_position,
        "source_url": "https://cdnb.artstation.com/a.jpg",
        "path": f"{profile}/{position:04d}_{project}/{asset_position:03d}_{name}",
        "status": status,
        "bytes": len(PNG),
        "sha256": "x" * 64,
        "width": 800,
        "height": gallery_height,
        "content_type": "image/png",
        "selected_url": "https://cdnb.artstation.com/a.jpg",
        "error": None,
        "updated_at": 1.0,
    }


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.profile_dir = self.root / "artist"
        self.profile_dir.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def _create_files(self, rows):
        for item in rows:
            path = self.root / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG)

    def test_gallery_groups_projects_and_links_relatively(self):
        rows = [
            row("a.png", project="First", position=1, asset_position=1),
            row("b.png", project="First", position=1, asset_position=2),
            row("c.png", project="Second", position=2, asset_position=1),
        ]
        self._create_files(rows)
        destination = self.profile_dir / "index.html"

        stats = write_gallery(
            destination,
            profile="artist",
            artist="Test Artist",
            rows=rows,
            output_root=self.root,
            version="0.3.0",
            generated_at="2026-01-01 12:00",
        )

        page = destination.read_text(encoding="utf-8")
        self.assertEqual((stats.projects, stats.images, stats.missing), (2, 3, 0))
        self.assertIn("Test Artist", page)
        self.assertIn("2 个项目", page)
        self.assertIn("3 张图片", page)
        self.assertIn('href="0001_First/001_a.png"', page)
        self.assertIn('href="0002_Second/001_c.png"', page)
        self.assertIn('loading="lazy"', page)
        self.assertIn("2026-01-01 12:00", page)
        self.assertIn("生成于", page)

    def test_only_completed_and_present_files_are_listed(self):
        rows = [
            row("ok.png"),
            row("failed.png", status="failed"),
            row("gone.png", asset_position=2),
        ]
        self._create_files([rows[0]])  # only the completed row exists on disk
        destination = self.profile_dir / "index.html"

        stats = write_gallery(
            destination, profile="artist", artist="A", rows=rows, output_root=self.root
        )

        page = destination.read_text(encoding="utf-8")
        self.assertEqual(stats.images, 1)
        self.assertEqual(stats.missing, 1)
        self.assertNotIn("gone.png", page)
        self.assertNotIn("failed.png", page)
        self.assertIn("1 个文件缺失", page)

    def test_html_is_escaped(self):
        rows = [row("a&b.png", project="Tom & Jerry", position=1)]
        self._create_files(rows)
        destination = self.profile_dir / "index.html"

        write_gallery(destination, profile="artist", artist="A & B", rows=rows, output_root=self.root)

        page = destination.read_text(encoding="utf-8")
        self.assertIn("Tom &amp; Jerry", page)
        self.assertIn("A &amp; B", page)
        self.assertNotIn("Tom & Jerry", page)
        self.assertIn("a&amp;b.png", page)

    def test_empty_gallery_is_still_a_page(self):
        destination = self.profile_dir / "index.html"
        stats = write_gallery(destination, profile="artist", artist="A", rows=[], output_root=self.root)
        page = destination.read_text(encoding="utf-8")
        self.assertEqual(stats.images, 0)
        self.assertIn("还没有下载任何图片", page)
        self.assertIn("<title>", page)

    def test_missing_dimensions_are_omitted(self):
        rows = [row("a.png", gallery_height=None)]
        self._create_files(rows)
        destination = self.profile_dir / "index.html"
        write_gallery(destination, profile="artist", artist="A", rows=rows, output_root=self.root)
        page = destination.read_text(encoding="utf-8")
        self.assertNotIn("×", page)


if __name__ == "__main__":
    unittest.main()
