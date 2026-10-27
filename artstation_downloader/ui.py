"""Terminal presentation: colors, a live progress line, and the summary.

Deliberately dependency-free. On Windows the console falls back to plain text
when virtual-terminal support is unavailable, and every code path works when
stdout is redirected to a file.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from typing import TextIO

RESET = "\x1b[0m"
STYLES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "cyan": "36",
    "grey": "90",
}
SPINNER_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_ASCII = "|/-\\"


def enable_windows_vt() -> bool:
    """Turn on ANSI processing for the Windows console; False if unsupported."""

    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # pragma: no cover - platform specific
        return False


def supports_unicode(stream: TextIO) -> bool:
    encoding = (getattr(stream, "encoding", "") or "").lower()
    return "utf" in encoding


def format_bytes(value: float | int | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_rate(bytes_per_second: float | None) -> str:
    return f"{format_bytes(bytes_per_second)}/s"


def truncate(text: str, width: int, *, keep_tail: bool = False, ellipsis: str = "…") -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if keep_tail:
        return ellipsis + text[-(width - len(ellipsis)) :]
    return text[: max(0, width - len(ellipsis))] + ellipsis


class Console:
    """Serializes all terminal writes so progress and logs never interleave.

    Symbol choice follows the stream encoding: a Chinese Windows console piped
    into a file encodes as cp936, where ``✓`` would degrade to ``?``. In that
    case the plain-ASCII forms are used instead.
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        color: bool | None = None,
        quiet: bool = False,
    ) -> None:
        self.stream = stream or sys.stdout
        self.lock = threading.RLock()
        self.quiet = quiet
        self.unicode = supports_unicode(self.stream)
        self.color = self._detect_color(color)
        self._progress: Progress | None = None

    @property
    def mark_ok(self) -> str:
        return "✓" if self.unicode else "OK"

    @property
    def mark_fail(self) -> str:
        return "✗" if self.unicode else "x"

    @property
    def bullet(self) -> str:
        return "·" if self.unicode else "-"

    @property
    def rule_char(self) -> str:
        return "─" if self.unicode else "-"

    @property
    def ellipsis(self) -> str:
        return "…" if self.unicode else "..."

    def _detect_color(self, color: bool | None) -> bool:
        if color is not None:
            return color
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("TERM", "").lower() == "dumb":
            return False
        isatty = getattr(self.stream, "isatty", None)
        if not isatty or not isatty():
            return False
        return enable_windows_vt()

    def style(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        codes = ";".join(STYLES[style] for style in styles if style in STYLES)
        return f"\x1b[{codes}m{text}{RESET}" if codes else text

    # -- progress coupling ----------------------------------------------
    def attach(self, progress: Progress | None) -> None:
        self._progress = progress

    def _clear_progress(self) -> None:
        if self._progress is not None:
            self._progress.clear()

    def write(self, text: str = "", *, end: str = "\n", err: bool = False) -> None:
        stream = sys.stderr if err else self.stream
        with self.lock:
            if not err:
                self._clear_progress()
            try:
                stream.write(text + end)
                stream.flush()
            except (BrokenPipeError, OSError, ValueError):
                # A closed pipe (for example `| head`) must not crash a run.
                pass

    # -- message helpers -------------------------------------------------
    def info(self, text: str) -> None:
        self.write(text)

    def detail(self, text: str) -> None:
        if not self.quiet:
            self.write(self.style(text, "dim"))

    def ok(self, text: str) -> None:
        if not self.quiet:
            self.write(f"{self.style(self.mark_ok, 'green')} {text}")

    def warn(self, text: str) -> None:
        if not self.quiet:
            self.write(f"{self.style('!', 'yellow')} {text}")

    def error(self, text: str) -> None:
        self.write(f"{self.style(self.mark_fail, 'red')} {text}", err=True)

    def failure(self, text: str) -> None:
        self.write(f"{self.style(self.mark_fail, 'red')} {text}")

    def heading(self, text: str) -> None:
        if not self.quiet:
            self.write(self.style(text, "bold"))


class Progress:
    """A single self-updating status line, with a plain-text fallback."""

    INTERVAL = 0.1

    def __init__(self, console: Console, *, enabled: bool, width: int | None = None) -> None:
        self.console = console
        self.enabled = enabled and not console.quiet
        self._width = width
        self.projects_done = 0
        self.projects_total: int | None = None
        self.assets_seen = 0
        self.downloaded = 0
        self.cached = 0
        self.skipped = 0
        self.failed = 0
        self.pending = 0
        self.bytes_downloaded = 0
        self.current = ""
        self._started = time.monotonic()
        self._last_render = 0.0
        self._frame = 0
        self._visible = False
        self._lock = threading.Lock()
        self.frames = SPINNER_UNICODE if console.unicode else SPINNER_ASCII
        if self.enabled:
            console.attach(self)

    # -- state -----------------------------------------------------------
    def set_projects(self, done: int, total: int | None) -> None:
        self.projects_done = done
        self.projects_total = total

    def set_current(self, label: str) -> None:
        with self._lock:
            self.current = label

    def set_counts(self, **counts: int) -> None:
        for name, value in counts.items():
            if hasattr(self, name):
                setattr(self, name, value)

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._started)

    # -- rendering -------------------------------------------------------
    def clear(self) -> None:
        if not self.enabled or not self._visible:
            return
        try:
            self.console.stream.write("\r\x1b[K")
            self.console.stream.flush()
        except (OSError, ValueError):
            pass
        self._visible = False

    def tick(self, *, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self._last_render < self.INTERVAL:
            return
        self._last_render = now
        self._frame += 1
        line = self.render()
        with self.console.lock:
            try:
                self.console.stream.write("\r\x1b[K" + line)
                self.console.stream.flush()
            except (OSError, ValueError):
                return
        self._visible = True

    def render(self) -> str:
        spinner = self.frames[self._frame % len(self.frames)]
        rate = self.bytes_downloaded / self.elapsed if self.elapsed > 0 else 0.0
        parts = [f"{self.console.style(spinner, 'cyan')} "]
        if self.projects_total:
            parts.append(f"{self.projects_done}/{self.projects_total} projects")
        else:
            parts.append(f"{self.projects_done} projects")
        parts.append(f"{self.downloaded + self.cached} files")
        if self.pending:
            parts.append(f"+{self.pending} queued")
        if self.failed:
            parts.append(self.console.style(f"{self.failed} failed", "red"))
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        parts.append(format_bytes(self.bytes_downloaded))
        parts.append(format_rate(rate))
        eta = self.eta_seconds()
        if eta is not None:
            parts.append(f"ETA {format_duration(eta)}")
        separator = f" {self.console.bullet} "
        head = separator.join(parts)
        width = self._terminal_width()
        remaining = width - len(head) - 4
        current = (
            truncate(self.current, remaining, keep_tail=True, ellipsis=self.console.ellipsis)
            if self.current and remaining > 8
            else ""
        )
        line = f"{head}{separator}{self.console.style(current, 'dim')}" if current else head
        return truncate(line, max(20, width - 1), ellipsis=self.console.ellipsis)

    def _terminal_width(self) -> int:
        return self._width or shutil.get_terminal_size((100, 24)).columns

    def eta_seconds(self) -> float | None:
        if not self.projects_total or self.projects_done < 2:
            return None
        files = self.downloaded + self.cached
        if self.pending and files == 0:
            # Enumeration outran the downloads; there is no honest estimate yet.
            return None
        per_project = self.elapsed / max(1, self.projects_done)
        estimate = per_project * max(0, self.projects_total - self.projects_done)
        if self.pending and files:
            estimate += (self.elapsed / files) * self.pending
        return max(0.0, estimate)

    def finish(self) -> None:
        if self.enabled and self._visible:
            with self.console.lock:
                try:
                    self.console.stream.write("\r\x1b[K")
                    self.console.stream.flush()
                except (OSError, ValueError):
                    pass
            self._visible = False
        self.console.attach(None)


@dataclass
class SummaryData:
    profile: str
    artist: str
    projects: int
    projects_cached: int
    assets: int
    downloaded: int
    cached: int
    skipped: int
    failed: int
    planned: int
    bytes_downloaded: int
    elapsed: float
    output_root: str
    manifest_path: str
    failures: list[tuple[str, str]]
    interrupted: bool = False
    dry_run: bool = False
    gallery_path: str | None = None


def render_summary(console: Console, data: SummaryData) -> str:
    """Return the end-of-run report as text (also used by tests)."""

    width = max(46, min(78, shutil.get_terminal_size((78, 24)).columns))
    rule = console.rule_char * min(width, 60)
    lines: list[str] = ["", console.style(rule, "grey")]
    title = "Interrupted" if data.interrupted else ("Dry run complete" if data.dry_run else "Done")
    lines.append(
        console.style(f" {title} {console.bullet} {data.artist} (@{data.profile})", "bold")
    )
    rate = data.bytes_downloaded / data.elapsed if data.elapsed > 0 else 0.0
    lines.append(
        f"  Downloaded  {data.downloaded} file(s), {format_bytes(data.bytes_downloaded)}"
        f"  ({format_rate(rate)})"
    )
    if data.cached:
        lines.append(f"  Already had {data.cached} file(s)")
    if data.planned:
        lines.append(f"  Planned     {data.planned} file(s)")
    if data.skipped:
        lines.append(f"  Skipped     {data.skipped} file(s)")
    if data.failed:
        lines.append(console.style(f"  Failed      {data.failed} file(s)", "red"))
    lines.append(f"  Projects    {data.projects}")
    lines.append(f"  Elapsed     {format_duration(data.elapsed)}")
    lines.append(f"  Output      {data.output_root}")
    lines.append(f"  Manifest    {data.manifest_path}")
    if data.gallery_path:
        lines.append(f"  Gallery     {data.gallery_path}")
    if data.interrupted:
        lines.append(
            console.style("  Resume with the same command — finished files are skipped.", "yellow")
        )
    if data.failures:
        lines.append("")
        lines.append(console.style("  Failed items", "red"))
        for label, reason in data.failures[:8]:
            lines.append(f"    {label}")
            lines.append(console.style(f"      {reason}", "dim"))
        if len(data.failures) > 8:
            lines.append(
                console.style(f"    {console.ellipsis} and {len(data.failures) - 8} more in manifest", "dim")
            )
        lines.append(
            console.style("  Re-run the same command to retry only the failed files.", "dim")
        )
    lines.append(console.style(rule, "grey"))
    return "\n".join(lines)
