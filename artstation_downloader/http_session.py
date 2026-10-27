"""Small keep-alive HTTP layer used by the downloader.

``urllib`` opens a fresh TCP/TLS connection for every request, so a portfolio
with hundreds of images pays for a handshake (often 100-400 ms over a long
link) on every single file. This module keeps one connection per
``(scheme, host, port, proxy)`` per thread alive and reuses it, with the few
HTTP features the downloader needs:

* GET with redirects (including cross-host CDN redirects),
* transparent gzip decoding,
* cookie injection/extraction through a standard :class:`CookieJar`,
* environment proxy support (``HTTP_PROXY``/``HTTPS_PROXY``/``NO_PROXY``),
* one silent reconnect when a pooled socket was closed by the server.

Only this narrow surface is implemented on purpose: no HTTP/2, no connection
upgrade, no request bodies. Anything unexpected propagates as an error that the
retry layer can classify.
"""

from __future__ import annotations

import base64
import gzip
import http.client
import ssl
import threading
import zlib
from collections.abc import Mapping
from contextlib import suppress
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, getproxies, proxy_bypass

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
DEFAULT_PORTS = {"http": 80, "https": 443}
# A pooled socket the server closed while idle fails on reuse; retrying once
# on a fresh connection is safe for idempotent GETs and keeps long runs alive.
STALE_CONNECTION_ERRORS = (
    ConnectionResetError,
    BrokenPipeError,
    http.client.BadStatusLine,
    http.client.RemoteDisconnected,
)
DRAIN_LIMIT = 256 * 1024


class HttpSessionError(RuntimeError):
    """Raised when a request cannot be performed at the transport level."""


