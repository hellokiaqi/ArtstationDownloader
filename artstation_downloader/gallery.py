"""Generate a browsable, self-contained HTML index for a download folder.

The gallery is a single file with inline CSS/JS and no network references, so
it works offline, after moving the folder, and inside a zip archive. Cards link
straight to the original image files; a text filter narrows large libraries
without any build step.
"""

from __future__ import annotations

import html
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import quote

from .core import resolve_download_path
from .ui import format_bytes

__all__ = ["GalleryStats", "write_gallery"]


@dataclass
class GalleryStats:
    projects: int = 0
    images: int = 0
    bytes: int = 0
    missing: int = 0
    path: Path | None = None


@dataclass
class _Card:
    href: str
    name: str
    project: str
    size: int
    width: int | None
    height: int | None
    search: str


_TEMPLATE = Template(
    """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>$title</title>
<style>
:root {
  --bg: #0e1014;
  --panel: #161a21;
  --card: #1a1f27;
  --border: #262d38;
  --text: #e7ebf2;
  --muted: #8b95a5;
  --accent: #4c9aff;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f6f7f9;
    --panel: #ffffff;
    --card: #ffffff;
    --border: #e3e6ea;
    --text: #1c2027;
    --muted: #6b7483;
    --accent: #2b6cd4;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font: 14px/1.5 "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}
header {
  position: sticky;
  top: 0;
  z-index: 10;
  padding: 14px clamp(14px, 3vw, 40px) 12px;
  background: var(--bg);
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid var(--border);
}
.bar { display: flex; align-items: center; justify-content: space-between; gap: 18px; flex-wrap: wrap; }
h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: .01em; }
h1 a { color: inherit; text-decoration: none; }
h1 a:hover { color: var(--accent); }
.meta { margin: 3px 0 0; color: var(--muted); font-size: 12px; }
.tools { display: flex; align-items: center; gap: 10px; }
#q {
  width: min(320px, 52vw);
  padding: 7px 11px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  font: inherit;
  outline: none;
}
#q:focus { border-color: var(--accent); }
.count { color: var(--muted); font-size: 12px; min-width: 56px; text-align: right; font-variant-numeric: tabular-nums; }
main { padding: 20px clamp(14px, 3vw, 40px) 70px; }
.wall { columns: 240px; column-gap: 14px; }
.card {
  display: block;
  break-inside: avoid;
  margin: 0 0 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  background: var(--card);
  text-decoration: none;
  color: inherit;
  transition: transform .12s ease, border-color .12s ease;
}
.card:hover { transform: translateY(-2px); border-color: var(--accent); }
.card img { display: block; width: 100%; height: auto; background: #08090b; }
.info { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; padding: 8px 10px; }
.info b {
  font-weight: 500;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.info small { color: var(--muted); font-size: 11px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.empty { color: var(--muted); padding: 40px 0; text-align: center; }
footer { color: var(--muted); font-size: 12px; padding: 0 clamp(14px, 3vw, 40px) 40px; }
[hidden] { display: none !important; }
@media (prefers-reduced-motion: reduce) { .card { transition: none; } }
</style>
</head>
<body>
<header>
  <div class="bar">
    <div>
      <h1><a href="$profile_url">$artist</a></h1>
      <p class="meta">$meta</p>
    </div>
    <div class="tools">
      <input id="q" type="search" placeholder="筛选项目或文件名（按 / 聚焦）" autocomplete="off">
      <span id="count" class="count"></span>
    </div>
  </div>
</header>
<main>
<div class="wall">
$cards
</div>
</main>
<footer>$footer</footer>
<script>
(function () {
  var input = document.getElementById('q');
  var count = document.getElementById('count');
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var total = cards.length;

  function apply() {
    var query = input.value.trim().toLowerCase();
    var shown = 0;
    cards.forEach(function (card) {
      var hit = !query || card.getAttribute('data-search').indexOf(query) >= 0;
      card.hidden = !hit;
      if (hit) { shown += 1; }
    });
    count.textContent = query ? shown + ' / ' + total : total + '';
  }

  input.addEventListener('input', apply);
  document.addEventListener('keydown', function (event) {
    if (event.key === '/' && document.activeElement !== input) {
      event.preventDefault();
      input.focus();
    } else if (event.key === 'Escape' && document.activeElement === input) {
      input.value = '';
      apply();
      input.blur();
    }
  });
  apply();
})();
</script>
</body>
</html>
"""
)

