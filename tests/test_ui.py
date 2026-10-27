import contextlib
import io
import unittest

from artstation_downloader.ui import (
    Console,
    Progress,
    SummaryData,
    format_bytes,
    format_duration,
    format_rate,
    render_summary,
    truncate,
)


class FormatTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(999), "999 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(1536 * 1024), "1.5 MB")

    def test_duration(self):
        self.assertEqual(format_duration(None), "--:--")
        self.assertEqual(format_duration(0), "00:00")
        self.assertEqual(format_duration(75), "01:15")
        self.assertEqual(format_duration(3725), "1:02:05")

    def test_rate_and_truncate(self):
        self.assertEqual(format_rate(2048), "2.0 KB/s")
        self.assertEqual(truncate("short", 10), "short")
        self.assertEqual(truncate("abcdefghij", 5), "abcd…")
        self.assertEqual(truncate("abcdefghij", 5, keep_tail=True), "…ghij")


class ConsoleTests(unittest.TestCase):
    def test_plain_console_has_no_ansi(self):
        stream = io.StringIO()
        console = Console(stream, color=False)
        console.write(console.style("hello", "green"))
        self.assertEqual(stream.getvalue(), "hello\n")

    def test_color_console_uses_ansi(self):
        stream = io.StringIO()
        console = Console(stream, color=True)
        console.write(console.style("hello", "green"))
        self.assertIn("\x1b[32m", stream.getvalue())
        self.assertTrue(stream.getvalue().endswith("\x1b[0m\n"))

    def test_quiet_console_suppresses_detail_messages(self):
        stream = io.StringIO()
        errors = io.StringIO()
        console = Console(stream, color=False, quiet=True)
        console.detail("hidden")
        console.ok("hidden too")
        with contextlib.redirect_stderr(errors):
            console.error("shown")
        self.assertNotIn("hidden", stream.getvalue())
        self.assertIn("shown", errors.getvalue())

    def test_quite_console_still_writes_explicit_lines(self):
        stream = io.StringIO()
        console = Console(stream, color=False, quiet=True)
        console.write("always")
        self.assertIn("always", stream.getvalue())


class ProgressTests(unittest.TestCase):
    def test_disabled_progress_renders_nothing(self):
        stream = io.StringIO()
        console = Console(stream, color=False)
        progress = Progress(console, enabled=False)
        progress.set_projects(3, 10)
        progress.tick(force=True)
        progress.finish()
        self.assertEqual(stream.getvalue(), "")

    def test_progress_line_contains_counters_and_eta(self):
        stream = io.StringIO()
        console = Console(stream, color=False)
        progress = Progress(console, enabled=True, width=200)
        progress.set_projects(5, 10)
        progress.set_counts(downloaded=3, cached=2, failed=1, pending=4, bytes_downloaded=1024 * 1024)
        progress.set_current("artist/0001_Project/003_image.jpg")
        progress._started -= 10  # pretend ten seconds have passed
        progress.tick(force=True)
        line = stream.getvalue()
        self.assertIn("5/10 projects", line)
        self.assertIn("5 files", line)
        self.assertIn("+4 queued", line)
        self.assertIn("1.0 MB", line)
        self.assertIn("1 failed", line)
        self.assertIn("ETA", line)
        self.assertIn("003_image.jpg", line)
        self.assertTrue(line.startswith("\r\x1b[K"))
        progress.finish()

    def test_eta_is_unknown_while_downloads_lag_enumeration(self):
        console = Console(io.StringIO(), color=False)
        progress = Progress(console, enabled=True)
        progress.set_projects(8, 8)
        progress.set_counts(pending=6)
        self.assertIsNone(progress.eta_seconds())
        progress.set_counts(downloaded=2)
        self.assertIsNotNone(progress.eta_seconds())
        progress.finish()

    def test_finish_clears_the_line(self):
        stream = io.StringIO()
        console = Console(stream, color=False)
        progress = Progress(console, enabled=True)
        progress.tick(force=True)
        progress.finish()
        self.assertTrue(stream.getvalue().endswith("\r\x1b[K"))


class SummaryTests(unittest.TestCase):
    def _data(self, **overrides):
        values = dict(
            profile="artist",
            artist="Artist Name",
            projects=3,
            projects_cached=0,
            assets=10,
            downloaded=4,
            cached=2,
            skipped=1,
            failed=0,
            planned=0,
            bytes_downloaded=4 * 1024 * 1024,
            elapsed=64.0,
            output_root="D:/out/artist",
            manifest_path="D:/out/artist/manifest.jsonl",
            failures=[],
        )
        values.update(overrides)
        return SummaryData(**values)

    def test_summary_reports_counts_and_rate(self):
        console = Console(io.StringIO(), color=False)
        text = render_summary(console, self._data())
        self.assertIn("Done", text)
        self.assertIn("@artist", text)
        self.assertIn("Downloaded  4 file(s), 4.0 MB  (64.0 KB/s)", text)
        self.assertIn("Already had 2 file(s)", text)
        self.assertIn("Manifest", text)

    def test_summary_lists_failures_and_retry_hint(self):
        console = Console(io.StringIO(), color=False)
        data = self._data(failed=2, failures=[("a/1.jpg", "HTTP 404"), ("a/2.jpg", "timeout")])
        text = render_summary(console, data)
        self.assertIn("Failed      2 file(s)", text)
        self.assertIn("HTTP 404", text)
        self.assertIn("Re-run the same command", text)

    def test_interrupted_summary_mentions_resume(self):
        console = Console(io.StringIO(), color=False)
        text = render_summary(console, self._data(interrupted=True))
        self.assertIn("Interrupted", text)
        self.assertIn("Resume with the same command", text)


if __name__ == "__main__":
    unittest.main()
