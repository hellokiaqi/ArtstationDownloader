"""Crawl a portfolio: enumerate projects, download assets, report progress.

Enumeration stays sequential and rate-limited, while image downloads run on a
small worker pool. A bounded queue keeps the pipeline full without buffering
thousands of pending jobs, and every worker reports back through an event
queue so a single thread owns the terminal.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .client import ArtStationClient
from .core import AccessDeniedError, ImageAsset, Profile, SchemaError
from .downloader import LocalDownloader
from .store import DownloadState
from .ui import Console, Progress, format_bytes


@dataclass
class CrawlResult:
    artist: str = ""
    projects: int = 0
    projects_cached: int = 0
    assets: int = 0
    downloaded: int = 0
    cached: int = 0
    skipped: int = 0
    failed: int = 0
    planned: int = 0
    bytes_downloaded: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    interrupted: bool = False
    elapsed: float = 0.0

    def merge(self, other: CrawlResult) -> CrawlResult:
        """Combine two passes (for example HTTP followed by a browser retry)."""

        return CrawlResult(
            artist=other.artist or self.artist,
            projects=max(self.projects, other.projects),
            projects_cached=max(self.projects_cached, other.projects_cached),
            assets=max(self.assets, other.assets),
            downloaded=self.downloaded + other.downloaded,
            cached=max(self.cached, other.cached),
            skipped=max(self.skipped, other.skipped),
            failed=other.failed,
            planned=max(self.planned, other.planned),
            bytes_downloaded=self.bytes_downloaded + other.bytes_downloaded,
            failures=other.failures or self.failures,
            interrupted=self.interrupted or other.interrupted,
            elapsed=self.elapsed + other.elapsed,
        )


class Crawler:
    def __init__(
        self,
        client: ArtStationClient,
        downloader: LocalDownloader,
        state: DownloadState,
        *,
        profile: Profile,
        console: Console,
        progress: Progress,
        ignore_noai: bool = False,
        max_projects: int | None = None,
        max_files: int | None = None,
        jobs: int = 1,
        cancel: threading.Event | None = None,
    ) -> None:
        self.client = client
        self.downloader = downloader
        self.state = state
        self.profile = profile
        self.console = console
        self.progress = progress
        self.ignore_noai = ignore_noai
        self.max_projects = max_projects
        self.max_files = max_files
        self.jobs = max(1, int(jobs))
        self.cancel = cancel or threading.Event()

        self.result = CrawlResult()
        self._queue: queue.Queue[ImageAsset | None] = queue.Queue(maxsize=max(2, self.jobs * 2))
        self._events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._workers_alive = 0
        self._aborted = False
        self._fatal: BaseException | None = None
        self._started = 0.0
        self._projects_done = 0

    # -- lifecycle -------------------------------------------------------
    def run(self) -> CrawlResult:
        self._started = time.monotonic()
        info = self.client.get_user_info(self.profile.username)
        self.result.artist = str(
            info.get("full_name") or info.get("username") or self.profile.username
        )
        self.console.heading(
            f"{self.result.artist} {self.console.bullet} @{self.profile.username}"
        )
        self._start_workers()
        try:
            self._enumerate()
        finally:
            self._shutdown()
        self.result.elapsed = self._elapsed()
        if self._fatal is not None:
            raise self._fatal
        self.result.interrupted = self._interrupted()
        return self.result

    def _start_workers(self) -> None:
        for index in range(self.jobs):
            worker = threading.Thread(
                target=self._worker, name=f"artstation-dl-{index + 1}", daemon=True
            )
            worker.start()
            self._workers.append(worker)
        self._workers_alive = len(self._workers)

    def _enumerate(self) -> None:
        projects = self.client.iter_projects(self.profile.username, max_projects=self.max_projects)
        for project in projects:
            if self._should_stop():
                break
            if self.max_projects is not None and self.result.projects >= self.max_projects:
                break
            assets = self._load_project(project)
            self.result.projects += 1
            self._drain_events()
            total = self.client.project_total
            if total is not None and self.max_projects is not None:
                total = min(total, self.max_projects)
            self.progress.set_projects(self.result.projects, total)
            self._tick(force=True)
            for asset in assets:
                if self.max_files is not None and self.result.assets >= self.max_files:
                    break
                if not self._submit(asset):
                    break
                self.result.assets += 1
            if self.max_files is not None and self.result.assets >= self.max_files:
                break

    # -- project loading --------------------------------------------------
    def _load_project(self, project) -> list[ImageAsset]:
        cached = self.state.get_project(self.profile.username, project.hash_id)
        if cached and project.updated_at and cached.get("updated_at") == project.updated_at:
            snapshot = _decode_snapshot(cached.get("snapshot"))
            if snapshot is not None:
                self.result.projects_cached += 1
                return self.client.assets_from_snapshot(self.profile.username, project, snapshot)

        snapshot: dict[str, Any] | None = None
        assets: list[ImageAsset] = []
        if project.assets_count == 0:
            snapshot = {"title": project.title, "url": project.url, "assets": []}
        else:
            try:
                payload = self.client.fetch_project(project)
                assets = self.client.assets_from_payload(self.profile.username, project, payload)
                snapshot = self.client.project_snapshot(project, payload, assets)
            except AccessDeniedError:
                raise
            except (SchemaError, RuntimeError, ValueError) as exc:
                self.console.warn(f"skipping project {project.hash_id}: {exc}")
                return []
        if not self.downloader.dry_run:
            self.state.upsert_project(
                profile=self.profile.username,
                hash_id=project.hash_id,
                position=project.position,
                title=project.title,
                updated_at=project.updated_at,
                assets_count=project.assets_count,
                snapshot=snapshot,
            )
        return assets

    # -- producer/consumer plumbing ---------------------------------------
    def _submit(self, asset: ImageAsset) -> bool:
        while not self._should_stop():
            # Drain finished downloads first: otherwise the counters (and the
            # progress line) go stale while enumeration is busy.
            self._drain_events()
            try:
                self._queue.put(asset, timeout=0.05)
                return True
            except queue.Full:
                self._pump(0.05)
                self._tick()
        return False

    def _drain_events(self) -> int:
        handled = 0
        while self._pump(0):
            handled += 1
        return handled

    def _worker(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                if self._should_stop():
                    continue
                self._events.put(("start", item))
                try:
                    record = self._handle_asset(item)
                except AccessDeniedError as exc:
                    self._events.put(("fatal", exc))
                    self._aborted = True
                    return
                except Exception as exc:  # a worker must never die silently
                    record = self.downloader.fail(item, f"unexpected error: {exc}")
                self._events.put(("done", item, record))
        finally:
            self._events.put(("worker_exit", None))

    def _handle_asset(self, asset: ImageAsset) -> dict[str, Any]:
        if asset.no_ai and not self.ignore_noai:
            return self.downloader.skip(asset, "project is marked NoAI")
        return self.downloader.download(asset)

    def _shutdown(self) -> None:
        sentinels = 0
        while self._workers_alive > 0 and sentinels < len(self._workers):
            try:
                self._queue.put(None, timeout=0.05)
                sentinels += 1
            except queue.Full:
                pass
            self._pump(0.05)
            self._tick()
        while self._workers_alive > 0:
            self._pump(0.1)
            self._tick()
        for worker in self._workers:
            worker.join(timeout=5.0)
        # Drain whatever finished after the workers were accounted for.
        while self._pump(0):
            pass
        self.progress.finish()

    # -- event handling ---------------------------------------------------
    def _pump(self, timeout: float) -> bool:
        try:
            if timeout > 0:
                event = self._events.get(timeout=timeout)
            else:
                event = self._events.get_nowait()
        except queue.Empty:
            return False
        self._handle_event(event)
        return True

    def _handle_event(self, event: tuple[Any, ...]) -> None:
        kind = event[0]
        if kind == "start":
            asset: ImageAsset = event[1]
            self.progress.set_current(f"{asset.project_title}/{asset.original_name}")
        elif kind == "done":
            self._report_success(event[1], event[2])
        elif kind == "fatal":
            self._fatal = event[1]
            self.progress.set_current("")
        elif kind == "worker_exit":
            self._workers_alive -= 1

    def _report_success(self, asset: ImageAsset, record: dict[str, Any]) -> None:
        status = record.get("status")
        label = str(record.get("path") or asset.original_name)
        if status == "completed":
            if record.get("cached"):
                self.result.cached += 1
            else:
                self.result.downloaded += 1
                self.result.bytes_downloaded += int(record.get("bytes") or 0)
                if not self.progress.enabled:
                    size = format_bytes(record.get("bytes"))
                    mark = self.console.style(self.console.mark_ok, "green")
                    self.console.write(f"{mark} {size:>9}  {label}")
        elif status == "skipped":
            self.result.skipped += 1
            if not self.console.quiet:
                self.console.detail(f"- {label} ({record.get('error') or 'skipped'})")
        elif status == "planned":
            self.result.planned += 1
            if not self.progress.enabled:
                self.console.write(f"{self.console.style('..', 'cyan')} {'plan':>9}  {label}")
        elif status == "failed":
            self.result.failed += 1
            reason = str(record.get("error") or "failed")
            self.result.failures.append((label, reason))
            self.console.failure(f"{label} — {reason}")
        self._tick()

    def _tick(self, *, force: bool = False) -> None:
        finished = (
            self.result.downloaded
            + self.result.cached
            + self.result.skipped
            + self.result.failed
            + self.result.planned
        )
        self.progress.set_counts(
            downloaded=self.result.downloaded,
            cached=self.result.cached,
            skipped=self.result.skipped,
            failed=self.result.failed,
            pending=max(0, self.result.assets - finished),
            bytes_downloaded=self.result.bytes_downloaded,
        )
        self.progress.tick(force=force)

    # -- state helpers ----------------------------------------------------
    def _should_stop(self) -> bool:
        return self._aborted or self.cancel.is_set()

    def _interrupted(self) -> bool:
        return self.cancel.is_set() and not self._aborted

    def _elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._started)


def _decode_snapshot(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
