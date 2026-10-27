import unittest

from artstation_downloader.browser_client import _with_download_flag


class BrowserClientTests(unittest.TestCase):
    def test_download_flag_is_added_without_destroying_query(self):
        self.assertEqual(
            _with_download_flag("https://cdnb.artstation.com/image.jpg"),
            "https://cdnb.artstation.com/image.jpg?dl=1",
        )
        self.assertEqual(
            _with_download_flag("https://cdnb.artstation.com/image.jpg?12345"),
            "https://cdnb.artstation.com/image.jpg?12345&dl=1",
        )
        self.assertEqual(
            _with_download_flag("https://cdnb.artstation.com/image.jpg?12345&dl=1"),
            "https://cdnb.artstation.com/image.jpg?12345&dl=1",
        )


if __name__ == "__main__":
    unittest.main()
