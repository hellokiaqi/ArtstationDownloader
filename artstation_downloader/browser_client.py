from __future__ import annotations

import hashlib
import html
import mimetypes
import os
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any

from .client import ArtStationClient
from .core import AccessDeniedError, ArtStationError, SchemaError, sniff_image


class ArtStationBrowserClient(ArtStationClient):
    """Use a normal headed Chrome session when direct HTTP is challenged.

    The browser is used for the same public JSON and image URLs as the HTTP
    client. It does not solve CAPTCHAs, alter browser fingerprints, or inspect
    another browser's profile/cookies.
    """

    def __init__(
        self,
        *,
        profile_url: str,
        browser_path: Path | None = None,
        headless: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._download_page = None
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - exercised on fresh installs
            raise ArtStationError(
                "browser fallback needs Playwright; install project dependencies with "
                "python -m pip install -e ."
            ) from exc

        self._playwright_timeout = PlaywrightTimeoutError
        self._playwright_error = PlaywrightError
        self._playwright = sync_playwright().start()
        executable = browser_path or _find_browser()
        launch_options: dict[str, Any] = {"headless": headless}
        if executable:
            launch_options["executable_path"] = str(executable)
        try:
            self._browser = self._playwright.chromium.launch(**launch_options)
        except Exception as exc:
            self.close()
            if executable is None:
                raise ArtStationError(
                    "no usable Chrome/Edge browser was found for the fallback; "
                    "install Chrome or set ARTSTATION_BROWSER_PATH"
                ) from exc
            raise ArtStationError(f"could not start browser at {executable}: {exc}") from exc

        try:
            cookies = [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path or "/",
                    "secure": bool(cookie.secure),
                }
                for cookie in self.cookie_jar
            ]
            self._context = self._browser.new_context(
                accept_downloads=True,
                locale="en-US",
                user_agent=self.user_agent,
            )
            if cookies:
                self._context.add_cookies(cookies)
            self._page = self._context.new_page()
            self._download_page = self._context.new_page()
            response = None
            try:
                response = self._page.goto(
                    profile_url,
                    wait_until="domcontentloaded",
                    timeout=int(self.timeout * 1000),
                )
            except self._playwright_timeout:
                # ArtStation can keep a profile navigation open because of
                # long-lived page resources even after the usable DOM exists.
                # Continue only when the browser really reached ArtStation and
                # did not show a challenge page.
                self._page.wait_for_timeout(800)
                self._check_page(None)
                if "artstation.com" not in (self._page.url or ""):
                    raise
            self._page.wait_for_timeout(1200)
            self._check_page(response.status if response else None)
        except self._playwright_timeout as exc:
            self.close()
            raise ArtStationError(
                "the browser opened ArtStation but did not finish loading within the timeout"
            ) from exc
        except Exception:
            self.close()
            raise

    def get_json(self, url: str, *, referer: str | None = None) -> dict[str, Any]:
        del referer  # the browser supplies the profile page as the real referer
        for attempt in range(self.retries + 1):
            self.limiter.acquire()
            try:
                result = self._page.evaluate(
                    """
                    async ({url}) => {
                        const response = await window.fetch(url, {
                            credentials: "include",
                            headers: {"Accept": "application/json, text/plain, */*"}
                        });
                        return {
                            status: response.status,
                            contentType: response.headers.get("content-type") || "",
                            text: await response.text()
                        };
                    }
                    """,
                    {"url": url},
                )
                status = int(result.get("status", 0))
                body = str(result.get("text", ""))
                if status in {401, 403}:
                    raise AccessDeniedError(
                        f"ArtStation also challenged the normal browser request ({status}) for {url}"
                    )
                if (status == 429 or 500 <= status <= 599) and attempt < self.retries:
                    self._backoff(attempt, None)
                    continue
                if status < 200 or status >= 300:
                    raise RuntimeError(f"HTTP {status} while requesting {url}")
                self._check_page(status)
                try:
                    import json

                    payload = json.loads(body)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise SchemaError(f"expected JSON from {url}") from exc
                if not isinstance(payload, dict):
                    raise SchemaError(f"expected a JSON object from {url}")
                if self.verbose:
                    print(f"[browser] {status} {url}")
                return payload
            except AccessDeniedError:
                raise
            except (self._playwright_timeout, self._playwright_error) as exc:
                if attempt < self.retries:
                    self._backoff(attempt, None)
                    continue
                raise RuntimeError(f"browser request timed out for {url}") from exc
        raise AssertionError("unreachable")

    def stream_to_file(
        self,
        url: str,
        part_path: Path,
        *,
        referer: str | None = None,
        max_bytes: int = 250 * 1024 * 1024,
    ) -> tuple[str, int, str]:
        del referer
        download_url = _with_download_flag(url)
        for attempt in range(self.retries + 1):
            self.limiter.acquire()
            try:
                anchor = html.escape(download_url, quote=True)
                self._download_page.set_content(
                    f'<a id="artstation-download" href="{anchor}">download</a>'
                )
                with self._download_page.expect_download(timeout=int(self.timeout * 1000)) as info:
                    self._download_page.locator("#artstation-download").click()
                download = info.value
                failure = download.failure()
                if failure:
                    raise RuntimeError(f"browser download failed: {failure}")
                temporary = download.path()
                if not temporary:
                    raise RuntimeError("browser did not provide a downloaded file")
                temporary_path = Path(temporary)
                byte_count = temporary_path.stat().st_size
                if byte_count > max_bytes:
                    raise RuntimeError(f"file exceeds limit of {max_bytes} bytes")
                with temporary_path.open("rb") as handle:
                    first_bytes = handle.read(16)
                if not sniff_image(first_bytes):
                    raise RuntimeError("response is not a recognized image")
                digest = hashlib.sha256()
                with temporary_path.open("rb") as handle:
                    while block := handle.read(1024 * 1024):
                        digest.update(block)
                part_path.parent.mkdir(parents=True, exist_ok=True)
                part_path.unlink(missing_ok=True)
                download.save_as(str(part_path))
                content_type = mimetypes.guess_type(url.split("?", 1)[0])[0] or "application/octet-stream"
                if self.verbose:
                    print(f"[browser] download {url} -> {byte_count} bytes")
                return content_type, byte_count, digest.hexdigest()
            except AccessDeniedError:
                raise
            except (self._playwright_timeout, self._playwright_error) as exc:
                if attempt < self.retries:
                    self._backoff(attempt, None)
                    continue
                raise RuntimeError(f"browser image download timed out for {url}") from exc
            except (OSError, RuntimeError):
                if attempt < self.retries:
                    self._backoff(attempt, None)
                    continue
                raise
        raise AssertionError("unreachable")

    def _check_page(self, status: int | None) -> None:
        if status in {401, 403}:
            raise AccessDeniedError(f"ArtStation returned HTTP {status} in the browser")
        if not self._page:
            return
        try:
            title = (self._page.title() or "").lower()
            text = self._page.locator("body").inner_text(timeout=5000).lower()
        except (self._playwright_timeout, self._playwright_error):
            return
        markers = (
            "just a moment",
            "challenge-platform",
            "cf-chl-",
            "attention required! | cloudflare",
            "verify you are human",
            "captcha",
        )
        if any(marker in title or marker in text for marker in markers):
            raise AccessDeniedError(
                "ArtStation presented a browser access challenge. "
                "Manual verification may be required; this tool does not solve CAPTCHAs."
            )

    def close(self) -> None:
        for resource in (self._context, self._browser):
            if resource is not None:
                with suppress(Exception):
                    resource.close()
        if self._playwright is not None:
            with suppress(Exception):
                self._playwright.stop()
        self._context = None
        self._browser = None
        self._playwright = None


def _with_download_flag(url: str) -> str:
    if "dl=" in url.split("?", 1)[-1]:
        return url
    return f"{url}&dl=1" if "?" in url else f"{url}?dl=1"


def _find_browser() -> Path | None:
    configured = os.environ.get("ARTSTATION_BROWSER_PATH")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if not root:
            continue
        candidates.extend(
            [
                Path(root) / "Google/Chrome/Application/chrome.exe",
                Path(root) / "Microsoft/Edge/Application/msedge.exe",
            ]
        )
    candidates.extend(
        [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    )
    for command in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge"):
        located = shutil.which(command)
        if located:
            candidates.append(Path(located))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