class HttpResponse:
    """A response body that can be streamed and then released to the pool."""

    def __init__(
        self,
        session: HttpSession,
        key: tuple[str, str, int, str | None],
        response: http.client.HTTPResponse,
        raw: Any,
        url: str,
    ) -> None:
        self._session = session
        self._key = key
        self._response = response
        self._raw = raw
        self._closed = False
        self.status = response.status
        self.reason = response.reason
        self.headers = response.headers
        self.url = url

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, amount: int = -1) -> bytes:
        if self._closed:
            return b""
        try:
            return self._raw.read(amount)
        except (OSError, EOFError, http.client.HTTPException, zlib.error) as exc:
            self._session._discard(self._key)
            raise HttpSessionError(f"connection dropped while reading {self.url}: {exc}") from exc

    def read_all(self, limit: int | None = None) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = self.read(64 * 1024)
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if limit is not None and total >= limit:
                break
        return b"".join(chunks)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Decide reuse before closing: http.client clears its file pointer when
        # a response is closed, which would make every body look complete.
        fully_read = self._response.isclosed()
        if not fully_read:
            # A decoder can finish before the socket does (gzip trailer, or a
            # body the caller did not read). Draining a bounded remainder keeps
            # the connection reusable instead of paying for a new handshake.
            with suppress(OSError, EOFError, http.client.HTTPException, zlib.error):
                self._response.read(DRAIN_LIMIT)
            fully_read = self._response.isclosed()
        reusable = fully_read and not self._response.will_close
        with suppress(Exception):
            self._raw.close()
        with suppress(Exception):
            self._response.close()
        self._session._release(self._key, reusable)

    def __enter__(self) -> HttpResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class HttpSession:
    """Thread-aware keep-alive HTTP client with cookie and proxy support."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 30.0,
        cookie_jar: CookieJar | None = None,
        proxy_url: str | None = None,
        trust_env_proxy: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = max(1.0, timeout)
        self.cookie_jar = cookie_jar if cookie_jar is not None else CookieJar()
        self._proxy_url = proxy_url
        self._trust_env_proxy = trust_env_proxy
        self._local = threading.local()
        self._ssl_context = ssl.create_default_context()
        self._connections: set[http.client.HTTPConnection] = set()
        self._connections_lock = threading.Lock()

    # -- connection pool -------------------------------------------------
    def _pool(self) -> dict[tuple[str, str, int, str | None], http.client.HTTPConnection]:
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = {}
            self._local.pool = pool
        return pool

    def _register(self, connection: http.client.HTTPConnection) -> None:
        with self._connections_lock:
            self._connections.add(connection)

    def _discard(self, key: tuple[str, str, int, str | None]) -> None:
        connection = self._pool().pop(key, None)
        if connection is None:
            return
        with self._connections_lock:
            self._connections.discard(connection)
        with suppress(Exception):
            connection.close()

    def _release(self, key: tuple[str, str, int, str | None], reusable: bool) -> None:
        if not reusable:
            self._discard(key)

    def _connection(
        self,
        scheme: str,
        host: str,
        port: int,
        proxy: tuple[str, int, str] | None,
    ) -> tuple[http.client.HTTPConnection, tuple[str, str, int, str | None]]:
        proxy_key = f"{proxy[0]}:{proxy[1]}" if proxy else None
        key = (scheme, host, port, proxy_key)
        pool = self._pool()
        connection = pool.get(key)
        if connection is not None:
            return connection, key
        if proxy is not None:
            proxy_host, proxy_port, _ = proxy
            if scheme == "https":
                connection = http.client.HTTPSConnection(
                    proxy_host, proxy_port, timeout=self.timeout, context=self._ssl_context
                )
                connection.set_tunnel(host, port)
            else:
                connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=self.timeout)
        elif scheme == "https":
            connection = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self._ssl_context)
        else:
            connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
        pool[key] = connection
        self._register(connection)
        return connection, key

    # -- proxies ---------------------------------------------------------
    def _proxy_for(self, scheme: str, host: str) -> tuple[str, int, str] | None:
        if self._proxy_url:
            raw = self._proxy_url
        elif self._trust_env_proxy:
            if proxy_bypass(host):
                return None
            proxies = getproxies()
            raw = proxies.get(scheme) or proxies.get("all") or ""
        else:
            return None
        if not raw:
            return None
        candidate = raw if "://" in raw else f"http://{raw}"
        parsed = urlsplit(candidate)
        if parsed.scheme.startswith("socks"):
            raise HttpSessionError(
                "SOCKS proxies are not supported; point HTTPS_PROXY at an HTTP proxy instead"
            )
        if not parsed.hostname:
            return None
        auth = ""
        if parsed.username:
            credentials = f"{parsed.username}:{parsed.password or ''}"
            auth = "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        return parsed.hostname, parsed.port or 8080, auth

    # -- cookies ---------------------------------------------------------
    def _cookie_header(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": self.user_agent})
        with suppress(Exception):
            self.cookie_jar.add_cookie_header(request)
        return request.get_header("Cookie") or ""

    def _store_cookies(self, response: http.client.HTTPResponse, url: str) -> None:
        class _Adapter:
            def info(self) -> Any:
                return response.headers

            def geturl(self) -> str:
                return url

        with suppress(Exception):
            request = Request(url, headers={"User-Agent": self.user_agent})
            self.cookie_jar.extract_cookies(_Adapter(), request)  # type: ignore[arg-type]

    # -- requests --------------------------------------------------------
    def open(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        referer: str | None = None,
    ) -> HttpResponse:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            response = self._single_request(current, headers, referer)
            if response.status in REDIRECT_STATUSES:
                location = response.getheader("Location")
                self._drain(response)
                if not location:
                    raise HttpSessionError(f"redirect without a Location header from {current}")
                current = urljoin(current, location)
                continue
            return response
        raise HttpSessionError(f"too many redirects while requesting {url}")

    def _drain(self, response: HttpResponse) -> None:
        """Consume a small body so the connection can be reused."""

        try:
            response.read(DRAIN_LIMIT)
        except HttpSessionError:
            return
        finally:
            response.close()

    def _single_request(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        referer: str | None,
    ) -> HttpResponse:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in DEFAULT_PORTS:
            raise HttpSessionError(f"unsupported URL scheme in {url!r}")
        host = parts.hostname
        if not host:
            raise HttpSessionError(f"URL without a host: {url!r}")
        port = parts.port or DEFAULT_PORTS[scheme]
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"

        proxy = self._proxy_for(scheme, host)
        request_headers: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.8",
            "Connection": "keep-alive",
        }
        if referer:
            request_headers["Referer"] = referer
        if headers:
            request_headers.update(headers)
        cookie = self._cookie_header(url)
        if cookie:
            request_headers["Cookie"] = cookie
        if proxy and proxy[2]:
            request_headers["Proxy-Authorization"] = proxy[2]

        path = target
        if proxy is not None and scheme == "http":
            # Plain HTTP proxying uses the absolute-URI request form.
            authority = host if port == DEFAULT_PORTS[scheme] else f"{host}:{port}"
            path = f"http://{authority}{target}"

        last_error: BaseException | None = None
        for attempt in (0, 1):
            connection, key = self._connection(scheme, host, port, proxy)
            try:
                connection.request("GET", path, headers=request_headers)
                raw_response = connection.getresponse()
                self._store_cookies(raw_response, url)
                return self._wrap(key, raw_response, url)
            except STALE_CONNECTION_ERRORS as exc:
                last_error = exc
                self._discard(key)
                if attempt == 0:
                    continue
                break
            except (OSError, http.client.HTTPException) as exc:
                self._discard(key)
                raise HttpSessionError(f"request to {url} failed: {exc}") from exc
        raise HttpSessionError(f"request to {url} failed after reconnect: {last_error}")

    def _wrap(
        self,
        key: tuple[str, str, int, str | None],
        response: http.client.HTTPResponse,
        url: str,
    ) -> HttpResponse:
        raw: Any = response
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip":
            raw = gzip.GzipFile(fileobj=response)
        elif encoding in {"br", "zstd"}:
            # Never negotiated by this client; treat it as an error rather than
            # handing compressed bytes to the caller.
            response.close()
            self._discard(key)
            raise HttpSessionError(f"unsupported content encoding {encoding!r} from {url}")
        return HttpResponse(self, key, response, raw, url)

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            with suppress(Exception):
                connection.close()
        self._local = threading.local()


__all__ = ["HttpResponse", "HttpSession", "HttpSessionError"]
