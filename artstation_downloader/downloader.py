from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import ArtStationClient
from .core import (
    AccessDeniedError,
    ImageAsset,
    build_destination,
    extension_from_content_type,
    image_dimensions,
    image_quality_candidates,
    now,
    resolve_download_path,
    safe_component,
)
from .store import DownloadState, default_state_path


@dataclass
class VerifyReport:
    checked: int = 0
    missing: int = 0
    corrupt: int = 0
    invalidated: int = 0
    bytes_checked: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.corrupt


class LocalDownloader:
    """Turns image assets into files on disk, safely and resumably."""

    def __init__(
        self,
        client: ArtStationClient,
        output_root: Path,
        *,
        quality: str = "best",
        max_file_size: int = 250 * 1024 * 1024,
        overwrite: bool = False,
        dry_run: bool = False,
        verbose: bool = False,
        state: DownloadState | None = None,
    ) -> None:
        self.client = client
        self.output_root = Path(output_root).resolve()
        self.quality = quality
        self.max_file_size = max(1, int(max_file_size))
        self.overwrite = overwrite
        self.dry_run = dry_run
        self.verbose = verbose
        self.state = state if state is not None else DownloadState(default_state_path(self.output_root))

    def close(self) -> None:
        self.state.close()

    # -- helpers ---------------------------------------------------------
    def _allowed_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        for base in self.client.media_hosts:
            if host == base or host.endswith("." + base):
                return True
        return False

    def _destination(self, asset: ImageAsset, extension: str | None = None) -> Path:
        name = safe_component(asset.original_name, fallback=f"asset-{asset.asset_id}")
        suffix = Path(name).suffix.lower()
        stem = Path(name).stem or f"asset-{asset.asset_id}"
        if extension:
            suffix = extension
        elif not suffix or suffix == ".bin":
            suffix = ".bin"
        return build_destination(
            self.output_root,
            asset.profile,
            asset.project_position,
            asset.project_title,
            asset.position,
            stem,
            suffix,
        )

    def skip(self, asset: ImageAsset, reason: str) -> dict[str, Any]:
        return self._record(asset, "skipped", None, error=reason)

    def fail(self, asset: ImageAsset, reason: str) -> dict[str, Any]:
        return self._record(asset, "failed", None, error=reason)

    # -- main entry point ------------------------------------------------
    def download(self, asset: ImageAsset) -> dict[str, Any]:
        if not self._allowed_url(asset.source_url):
            return self._record(asset, "skipped", None, error="media URL is outside ArtStation domains")

        existing = self.state.get(asset.key)
        if existing and existing.get("status") == "completed" and not self.overwrite:
            existing_path = self._resolve_record_path(existing.get("path"))
            if existing_path is not None and existing_path.exists():
                portable = self._portable_path(existing_path)
                if portable != existing.get("path") and not self.dry_run:
                    # The folder was moved or the record came from another
                    # machine: heal it to a portable path so the next run is
                    # quiet too.
                    existing = {**existing, "path": portable}
                    self.state.upsert(existing)
                return {**existing, "cached": True}

        candidates = image_quality_candidates(asset.source_url, self.quality)
        if self.dry_run:
            destination = self._destination(asset, extension_from_content_type(None, candidates[0]))
            record = self._record(asset, "planned", candidates[0], destination=destination)
            return {**record, "cached": False}

        last_error: str | None = None
        for index, candidate in enumerate(candidates):
            destination = self._destination(asset, extension_from_content_type(None, candidate))
            temp_path = destination.with_name(destination.name + ".part")
            is_last = index == len(candidates) - 1
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                content_type, byte_count, digest = self.client.stream_to_file(
                    candidate,
                    temp_path,
                    referer=asset.project_url,
                    max_bytes=self.max_file_size,
                )
                extension = extension_from_content_type(content_type, candidate)
                if destination.suffix.lower() != extension:
                    destination = self._destination(asset, extension)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                # The temp file keeps the name the client wrote to; only the
                # final name follows the detected content type.
                os.replace(temp_path, destination)
                width, height = image_dimensions(destination)
                record = self._record(
                    asset,
                    "completed",
                    candidate,
                    destination=destination,
                    byte_count=byte_count,
                    sha256=digest,
                    content_type=content_type,
                    width=width,
                    height=height,
                )
                return {**record, "cached": False}
            except AccessDeniedError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                # Any unusable variant falls through to the next documented
                # CDN size. A partial of a different URL must not be resumed.
                last_error = str(exc)
                if not is_last:
                    temp_path.unlink(missing_ok=True)
                if self.verbose:
                    print(f"[fallback] {candidate}: {last_error}", flush=True)
        return {**self._record(asset, "failed", None, error=last_error or "all candidates failed"), "cached": False}

    # -- integrity check -------------------------------------------------
    def verify_library(self, profile: str) -> VerifyReport:
        """Compare completed files on disk with the recorded size and checksum.

        A corrupt file is marked failed so the next run downloads it again;
        a missing file needs no state change because a rerun re-downloads
        anything whose path is gone.
        """

        report = VerifyReport()
        for row in self.state.rows_for_profile(profile):
            if row.get("status") != "completed":
                continue
            label = str(row.get("path") or row.get("asset_id") or "asset")
            path = self._resolve_record_path(row.get("path"))
            if path is None or not path.is_file():
                report.missing += 1
                report.problems.append((label, "file is missing on disk"))
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                report.missing += 1
                report.problems.append((label, f"cannot read file: {exc}"))
                continue
            reason = self._mismatch_reason(row, path, size)
            if reason is None:
                report.checked += 1
                report.bytes_checked += size
                continue
            report.corrupt += 1
            report.problems.append((label, reason))
            if not self.dry_run:
                self.state.upsert(
                    {**row, "status": "failed", "error": f"{reason}; will re-download", "updated_at": now()}
                )
                report.invalidated += 1
        return report

    @staticmethod
    def _mismatch_reason(row: dict[str, Any], path: Path, size: int) -> str | None:
        expected_size = row.get("bytes")
        if expected_size is not None:
            try:
                if int(expected_size) != size:
                    return f"size mismatch: expected {expected_size} bytes, found {size}"
            except (TypeError, ValueError):
                pass
        expected_hash = row.get("sha256")
        if expected_hash:
            digest = _sha256_file(path)
            if digest != expected_hash:
                return "checksum mismatch"
        return None

    # -- persistence -----------------------------------------------------
    def _record(
        self,
        asset: ImageAsset,
        status: str,
        selected_url: str | None,
        *,
        destination: Path | None = None,
        byte_count: int | None = None,
        sha256: str | None = None,
        width: int | None = None,
        height: int | None = None,
        content_type: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        record_path = destination or self._destination(asset)
        values = {
            "asset_key": asset.key,
            "profile": asset.profile,
            "project_hash_id": asset.project_hash_id,
            "project_position": asset.project_position,
            "project_title": asset.project_title,
            "asset_id": asset.asset_id,
            "asset_position": asset.position,
            "source_url": asset.source_url,
            # Keep manifests portable when the output directory is moved to a
            # different machine. Absolute paths from older state files remain
            # supported by _resolve_record_path above.
            "path": self._portable_path(record_path),
            "status": status,
            "bytes": byte_count,
            "sha256": sha256,
            "width": width,
            "height": height,
            "content_type": content_type,
            "selected_url": selected_url,
            "error": error,
            "updated_at": now(),
        }
        # A dry run must leave the folder (including its state) untouched.
        if not self.dry_run:
            self.state.upsert(values)
        return values

    def _portable_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.output_root).as_posix()
        except ValueError:
            # Defensive fallback for callers that provide a destination outside
            # the configured output root.
            return str(path)

    def _resolve_record_path(self, value: Any) -> Path | None:
        return resolve_download_path(value, self.output_root)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
