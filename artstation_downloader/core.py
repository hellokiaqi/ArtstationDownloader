from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Long paths are opt-in on Windows; stay below MAX_PATH so folders copied to
# a machine without that setting still work. POSIX limits are per component.
WINDOWS_PATH_BUDGET = 248
POSIX_PATH_BUDGET = 4000
COMPONENT_LIMIT = 180


class ArtStationError(RuntimeError):
    """Base exception for expected downloader failures."""


class AccessDeniedError(ArtStationError):
    """The site rejected the request or presented an anti-bot challenge."""


class SchemaError(ArtStationError):
    """A response did not look like the expected ArtStation payload."""


@dataclass(frozen=True)
class Profile:
    username: str
    canonical_url: str


@dataclass(frozen=True)
class ProjectRef:
    hash_id: str
    position: int
    title: str = ""
    url: str = ""
    assets_count: int | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class ImageAsset:
    profile: str
    project_hash_id: str
    project_position: int
    project_title: str
    project_url: str
    asset_id: str
    position: int
    source_url: str
    original_name: str
    created_at: str | None = None
    no_ai: bool = False

    @property
    def key(self) -> str:
        raw = f"{self.profile}\0{self.project_hash_id}\0{self.asset_id}\0{self.position}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_profile_url(value: str) -> Profile:
    """Accept a username, a community profile URL, or a custom subdomain URL."""

    raw = value.strip()
    if not raw:
        raise ValueError("profile URL or username is empty")

    candidate = raw if "://" in raw else f"https://www.artstation.com/{raw}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host == "artstation.com" or host == "www.artstation.com":
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError("a profile username is required")
        if parts[0].lower() in {"artwork", "projects", "search", "api", "users"}:
            raise ValueError("expected an ArtStation profile URL, not a project/search/API URL")
        username = parts[0]
    elif host.endswith(".artstation.com"):
        username = host[: -len(".artstation.com")]
    else:
        raise ValueError("URL host must be artstation.com or a *.artstation.com profile host")

    if not re.fullmatch(r"[\w-]+", username, flags=re.UNICODE):
        raise ValueError(f"unsupported ArtStation username: {username!r}")
    return Profile(username=username, canonical_url=f"https://www.artstation.com/{quote(username)}")


def safe_component(value: str, fallback: str = "untitled", max_length: int = 120) -> str:
    """Make a title safe for Windows, macOS, and Linux file paths."""

    text = html.unescape(str(value or "")).strip()
    text = INVALID_FILENAME_CHARS.sub("_", text)
    text = WHITESPACE.sub(" ", text).strip(" .")
    if not text:
        text = fallback
    if text.upper() in RESERVED_NAMES:
        text = f"_{text}"
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    return text or fallback


