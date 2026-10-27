import tempfile
import unittest
from pathlib import Path

from artstation_downloader.core import (
    build_destination,
    image_quality_candidates,
    normalize_profile_url,
    parse_netscape_cookies,
    safe_component,
    shorten_with_hash,
    sniff_image,
)


class CoreTests(unittest.TestCase):
    def test_profile_normalization(self):
        self.assertEqual(normalize_profile_url("artist").username, "artist")
        self.assertEqual(
            normalize_profile_url("https://www.artstation.com/artist/albums/all").canonical_url,
            "https://www.artstation.com/artist",
        )
        self.assertEqual(
            normalize_profile_url("https://artist.artstation.com/projects").username,
            "artist",
        )
        with self.assertRaises(ValueError):
            normalize_profile_url("https://www.artstation.com/artwork/abc")

    def test_quality_candidates_only_transform_known_layout(self):
        url = "https://cdna.artstation.com/p/assets/images/images/001/large/file.jpg"
        candidates = image_quality_candidates(url)
        self.assertIn("/8k/", candidates[0])
        self.assertIn("/4k/", candidates[1])
        self.assertEqual(image_quality_candidates("https://cdn.example.com/file.jpg"), ["https://cdn.example.com/file.jpg"])

    def test_safe_component(self):
        self.assertEqual(safe_component('a:b*?c'), "a_b__c")
        self.assertEqual(safe_component("CON"), "_CON")
        self.assertEqual(safe_component(""), "untitled")
        self.assertEqual(safe_component('CON.txt. ').upper(), "CON.TXT")

    def test_cookie_parser_filters_domains(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                "#HttpOnly_.artstation.com\tTRUE\t/\tTRUE\t0\tsession\tsecret\n"
                ".example.com\tTRUE\t/\tFALSE\t0\tother\tignored\n",
                encoding="utf-8",
            )
            self.assertEqual(parse_netscape_cookies(path), [(".artstation.com", "/", "session", "secret")])

    def test_image_sniff(self):
        self.assertTrue(sniff_image(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(sniff_image(b"\xff\xd8\xff\xe0"))
        self.assertFalse(sniff_image(b"<html>challenge</html>"))


class DestinationTests(unittest.TestCase):
    def test_normal_destination_is_readable(self):
        path = build_destination(
            Path("D:/out"),
            "artist",
            project_position=4,
            project_title="A project: part 1?",
            asset_position=3,
            file_stem="image",
            suffix=".jpg",
        )
        self.assertEqual(path.as_posix(), "D:/out/artist/0004_A project_ part 1_/003_image.jpg")

    def test_long_paths_are_shortened_within_budget(self):
        deep_root = Path("C:/Users/someone/Downloads/" + "a" * 60)
        title = "Very long project title " * 6
        path = build_destination(
            deep_root,
            "some-artist-name",
            project_position=12,
            project_title=title,
            asset_position=7,
            file_stem="very long file name " * 5,
            suffix=".jpg",
        )
        self.assertLessEqual(len(str(path)), 248)
        self.assertTrue(path.parent.name.startswith("0012_"), path.parent.name)
        self.assertTrue(path.name.startswith("007_"), path.name)
        self.assertTrue(path.name.endswith(".jpg"))
        self.assertIn("some-artist-name", str(path))

    def test_extreme_budget_still_fits(self):
        path = build_destination(
            Path("C:/out"),
            "artist",
            project_position=12,
            project_title="Very long project title " * 6,
            asset_position=7,
            file_stem="very long file name " * 5,
            suffix=".jpg",
            max_length=70,
        )
        self.assertLessEqual(len(str(path)), 70)
        self.assertIn("007_", path.name)

    def test_shortening_keeps_names_distinct(self):
        common = "Same prefix " * 10
        first = shorten_with_hash(common + "one", 40)
        second = shorten_with_hash(common + "two", 40)
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 40)

    def test_shorten_with_hash_handles_tiny_budgets(self):
        self.assertEqual(shorten_with_hash("abcdef", 0), "")
        self.assertLessEqual(len(shorten_with_hash("abcdef", 6)), 6)
        self.assertLessEqual(len(shorten_with_hash("abcdef", 3)), 3)


if __name__ == "__main__":
    unittest.main()