_EMPTY_PROJECTS = '<p class="empty">还没有下载任何图片。运行 <code>artstation-dl</code> 后重新生成即可。</p>'


def write_gallery(
    destination: Path,
    *,
    profile: str,
    artist: str,
    rows: Iterable[dict[str, Any]],
    output_root: Path,
    version: str = "",
    generated_at: str | None = None,
) -> GalleryStats:
    """Render ``rows`` (state records) into ``destination`` and return stats."""

    destination = Path(destination)
    output_root = Path(output_root)
    cards: list[tuple[int, int, _Card]] = []
    stats = GalleryStats()

    for row in rows:
        if row.get("status") != "completed":
            continue
        link = _relative_link(row.get("path"), output_root, destination.parent)
        if link is None:
            stats.missing += 1
            continue
        file_path = _absolute(row.get("path"), output_root)
        try:
            size = file_path.stat().st_size
        except OSError:
            stats.missing += 1
            continue
        title = str(row.get("project_title") or "Untitled")
        name = file_path.name
        card = _Card(
            href=link,
            name=name,
            project=title,
            size=size,
            width=_optional_int(row.get("width")),
            height=_optional_int(row.get("height")),
            search=f"{title} {name}".lower(),
        )
        cards.append((int(row.get("project_position") or 0), int(row.get("asset_position") or 0), card))
        stats.images += 1
        stats.bytes += size

    cards.sort(key=lambda item: (item[0], item[1], item[2].name.lower()))
    stats.projects = len({position for position, _, _ in cards})

    body = "\n".join(_render_card(card) for _, _, card in cards) or _EMPTY_PROJECTS
    meta = [
        f"@{html.escape(profile)}",
        f"{stats.projects} 个项目",
        f"{stats.images} 张图片",
        format_bytes(stats.bytes),
    ]
    if stats.missing:
        meta.append(f"{stats.missing} 个文件缺失")
    if generated_at:
        meta.append(f"生成于 {generated_at}")

    footer = "由 artstation-dl 生成" + (f" · v{html.escape(version)}" if version else "")
    document = _TEMPLATE.substitute(
        title=f"{html.escape(artist)} · ArtStation",
        profile_url=f"https://www.artstation.com/{quote(profile)}",
        artist=html.escape(artist),
        meta=html.escape(" · ".join(meta)),
        cards=body,
        footer=html.escape(footer),
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(document, encoding="utf-8", newline="\n")
    os.replace(temporary, destination)
    stats.path = destination
    return stats


def _render_card(card: _Card) -> str:
    dimensions = f"{card.width}×{card.height}" if card.width and card.height else ""
    detail = " · ".join(part for part in (format_bytes(card.size), dimensions) if part)
    label = html.escape(f"{card.project} — {card.name}")
    size_attrs = (
        f' width="{card.width}" height="{card.height}"' if card.width and card.height else ""
    )
    return (
        f'  <a class="card" href="{html.escape(card.href)}" target="_blank" rel="noopener" '
        f'data-search="{html.escape(card.search, quote=True)}">\n'
        f'    <img src="{html.escape(card.href)}" alt="{label}"{size_attrs} loading="lazy" decoding="async">\n'
        f'    <span class="info"><b title="{label}">{html.escape(card.project)}</b>'
        f"<small>{html.escape(detail)}</small></span>\n"
        f"  </a>"
    )


def _absolute(value: Any, output_root: Path) -> Path:
    """Resolve a state path, re-anchoring records from a moved library."""

    resolved = resolve_download_path(value, output_root)
    return resolved if resolved is not None else output_root


def _relative_link(value: Any, output_root: Path, base: Path) -> str | None:
    """Return a URL usable from the gallery file, or None when it is missing."""

    if not value:
        return None
    path = _absolute(value, output_root)
    if not path.is_file():
        return None
    try:
        relative = os.path.relpath(path, base)
    except (OSError, ValueError):
        return None
    return quote(Path(relative).as_posix(), safe="/@:+~")


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