def shorten_with_hash(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters, keeping it unique via a hash."""

    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    if limit <= len(digest) + 1:
        return digest[:limit]
    return f"{text[: limit - len(digest) - 1].rstrip(' .')}-{digest}"


def filename_from_url(url: str, fallback: str = "asset") -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name or fallback
    return safe_component(name, fallback=fallback)


def image_quality_candidates(url: str, quality: str = "best") -> list[str]:
    """Return CDN variants only for URLs with the known '/large/' layout."""

    if quality not in {"best", "8k", "4k", "large", "medium", "small"}:
        raise ValueError(f"unsupported quality: {quality}")

    parsed = urlparse(url)
    path = parsed.path
    marker = "/large/"
    if marker not in path:
        return [url]

    order = ["8k", "4k", "large", "medium", "small"]
    if quality != "best":
        order = order[order.index(quality) :]

    candidates: list[str] = []
    for variant in order:
        variant_url = urlunparse(parsed._replace(path=path.replace(marker, f"/{variant}/", 1)))
        if variant_url not in candidates:
            candidates.append(variant_url)
    if url not in candidates:
        candidates.append(url)
    return candidates


def parse_netscape_cookies(path: Path) -> list[tuple[str, str, str, str]]:
    """Read a Netscape cookie export without persisting or printing values."""

    cookies: list[tuple[str, str, str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\r\n")
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 7:
                continue
            domain, _flag, cookie_path, _secure, _expires, name, value = fields
            if domain.startswith("#HttpOnly_"):
                domain = domain[len("#HttpOnly_") :]
            elif domain.startswith("#"):
                continue
            normalized_domain = domain.lower().lstrip(".")
            if normalized_domain != "artstation.com" and not normalized_domain.endswith(".artstation.com"):
                continue
            cookies.append((domain, cookie_path or "/", name, value))
    return cookies


def extension_from_content_type(content_type: str | None, fallback_url: str) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    by_mime = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
    }
    if mime in by_mime:
        return by_mime[mime]
    suffix = Path(urlparse(fallback_url).path).suffix.lower()
    return suffix if suffix in IMAGE_EXTENSIONS else ".bin"


def sniff_image(data: bytes) -> bool:
    """Recognize common image signatures, avoiding HTML-as-JPG mistakes."""

    return (
        data.startswith(
            (
                b"\xff\xd8\xff",
                b"\x89PNG\r\n\x1a\n",
                b"GIF87a",
                b"GIF89a",
                b"BM",
                b"II*\x00",
                b"MM\x00*",
            )
        )
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read dimensions when Pillow is available; keep it optional for portability."""

    try:
        from PIL import Image
    except ImportError:
        return None, None
    try:
        with Image.open(path) as image:
            return image.width, image.height
    except (OSError, ValueError):
        return None, None


def reanchor_path(path: Path, output_root: Path) -> Path | None:
    """Find a recorded absolute path under a (possibly moved) output root.

    A download folder or a state file can come from another machine. The
    longest trailing part of the old path that exists under the current root
    is the best evidence that it is the same file.
    """

    parts = path.parts[1:]
    for start in range(len(parts) - 1):
        candidate = output_root.joinpath(*parts[start:])
        if candidate.exists():
            return candidate
    return None


def resolve_download_path(value: Any, output_root: Path) -> Path | None:
    """Map a manifest/state path to a local path, healing moved folders."""

    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        return output_root / path
    if path.exists():
        return path
    return reanchor_path(path, output_root) or path


def path_budget() -> int:
    return WINDOWS_PATH_BUDGET if os.name == "nt" else POSIX_PATH_BUDGET


def build_destination(
    output_root: Path,
    profile: str,
    project_position: int,
    project_title: str,
    asset_position: int,
    file_stem: str,
    suffix: str,
    *,
    max_length: int | None = None,
) -> Path:
    """Compose an output path that fits the platform limit.

    Numeric prefixes (``0004_`` project order, ``003_`` asset order) are never
    truncated: they are what keeps the folder readable and the files unique.
    Overlong titles lose characters first, and a short content hash keeps the
    shortened name distinct.
    """

    limit = max_length or path_budget()
    project_prefix = f"{project_position:04d}"
    asset_prefix = f"{asset_position:03d}"
    profile_component = safe_component(profile)
    base = output_root / profile_component
    # Reserve room for the two separators between the three path components.
    budget = limit - len(str(base)) - 2

    title = safe_component(project_title, fallback="untitled", max_length=COMPONENT_LIMIT)
    stem = safe_component(file_stem, fallback=f"asset-{asset_position}", max_length=COMPONENT_LIMIT)
    suffix = suffix if suffix.startswith(".") or not suffix else f".{suffix}"

    def compose(title_allow: int, stem_allow: int) -> tuple[str, str]:
        project = (
            f"{project_prefix}_{shorten_with_hash(title, title_allow)}" if title_allow > 0 else project_prefix
        )
        return project, f"{asset_prefix}_{shorten_with_hash(stem, stem_allow)}{suffix}"

    title_allow = len(title)
    stem_allow = len(stem)
    project_component, filename = compose(title_allow, stem_allow)
    overflow = len(project_component) + len(filename) - budget

    # Keep the project folder readable first, then trim the title, then fall
    # back to a short stem. Numeric prefixes always survive.
    if overflow > 0:
        stem_allow = max(24, stem_allow - overflow)
        project_component, filename = compose(title_allow, stem_allow)
        overflow = len(project_component) + len(filename) - budget
    if overflow > 0:
        title_allow = max(0, title_allow - overflow)
        project_component, filename = compose(title_allow, stem_allow)
        overflow = len(project_component) + len(filename) - budget
    if overflow > 0:
        title_allow = 0
        stem_allow = max(1, stem_allow - overflow)
        project_component, filename = compose(title_allow, stem_allow)

    return base / project_component / filename


def write_manifest(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            count += 1
    return count


def now() -> float:
    return time.time()
