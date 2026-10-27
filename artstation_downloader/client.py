from __future__ import annotations

import errno
import hashlib
import json
import random
import threading
import time
from collections.abc import Iterator
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .core import (
    AccessDeniedError,
    ImageAsset,
    ProjectRef,
    SchemaError,
    filename_from_url,
    parse_netscape_cookies,
    sniff_image,
)
from .http_session import HttpResponse, HttpSession, HttpSessionError

ROOT = "https://www.artstation.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)
CHALLENGE_MARKERS = (
    "just a moment...",
    "challenge-platform",
    "cf-chl-",
    "attention required! | cloudflare",
)
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
DOWNLOAD_CHUNK = 256 * 1024
# Retrying cannot fix a full disk, a read-only volume, or a permission denial.
_PERMANENT_OS_ERRORS = frozenset({errno.ENOSPC, errno.EACCES, errno.EROFS, errno.EPERM, errno.EDQUOT})


class HttpStatusError(RuntimeError):
    """The server answered with an unexpected, non-retryable status."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} while requesting {url}")
        self.status = status
        self.url = url


class RateLimiter:
    """Keep a minimum interval between request starts, across threads.

    The wait happens while the lock is held on purpose: that is what turns
    parallel workers into one politely spaced request stream.
    """

    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, float(delay))
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if self.delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self.delay


class ArtStationClient:
    """Small, rate-limited client for the public ArtStation JSON surface."""

    def __init__(
        self,
        *,
        base_url: str = ROOT,
        delay: float = 1.0,
        timeout: float = 30.0,
        retries: int = 3,
        cookie_file: Path | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        verbose: bool = False,
        proxy_url: str | None = None,
        trust_env_proxy: bool = True,
    ) -> None:
        self.base_url = (base_url or ROOT).rstrip("/")
        self.delay = max(0.0, float(delay))
        self.timeout = max(1.0, float(timeout))
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self.verbose = verbose
        self.limiter = RateLimiter(self.delay)
        self.cookie_jar = CookieJar()
        # Filled in by iter_projects once the first page reports total_count.
        self.project_total: int | None = None
        if cookie_file:
            self._load_cookies(cookie_file)
        self.session = HttpSession(
            user_agent=self.user_agent,
            timeout=self.timeout,
            cookie_jar=self.cookie_jar,
            proxy_url=proxy_url,
            trust_env_proxy=trust_env_proxy,
        )

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self.session.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    @property
    def media_hosts(self) -> tuple[str, ...]:
        base_host = self.base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        return ("artstation.com", base_host)

    # -- cookies ---------------------------------------------------------
    def _load_cookies(self, path: Path) -> None:
        for domain, cookie_path, name, value in parse_netscape_cookies(path):
            self.cookie_jar.set_cookie(
                Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=domain.startswith("."),
                    domain_initial_dot=domain.startswith("."),
                    path=cookie_path,
                    path_specified=True,
                    secure=False,
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )

    # -- transport -------------------------------------------------------
    def _open(
        self,
        url: str,
        *,
        accept: str,
        referer: str | None = None,
        accept_encoding: str | None = None,
    ) -> HttpResponse:
        self.limiter.acquire()
        headers = {"Accept": accept}
        if accept_encoding:
            headers["Accept-Encoding"] = accept_encoding
        response = self.session.open(
            url,
            headers=headers,
            referer=referer or f"{self.base_url}/",
        )
        self._log(f"[http] {response.status} {url}")
        return response

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> None:
        try:
            wait = min(60.0, max(1.0, float(retry_after))) if retry_after else None
        except ValueError:
            wait = None
        if wait is None:
            wait = min(60.0, (2**attempt) + random.uniform(0.0, 0.5))
        time.sleep(wait)

    @staticmethod
    def _reject_challenge(body: bytes, status: int, url: str) -> None:
        sample = body[:100_000].decode("utf-8", errors="ignore").lower()
        if status in {401, 403} or any(marker in sample for marker in CHALLENGE_MARKERS):
            raise AccessDeniedError(
                f"ArtStation returned an access challenge (HTTP {status}) for {url}. "
                "Use a normal browser session if you are authorized, or stop the job."
            )

    def _retryable(self, status: int) -> bool:
        return status in RETRYABLE_STATUSES

    # -- JSON surface ----------------------------------------------------
    def get_json(self, url: str, *, referer: str | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._open(
                    url,
                    accept="application/json, text/plain, */*",
                    referer=referer,
                    accept_encoding="gzip",
                )
                with response:
                    status = response.status
                    body = response.read_all()
                self._reject_challenge(body, status, url)
                if self._retryable(status) and attempt < self.retries:
                    self._backoff(attempt, response.getheader("Retry-After"))
                    continue
                if status < 200 or status >= 300:
                    raise HttpStatusError(status, url)
            except AccessDeniedError:
                raise
            except (HttpSessionError, HttpStatusError) as exc:
                if isinstance(exc, HttpStatusError) and not self._retryable(exc.status):
                    raise
                last_error = exc
                if attempt < self.retries:
                    self._backoff(attempt, None)
                    continue
                raise RuntimeError(f"request failed for {url}: {exc}") from exc
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SchemaError(f"expected JSON from {url}") from exc
            if not isinstance(payload, dict):
                raise SchemaError(f"expected a JSON object from {url}")
            return payload
        raise RuntimeError(f"request failed for {url}: {last_error}")

    def get_user_info(self, username: str) -> dict[str, Any]:
        return self.get_json(
            f"{self.base_url}/users/{username.lower()}/quick.json",
            referer=f"{self.base_url}/{username}",
        )

    def iter_projects(self, username: str, *, max_projects: int | None = None) -> Iterator[ProjectRef]:
        page = 1
        seen: set[str] = set()
        yielded = 0
        while True:
            query = urlencode({"album_id": "all", "page": page})
            url = f"{self.base_url}/users/{username}/projects.json?{query}"
            payload = self.get_json(url, referer=f"{self.base_url}/{username}")
            data = payload.get("data")
            if not isinstance(data, list):
                raise SchemaError(f"project list from {url} lacks a data array")
            if not data:
                return
            total = payload.get("total_count")
            if isinstance(total, int):
                self.project_total = total
            for item in data:
                if not isinstance(item, dict):
                    continue
                hash_id = str(item.get("hash_id") or item.get("id") or "").strip()
                if not hash_id or hash_id in seen:
                    continue
                seen.add(hash_id)
                yielded += 1
                yield ProjectRef(
                    hash_id=hash_id,
                    position=yielded,
                    title=str(item.get("title") or ""),
                    url=str(item.get("permalink") or item.get("url") or f"{self.base_url}/artwork/{hash_id}"),
                    assets_count=_optional_int(item.get("assets_count")),
                    updated_at=_optional_str(item.get("updated_at")),
                )
                if max_projects is not None and yielded >= max_projects:
                    return
            if isinstance(total, int) and yielded >= total:
                return
            page += 1
            if page > 10_000:
                raise SchemaError("pagination safety limit exceeded")

    def fetch_project(self, project: ProjectRef) -> dict[str, Any]:
        return self.get_json(
            f"{self.base_url}/projects/{project.hash_id}.json",
            referer=project.url or f"{self.base_url}/artwork/{project.hash_id}",
        )

    def assets_from_payload(
        self, profile: str, project: ProjectRef, payload: dict[str, Any]
    ) -> list[ImageAsset]:
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise SchemaError(f"project {project.hash_id} lacks an assets array")
        project_title = str(payload.get("title") or project.title or project.hash_id)
        project_url = str(payload.get("permalink") or payload.get("url") or project.url)
        no_ai = _has_noai_tag(payload)
        created_at = _optional_str(payload.get("created_at"))
        result: list[ImageAsset] = []
        for position, item in enumerate(assets, start=1):
            if not isinstance(item, dict) or not item.get("has_image"):
                continue
            source_url = str(item.get("image_url") or "").strip()
            if not source_url:
                continue
            asset_id = str(item.get("id") or item.get("asset_id") or position)
            result.append(
                ImageAsset(
                    profile=profile,
                    project_hash_id=project.hash_id,
                    project_position=project.position,
                    project_title=project_title,
                    project_url=project_url,
                    asset_id=asset_id,
                    position=position,
                    source_url=source_url,
                    original_name=str(
                        item.get("filename")
                        or item.get("name")
                        or filename_from_url(source_url, fallback=f"asset-{asset_id}")
                    ),
                    created_at=created_at,
                    no_ai=no_ai,
                )
            )
        return result

    def iter_project_assets(self, profile: str, project: ProjectRef) -> Iterator[ImageAsset]:
        yield from self.assets_from_payload(profile, project, self.fetch_project(project))

    def project_snapshot(
        self, project: ProjectRef, payload: dict[str, Any], assets: list[ImageAsset]
    ) -> dict[str, Any]:
        """A compact project record for the on-disk cache.

        Keeping the extracted asset list (instead of the full JSON) makes an
        unchanged project cost zero requests on the next run, at a few hundred
        bytes per asset.
        """

        return {
            "title": str(payload.get("title") or project.title or project.hash_id),
            "url": str(payload.get("permalink") or payload.get("url") or project.url),
            "created_at": _optional_str(payload.get("created_at")),
            "no_ai": _has_noai_tag(payload),
            "assets": [
                {
                    "id": asset.asset_id,
                    "position": asset.position,
                    "image_url": asset.source_url,
                    "name": asset.original_name,
                }
                for asset in assets
            ],
        }

    def assets_from_snapshot(
        self, profile: str, project: ProjectRef, snapshot: dict[str, Any]
    ) -> list[ImageAsset]:
        title = str(snapshot.get("title") or project.title or project.hash_id)
        url = str(snapshot.get("url") or project.url)
        created_at = _optional_str(snapshot.get("created_at"))
        no_ai = bool(snapshot.get("no_ai"))
        result: list[ImageAsset] = []
        for index, item in enumerate(snapshot.get("assets") or [], start=1):
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("image_url") or "")
            if not source_url:
                continue
            asset_id = str(item.get("id") or index)
            result.append(
                ImageAsset(
                    profile=profile,
                    project_hash_id=project.hash_id,
                    project_position=project.position,
                    project_title=title,
                    project_url=url,
                    asset_id=asset_id,
                    position=int(item.get("position") or index),
                    source_url=source_url,
                    original_name=str(item.get("name") or filename_from_url(source_url)),
                    created_at=created_at,
                    no_ai=no_ai,
                )
            )
        return result

    # -- image download --------------------------------------------------
    def stream_to_file(
        self,
        url: str,
        part_path: Path,
        *,
        referer: str | None = None,
        max_bytes: int = 250 * 1024 * 1024,
    ) -> tuple[str, int, str]:
        """Stream an image to a ``.part`` file, resuming when Range is honored."""

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._stream_once(url, part_path, referer=referer, max_bytes=max_bytes)
            except AccessDeniedError:
                raise
            except (HttpSessionError, HttpStatusError, OSError) as exc:
                last_error = exc
                if isinstance(exc, OSError) and exc.errno in _PERMANENT_OS_ERRORS:
                    raise RuntimeError(f"cannot write {part_path}: {exc}") from exc
                if isinstance(exc, HttpStatusError) and not self._retryable(exc.status):
                    raise
                if attempt < self.retries:
                    self._backoff(attempt, None)
                    continue
                raise RuntimeError(f"download failed for {url}: {exc}") from exc
        raise RuntimeError(f"download failed for {url}: {last_error}")

    def _stream_once(
        self,
        url: str,
        part_path: Path,
        *,
        referer: str | None,
        max_bytes: int,
    ) -> tuple[str, int, str]:
        self.limiter.acquire()
        existing_size = part_path.stat().st_size if part_path.exists() else 0
        headers = {"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
        if existing_size:
            headers["Range"] = f"bytes={existing_size}-"
        response = self.session.open(url, headers=headers, referer=referer or f"{self.base_url}/")
        with response:
            status = response.status
            self._log(f"[http] {status} {url}")
            if status in {401, 403}:
                self._reject_challenge(response.read(4096), status, url)
            if self._retryable(status):
                # The caller decides how long to wait before trying again.
                raise HttpStatusError(status, url)
            if status >= 400:
                raise HttpStatusError(status, url)

            content_type = response.getheader("Content-Type") or ""
            resumed = existing_size > 0 and status == 206
            if not resumed:
                existing_size = 0
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    if existing_size + int(content_length) > max_bytes:
                        raise RuntimeError(f"file exceeds limit of {max_bytes} bytes")
                except ValueError:
                    pass

            first_chunk = response.read(DOWNLOAD_CHUNK)
            if not first_chunk:
                raise HttpSessionError("empty image response")
            if resumed:
                with part_path.open("rb") as existing:
                    first_bytes = existing.read(16)
                if len(first_bytes) < 16:
                    first_bytes += first_chunk[: 16 - len(first_bytes)]
                digest = hashlib.sha256()
                with part_path.open("rb") as existing:
                    while block := existing.read(1024 * 1024):
                        digest.update(block)
            else:
                first_bytes = first_chunk[:16]
                digest = hashlib.sha256()

            if not sniff_image(first_bytes):
                part_path.unlink(missing_ok=True)
                raise RuntimeError("response is not a recognized image")

            total = existing_size
            with part_path.open("ab" if resumed else "wb") as output:
                output.write(first_chunk)
                digest.update(first_chunk)
                total += len(first_chunk)
                while block := response.read(DOWNLOAD_CHUNK):
                    output.write(block)
                    digest.update(block)
                    total += len(block)
                    if total > max_bytes:
                        raise RuntimeError(f"file exceeds limit of {max_bytes} bytes")
            return content_type, total, digest.hexdigest()

    # -- compatibility aliases -------------------------------------------
    def get_bytes(self, url: str, *, referer: str | None = None) -> tuple[bytes, str]:
        response = self._open(
            url,
            accept="image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            referer=referer,
        )
        with response:
            if response.status in {401, 403}:
                self._reject_challenge(response.read(4096), response.status, url)
            if not 200 <= response.status < 300:
                raise HttpStatusError(response.status, url)
            return response.read_all(), response.getheader("Content-Type") or ""


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _has_noai_tag(payload: dict[str, Any]) -> bool:
    if payload.get("no_ai") is True or payload.get("noai") is True:
        return True
    tags = payload.get("tags") or payload.get("tag_list") or []
    if isinstance(tags, str):
        tags = tags.replace(",", " ").split()
    if not isinstance(tags, list):
        return False
    for tag in tags:
        if isinstance(tag, dict):
            tag = tag.get("name") or tag.get("title") or tag.get("slug") or ""
        normalized = str(tag).replace("-", "").replace("_", "").replace(" ", "").lower()
        if normalized == "noai":
            return True
    return False
