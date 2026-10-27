from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .client import ROOT, ArtStationClient
from .core import (
    AccessDeniedError,
    ArtStationError,
    normalize_profile_url,
    write_manifest,
)
from .crawl import Crawler, CrawlResult
from .downloader import LocalDownloader, VerifyReport
from .gallery import write_gallery
from .store import DownloadState, default_state_path
from .ui import Console, Progress, SummaryData, format_bytes, format_duration, render_summary

EXIT_OK = 0
EXIT_FILE_FAILURES = 1
EXIT_FATAL = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="artstation-dl",
        description="Download the images of a public ArtStation portfolio to a local folder.",
        epilog=(
            "exit codes: 0 all files handled, 1 some files failed (re-run to retry), "
            "2 fatal error, 130 interrupted.\n"
            "examples:\n"
            "  artstation-dl kyz\n"
            "  artstation-dl https://www.artstation.com/kyz -o ./kyz --quality 4k\n"
            "  artstation-dl kyz --jobs 6 --delay 0.3\n"
            "  artstation-dl kyz --dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("profile", help="ArtStation profile URL or username")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("downloads"),
        metavar="DIR",
        help="output folder (default: downloads)",
    )
    parser.add_argument(
        "--quality",
        choices=["best", "8k", "4k", "large", "medium", "small"],
        default="best",
        help="preferred CDN size; smaller variants are used as fallbacks (default: best)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=3,
        metavar="N",
        help="parallel image transfers, 1-16 (default: 3)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="minimum seconds between any two requests (default: 1.0)",
    )
    parser.add_argument(
        "--max-projects", type=int, default=None, metavar="N", help="stop after the first N projects"
    )
    parser.add_argument(
        "--max-files", type=int, default=None, metavar="N", help="stop after the first N images"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="per-request timeout (default: 30)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        metavar="N",
        help="retries for transient network failures (default: 3)",
    )
    parser.add_argument(
        "--max-file-size",
        type=int,
        default=250 * 1024 * 1024,
        metavar="BYTES",
        help="refuse files larger than this (default: 250 MiB)",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        default=None,
        metavar="FILE",
        help="optional local Netscape cookie export; never upload or share this file",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="HTTP proxy for all requests, e.g. http://127.0.0.1:7890 "
        "(HTTP_PROXY/HTTPS_PROXY are honored when omitted)",
    )
    parser.add_argument(
        "--browser-path",
        type=Path,
        default=None,
        metavar="EXE",
        help="Chrome/Edge executable used only if HTTP is challenged",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="never fall back to a real browser session",
    )
    parser.add_argument("--overwrite", action="store_true", help="re-download files already saved")
    parser.add_argument(
        "--ignore-noai",
        action="store_true",
        help="do not skip projects explicitly marked NoAI; use only when authorized",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be downloaded without writing files or state",
    )
    parser.add_argument(
        "--gallery",
        action="store_true",
        help="write a browsable index.html gallery into the profile folder",
    )
    parser.add_argument(
        "--gallery-only",
        action="store_true",
        help="rebuild index.html from saved state without network access",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check saved files against recorded sizes/checksums without network access",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only errors and the summary")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    parser.add_argument("--version", action="version", version=f"artstation-dl {__version__}")
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.jobs < 1 or args.jobs > 16:
        parser.error("--jobs must be between 1 and 16")
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    if args.max_projects is not None and args.max_projects < 1:
        parser.error("--max-projects must be positive")
    if args.max_files is not None and args.max_files < 1:
        parser.error("--max-files must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.retries < 0:
        parser.error("--retries cannot be negative")
    if args.max_file_size < 1:
        parser.error("--max-file-size must be positive")


def _preflight(output_root: Path, console: Console) -> None:
    """Fail early with a readable message instead of a traceback mid-run."""

    try:
        output_root.mkdir(parents=True, exist_ok=True)
        probe = output_root / ".artstation-dl-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise ArtStationError(f"cannot write to output folder {output_root}: {exc}") from exc
    try:
        free = shutil.disk_usage(output_root).free
    except OSError:
        return
    if free < 200 * 1024 * 1024:
        console.warn(f"low disk space: {free / 1024 / 1024:.0f} MiB free in {output_root}")


def _install_signal_handlers(console: Console, cancel: threading.Event) -> dict[int, object]:
    previous: dict[int, object] = {}

    def handler(_signum: int, _frame: object) -> None:
        if cancel.is_set():
            raise KeyboardInterrupt
        cancel.set()
        console.warn("Stopping after the files in flight; press Ctrl+C again to abort now.")

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            continue
    return previous


def _restore_signal_handlers(previous: dict[int, object]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)  # type: ignore[arg-type]
        except (ValueError, OSError, TypeError):
            continue


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    _validate(parser, args)

    console = Console(
        color=False if args.no_color else None,
        quiet=args.quiet,
    )
    profile = None
    downloader: LocalDownloader | None = None
    state: DownloadState | None = None
    cancel = threading.Event()
    previous_handlers: dict[int, object] = {}
    progress: Progress | None = None
    started = time.monotonic()

    try:
        if args.cookie_file and not args.cookie_file.is_file():
            raise ArtStationError(f"cookie file does not exist: {args.cookie_file}")
        if args.browser_path and not args.browser_path.is_file():
            raise ArtStationError(f"browser executable does not exist: {args.browser_path}")
        profile = normalize_profile_url(args.profile)
        output_root = Path(args.output).expanduser()
        _preflight(output_root, console)

        manifest_path = output_root / profile.username / "manifest.jsonl"
        state_path = default_state_path(output_root)
        legacy_state = output_root / "state.json"
        offline = args.gallery_only or args.verify
        if offline and not state_path.exists() and not legacy_state.exists():
            raise ArtStationError(
                f"no saved download state in {output_root}; run a download first"
            )
        # A dry run on a fresh folder should not create any file at all, so it
        # keeps its state in memory. Existing state (including a v0.2
        # state.json) is still read so the report shows what is already saved.
        use_memory = args.dry_run and not state_path.exists() and not legacy_state.exists()
        state = DownloadState(Path(":memory:") if use_memory else state_path)
        api_base = _api_base()
        downloader = LocalDownloader(
            ArtStationClient(
                base_url=api_base,
                delay=args.delay,
                timeout=args.timeout,
                retries=args.retries,
                cookie_file=args.cookie_file,
                verbose=args.verbose,
                proxy_url=args.proxy,
            ),
            output_root,
            quality=args.quality,
            max_file_size=args.max_file_size,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            verbose=args.verbose,
            state=state,
        )
        if offline:
            return _offline(
                args=args,
                console=console,
                downloader=downloader,
                state=state,
                profile=profile,
                output_root=output_root,
                manifest_path=manifest_path,
                started=started,
            )
        known = state.counts(profile.username)

        console.heading(f"artstation-dl {__version__}")
        console.info(f"  Profile  {profile.canonical_url}")
        console.info(f"  Output   {output_root.resolve()}")
        console.info(
            f"  Quality  {args.quality} {console.bullet} jobs {args.jobs} "
            f"{console.bullet} {args.delay:g}s between requests"
        )
        if not args.ignore_noai:
            console.detail("  Projects marked NoAI are skipped. Use --ignore-noai if authorized.")
        if known.get("completed"):
            pending = known.get("failed", 0)
            message = f"  Resume   {known['completed']} file(s) already saved"
            if pending:
                message += f", {pending} failed earlier (retried now)"
            console.info(message)
        console.info("")

        previous_handlers = _install_signal_handlers(console, cancel)
        live = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not args.verbose
        progress = Progress(console, enabled=live)

        def run_pass(client: ArtStationClient) -> CrawlResult:
            downloader.client = client  # type: ignore[union-attr]
            crawler = Crawler(
                client,
                downloader,  # type: ignore[arg-type]
                state,  # type: ignore[arg-type]
                profile=profile,  # type: ignore[arg-type]
                console=console,
                progress=progress,  # type: ignore[arg-type]
                ignore_noai=args.ignore_noai,
                max_projects=args.max_projects,
                max_files=args.max_files,
                jobs=args.jobs,
                cancel=cancel,
            )
            return crawler.run()

        result: CrawlResult | None = None
        try:
            result = run_pass(downloader.client)
        except AccessDeniedError:
            if args.no_browser or cancel.is_set():
                raise
            console.warn(
                "HTTP access was challenged; retrying with a normal browser session..."
            )
            browser_client = _browser_client(args, profile, console)
            try:
                result = run_pass(browser_client)
            except AccessDeniedError as exc:
                raise AccessDeniedError(
                    "the browser session was challenged as well; manual verification may be required"
                ) from exc
            finally:
                browser_client.close()

        assert result is not None
        if result.artist:
            state.set_meta(f"artist:{profile.username}", result.artist)
        gallery_path = None
        if args.gallery and not args.dry_run:
            gallery_path = _write_gallery(console, state, profile, output_root, result.artist)
        code = _finish(
            console=console,
            result=result,
            profile_username=profile.username,
            output_root=output_root,
            manifest_path=manifest_path,
            state=state,
            dry_run=args.dry_run,
            started=started,
            cancel=cancel,
            gallery_path=gallery_path,
        )
        if not args.gallery and not args.dry_run:
            console.detail(
                "Tip: --gallery writes a browsable index.html "
                "(--gallery-only rebuilds it offline)."
            )
        return code
    except KeyboardInterrupt:
        console.error("aborted")
        return EXIT_INTERRUPTED
    except (ValueError, ArtStationError, OSError, RuntimeError) as exc:
        console.error(str(exc))
        if isinstance(exc, AccessDeniedError):
            console.detail(
                "This tool does not solve CAPTCHAs or bypass access controls. "
                "Finish the verification in the browser window, then run the command again."
            )
        return EXIT_FATAL
    finally:
        _restore_signal_handlers(previous_handlers)
        if progress is not None:
            progress.finish()
        if downloader is not None:
            downloader.close()
        elif state is not None:
            state.close()


def _api_base() -> str:
    """Resolve the API root, rejecting malformed overrides with a clear message."""

    raw = (os.environ.get("ARTSTATION_API_BASE") or ROOT).strip()
    if not raw:
        return ROOT
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ArtStationError(
            f"ARTSTATION_API_BASE must be an http:// or https:// URL, got {raw!r}"
        )
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ArtStationError(f"ARTSTATION_API_BASE has an invalid port: {raw!r}") from exc
    return raw.rstrip("/")


def _browser_client(args: argparse.Namespace, profile, console: Console):
    from .browser_client import ArtStationBrowserClient

    try:
        return ArtStationBrowserClient(
            profile_url=profile.canonical_url,
            browser_path=args.browser_path,
            delay=args.delay,
            timeout=args.timeout,
            retries=args.retries,
            cookie_file=args.cookie_file,
            verbose=args.verbose,
        )
    except ArtStationError as exc:
        console.error(str(exc))
        if not args.no_browser:
            console.detail(
                "Install Playwright and Chromium, or point --browser-path at Chrome/Edge. "
                "Use --no-browser to fail instead of opening a window."
            )
        raise


def _write_gallery(
    console: Console,
    state: DownloadState,
    profile,
    output_root: Path,
    artist: str,
) -> Path:
    destination = output_root / profile.username / "index.html"
    rows = state.rows_for_profile(profile.username)
    stats = write_gallery(
        destination,
        profile=profile.username,
        artist=artist or profile.username,
        rows=rows,
        output_root=output_root,
        version=__version__,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    console.detail(f"  Gallery  {destination} ({stats.images} image(s))")
    return destination


def _offline(
    *,
    args: argparse.Namespace,
    console: Console,
    downloader: LocalDownloader,
    state: DownloadState,
    profile,
    output_root: Path,
    manifest_path: Path,
    started: float,
) -> int:
    """Handle --verify / --gallery-only: local maintenance, no network."""

    console.heading(f"artstation-dl {__version__}")
    console.info(f"  Profile  {profile.canonical_url}")
    console.info(f"  Output   {output_root.resolve()}")
    artist = state.get_meta(f"artist:{profile.username}") or profile.username
    return_code = EXIT_OK

    if args.verify:
        console.info("")
        console.info("Checking saved files against their recorded size and checksum...")
        report = downloader.verify_library(profile.username)
        if not args.dry_run:
            write_manifest(manifest_path, state.rows_for_profile(profile.username))
        _print_verify(console, report)
        if not report.ok:
            return_code = EXIT_FILE_FAILURES

    if args.gallery_only:
        console.info("")
        destination = output_root / profile.username / "index.html"
        stats = write_gallery(
            destination,
            profile=profile.username,
            artist=artist,
            rows=state.rows_for_profile(profile.username),
            output_root=output_root,
            version=__version__,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        console.ok(f"Gallery written: {destination} ({stats.images} image(s), {stats.projects} project(s))")

    elapsed = max(0.0, time.monotonic() - started)
    console.detail(f"  Elapsed  {format_duration(elapsed)}")
    return return_code


def _print_verify(console: Console, report: VerifyReport) -> None:
    console.ok(f"Checked {report.checked} file(s), {format_bytes(report.bytes_checked)}")
    if report.missing:
        console.warn(f"{report.missing} file(s) missing on disk; re-run the download to fetch them")
    if report.corrupt:
        console.warn(
            f"{report.corrupt} file(s) corrupt; marked for re-download ({report.invalidated} updated)"
        )
    for label, reason in report.problems[:5]:
        console.detail(f"  {label}: {reason}")
    if len(report.problems) > 5:
        console.detail(f"  ... and {len(report.problems) - 5} more")


def _finish(
    *,
    console: Console,
    result: CrawlResult,
    profile_username: str,
    output_root: Path,
    manifest_path: Path,
    state: DownloadState,
    dry_run: bool,
    started: float,
    cancel: threading.Event,
    gallery_path: Path | None = None,
) -> int:
    if not dry_run:
        rows = state.rows_for_profile(profile_username)
        write_manifest(manifest_path, rows)
    else:
        manifest_path = manifest_path.with_name("manifest.jsonl (dry run)")
    elapsed = max(0.0, time.monotonic() - started)
    summary = SummaryData(
        profile=profile_username,
        artist=result.artist or profile_username,
        projects=result.projects,
        projects_cached=result.projects_cached,
        assets=result.assets,
        downloaded=result.downloaded,
        cached=result.cached,
        skipped=result.skipped,
        failed=result.failed,
        planned=result.planned,
        bytes_downloaded=result.bytes_downloaded,
        elapsed=elapsed,
        output_root=str(output_root.resolve() / profile_username),
        manifest_path=str(manifest_path),
        failures=result.failures,
        interrupted=result.interrupted or cancel.is_set(),
        dry_run=dry_run,
        gallery_path=str(gallery_path) if gallery_path else None,
    )
    console.write(render_summary(console, summary))
    if summary.interrupted:
        return EXIT_INTERRUPTED
    return EXIT_FILE_FAILURES if result.failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
