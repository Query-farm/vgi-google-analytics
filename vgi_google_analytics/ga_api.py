"""Read-only HTTP access to the Google Analytics Data and Admin APIs.

This module is the **only** place in the package that touches the network, and
the only one that imports ``httpx``; ``tests/test_readonly_guard.py`` holds it
to both. Every request goes through :func:`_call`, which enforces an allow-list
rather than trusting call sites:

* ``GET`` — the Admin API's list/get endpoints and the Data API's metadata.
* ``POST`` — only to the three Data API RPCs that *read*: ``:runReport``,
  ``:runRealtimeReport`` and ``:checkCompatibility``. Google models those as
  POST because their request bodies are large, not because they change
  anything.

Anything else — creating an audience export, patching a property, archiving a
custom dimension — is refused before a socket is opened. Combined with the
read-only OAuth scope requested in :mod:`vgi_google_analytics.auth`, a write is
impossible twice over.

Both APIs speak REST+JSON here rather than through Google's generated gRPC
clients: one small dependency instead of grpcio and protobuf, plain dicts that
map straight onto Arrow, and an ``httpx.MockTransport`` seam that lets the
offline suite exercise the real request/response code.
"""

from __future__ import annotations

import atexit
import hmac
import random
import re
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
from google.auth.exceptions import TransportError

from vgi_google_analytics.auth import Credentials
from vgi_google_analytics.settings import MAX_PAGE_SIZE, Settings

#: The Data API RPCs that are reads despite being POSTs. Nothing else may POST.
READ_RPCS = frozenset({"runReport", "runRealtimeReport", "checkCompatibility"})

__all__ = ["MAX_PAGE_SIZE", "GoogleAnalyticsError", "Session"]

#: Admin list endpoints cap ``pageSize`` at 200.
ADMIN_PAGE_SIZE = 200

#: Connect and read timeouts. A large report can take tens of seconds to
#: compute server-side, so the read timeout is generous.
TIMEOUT = httpx.Timeout(120.0, connect=10.0)

#: How long the metadata (the list of dimensions/metrics a property supports)
#: is reused in-process. It changes only when someone edits a custom definition.
METADATA_TTL_SECONDS = 3_600

#: Statuses worth retrying. 429 is either the 10-concurrent-requests limit,
#: which clears in seconds, or an exhausted token bucket, which does not — a
#: short bounded retry covers the first without stalling long on the second.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_RETRY_ATTEMPTS = 4
_RETRY_BASE_SECONDS = 1.0

#: A GA4 property or account id: digits only. Validating rather than escaping
#: means a value from user SQL can never reach a URL path as anything else.
_NUMERIC_ID = re.compile(r"^[0-9]{1,20}$")


class GoogleAnalyticsError(RuntimeError):
    """A Google Analytics API call failed; the message is safe to show a user.

    Attributes:
        status: HTTP status code (0 for transport failures).
        reason: Google's canonical error status, e.g. ``PERMISSION_DENIED``.
    """

    def __init__(self, message: str, *, status: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def property_id(value: str) -> str:
    """Normalise ``'123'`` or ``'properties/123'`` to ``'123'``.

    Raises:
        ValueError: The value is not a numeric GA4 property id. A Universal
            Analytics id (``UA-...``) or a measurement id (``G-...``) gets a
            pointed message, since both are common mistakes.
    """
    raw = (value or "").strip().removeprefix("properties/")
    if _NUMERIC_ID.match(raw):
        return raw
    if raw.upper().startswith("G-"):
        raise ValueError(
            f"{value!r} is a measurement id (a data stream's tag), not a property id; "
            "GA4 property ids are numeric — find yours in ga4.main.account_summaries"
        )
    if raw.upper().startswith("UA-"):
        raise ValueError(
            f"{value!r} is a Universal Analytics id; UA was shut down in 2024 and this worker "
            "reads GA4 properties, whose ids are numeric"
        )
    raise ValueError(
        f"{value!r} is not a GA4 property id (expected digits such as '123456789', "
        "optionally prefixed 'properties/'); list yours with ga4.main.account_summaries"
    )


def account_id(value: str) -> str:
    """Normalise ``'123'`` or ``'accounts/123'`` to ``'123'``.

    Raises:
        ValueError: The value is not a numeric account id.
    """
    raw = (value or "").strip().removeprefix("accounts/")
    if _NUMERIC_ID.match(raw):
        return raw
    raise ValueError(f"{value!r} is not a Google Analytics account id (expected digits such as '1234567')")


def trailing_id(resource_name: Any) -> str | None:
    """The last path segment of a resource name: ``'properties/1/dataStreams/2'`` → ``'2'``."""
    if not resource_name:
        return None
    return str(resource_name).rsplit("/", 1)[-1] or None


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    """Who is asking (``credentials``) and where to ask (``settings``), for one call."""

    credentials: Credentials
    settings: Settings = field(default_factory=Settings)


def open_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """A client carrying this module's timeouts; ``transport`` is the test seam."""
    return httpx.Client(timeout=TIMEOUT, transport=transport)


_shared: httpx.Client | None = None
_shared_lock = threading.Lock()
_transport_override: httpx.BaseTransport | None = None


def shared_client() -> httpx.Client:
    """The process-wide connection pool.

    A paged scan fetches one page per ``process()`` tick with no enclosing scope
    to hold a client open, so without a shared pool every page would pay a new
    TLS handshake.
    """
    global _shared  # noqa: PLW0603 - a module-level pool is the point
    if _shared is None or _shared.is_closed:
        with _shared_lock:
            if _shared is None or _shared.is_closed:
                _shared = open_client(_transport_override)
    return _shared


def use_transport(transport: httpx.BaseTransport | None) -> None:
    """Route every request through ``transport`` (tests); ``None`` restores the network."""
    global _transport_override  # noqa: PLW0603 - test seam
    _transport_override = transport
    reset_shared_client()


def reset_shared_client() -> None:
    """Close and forget the process pool, so the next call opens a fresh one."""
    global _shared  # noqa: PLW0603
    with _shared_lock:
        if _shared is not None:
            _shared.close()
        _shared = None


atexit.register(reset_shared_client)


@dataclass(slots=True)
class _AuthResponse:
    """The ``google.auth.transport.Response`` shape google-auth reads back."""

    status: int
    headers: Mapping[str, str]
    data: bytes


class AuthTransport:
    """``google.auth.transport.Request`` implemented on the shared httpx pool.

    google-auth mints and refreshes tokens by calling a transport object; its
    stock ones need ``requests``. This keeps token traffic on the same client,
    timeouts and test seam as the API traffic, and keeps ``httpx`` confined to
    this module.
    """

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **_kwargs: Any,
    ) -> _AuthResponse:
        """Perform one token-endpoint (or metadata-server) request."""
        try:
            response = shared_client().request(
                method,
                url,
                content=body,
                headers=dict(headers or {}),
                timeout=timeout if timeout is not None else TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"token request to {url} failed: {exc}") from exc  # type: ignore[no-untyped-call]
        return _AuthResponse(status=response.status_code, headers=response.headers, data=response.content)


_AUTH_TRANSPORT = AuthTransport()


class OAuthCallback(HTTPServer):
    """A short-lived loopback listener for a desktop OAuth authorization code.

    It accepts only the expected callback path, Host and random state. It never
    logs the URL, which contains the authorization code, or reflects input in
    the browser response. The caller owns it with a ``with`` statement.
    """

    allow_reuse_address = False

    def __init__(self, state: str) -> None:
        self.state = state
        self.code = ""
        self.denied = False
        super().__init__(("127.0.0.1", 0), _OAuthCallbackHandler)
        self.timeout = 0.25

    @property
    def redirect_uri(self) -> str:
        """The loopback URL registered with this authorization request."""
        return f"http://127.0.0.1:{self.server_port}/oauth/callback"

    def get_request(self) -> tuple[socket.socket, Any]:
        """Bound how long an incomplete HTTP request can hold the listener."""
        connection, address = super().get_request()
        connection.settimeout(1.0)
        return connection, address

    def wait(self, timeout: float) -> str:
        """Wait for one matching response, or report denial / timeout."""
        deadline = time.monotonic() + timeout
        while not (self.code or self.denied):
            if time.monotonic() >= deadline:
                raise TimeoutError("Google sign-in timed out; run ATTACH again to retry")
            self.handle_request()
        if self.denied:
            raise ValueError("Google sign-in was not approved; run ATTACH again to retry")
        return self.code


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Keep callback URLs and authorization codes out of logs."""

    def do_GET(self) -> None:
        """Accept a code only when the path, host and state match this flow."""
        server = cast(OAuthCallback, self.server)
        try:
            parts = urlsplit(self.path)
            values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            self._respond(400, "Invalid sign-in response.")
            return
        states = values.get("state", [])
        expected_host = f"127.0.0.1:{server.server_port}"
        valid = (
            parts.path == "/oauth/callback"
            and not parts.scheme
            and not parts.netloc
            and self.headers.get("Host") == expected_host
            and len(states) == 1
            and hmac.compare_digest(states[0].encode(), server.state.encode())
        )
        codes, errors = values.get("code", []), values.get("error", [])
        if valid and len(codes) == 1 and codes[0] and not errors:
            server.code = codes[0]
        elif valid and len(errors) == 1 and errors[0] and not codes:
            server.denied = True
        else:
            self._respond(400, "This sign-in response was not accepted. Return to the Google sign-in page.")
            return
        self._respond(200, "Google sign-in response received. You can return to your SQL session.")

    def _respond(self, status: int, message: str) -> None:
        body = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)


def _allowed(method: str, url: str, settings: Settings) -> bool:
    """The read-only allow-list: any GET on the two APIs, POST only to read RPCs."""
    bases = (settings.data_api_url + "/", settings.admin_api_url + "/")
    if not url.startswith(bases):
        return False
    if method == "GET":
        return True
    if method == "POST":
        rpc = url.rsplit(":", 1)[-1] if ":" in url.rsplit("/", 1)[-1] else ""
        return url.startswith(bases[0]) and rpc in READ_RPCS
    return False


def _error_from(response: httpx.Response, credentials: Credentials) -> GoogleAnalyticsError:
    """Turn a Google error payload into a message an operator can act on."""
    status = response.status_code
    reason = ""
    message = response.text[:500] or response.reason_phrase
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        message = str(error.get("message") or message)
        reason = str(error.get("status") or "")
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason") == "SERVICE_DISABLED":
                reason = "SERVICE_DISABLED"

    hint = ""
    if reason == "SERVICE_DISABLED" or "has not been used in project" in message:
        hint = (
            " Enable both the 'Google Analytics Data API' and the 'Google Analytics Admin API' "
            "in the Google Cloud project that owns this credential."
        )
    elif status == 403:
        email = getattr(credentials.google, "service_account_email", None)
        who = f"the service account {email}" if email else "this identity"
        hint = (
            f" Grant {who} at least the Viewer role on the property (GA4 Admin → Property "
            "access management) and check the property id with ga4.main.account_summaries."
        )
    elif status == 401:
        hint = f" The {credentials.source} was rejected; it may be expired or revoked."
    elif status == 429:
        hint = (
            " The property's Analytics Data API quota is exhausted (tokens per hour/day, or 10 "
            "concurrent requests). Check ga4.main.property_quota, narrow the date range or "
            "dimensions, or retry later."
        )
    return GoogleAnalyticsError(
        f"Google Analytics API error {status}{f' {reason}' if reason else ''}: {message}{hint}",
        status=status,
        reason=reason,
    )


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait before retry ``attempt``: ``Retry-After`` if sent, else jittered backoff."""
    if response is not None:
        after = response.headers.get("retry-after", "")
        if after.isdigit():
            return min(float(after), 30.0)
    return float(_RETRY_BASE_SECONDS * (2**attempt) * (0.5 + random.random() / 2))


def _call(
    method: str,
    url: str,
    session: Session,
    *,
    params: Mapping[str, Any] | None = None,
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The single chokepoint: allow-list, authenticate, retry, decode.

    Raises:
        PermissionError: The request is not on the read-only allow-list. This is
            a programming error, never a user one.
        GoogleAnalyticsError: The API refused the request or kept failing.
    """
    credentials = session.credentials
    if not _allowed(method, url, session.settings):
        raise PermissionError(f"refusing {method} {url}: not a read-only Google Analytics request")
    client = shared_client()
    last: httpx.Response | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        headers = credentials.headers(_AUTH_TRANSPORT)
        try:
            response = client.request(method, url, params=params, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            if attempt + 1 < _RETRY_ATTEMPTS:
                time.sleep(_retry_delay(None, attempt))
                continue
            raise GoogleAnalyticsError(f"Google Analytics API timed out: {method} {url}") from exc
        except httpx.HTTPError as exc:
            raise GoogleAnalyticsError(f"could not reach the Google Analytics API: {exc}") from exc
        if response.status_code in _RETRYABLE and attempt + 1 < _RETRY_ATTEMPTS:
            last = response
            time.sleep(_retry_delay(response, attempt))
            continue
        if response.status_code >= 400:
            raise _error_from(response, credentials)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GoogleAnalyticsError(
                f"Google Analytics returned a non-JSON {response.status_code} for {method} {url}: "
                f"{response.text[:200]!r}",
                status=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise GoogleAnalyticsError(f"unexpected Google Analytics response shape for {url}")
        return payload
    assert last is not None  # the loop only falls through after a retryable response
    raise _error_from(last, credentials)


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------


def admin_page(
    path: str,
    key: str,
    session: Session,
    *,
    page_token: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of an Admin API list endpoint.

    Args:
        path: Path below the Admin API base, e.g. ``/accountSummaries``.
        key: The response key holding the items, e.g. ``accountSummaries``.
        session: Who to ask as, and where.
        page_token: Continuation token from the previous page, if any.
        params: Extra query parameters.

    Returns:
        The page's items and the next page token (``None`` on the last page).
    """
    query: dict[str, Any] = {"pageSize": ADMIN_PAGE_SIZE, **(params or {})}
    if page_token:
        query["pageToken"] = page_token
    payload = _call("GET", f"{session.settings.admin_api_url}{path}", session, params=query)
    items = payload.get(key) or []
    return [item for item in items if isinstance(item, dict)], payload.get("nextPageToken") or None


# ---------------------------------------------------------------------------
# Data API
# ---------------------------------------------------------------------------

_metadata_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
_metadata_lock = threading.Lock()


def metadata(prop: str, session: Session) -> dict[str, Any]:
    """Every dimension and metric ``prop`` supports, custom definitions included.

    Property ``'0'`` returns the universal set shared by every property.
    Cached per property and credential for :data:`METADATA_TTL_SECONDS`; report
    binding reads it to type columns, so a cold cache would otherwise mean an
    extra round trip on every query.
    """
    key = (session.settings.data_api_url, property_id(prop), id(session.credentials))
    now = time.monotonic()
    with _metadata_lock:
        hit = _metadata_cache.get(key)
        if hit is not None and now - hit[0] < METADATA_TTL_SECONDS:
            return hit[1]
    payload = _call("GET", f"{session.settings.data_api_url}/properties/{key[1]}/metadata", session)
    with _metadata_lock:
        _metadata_cache[key] = (now, payload)
    return payload


def clear_metadata_cache() -> None:
    """Drop every cached metadata response (tests)."""
    with _metadata_lock:
        _metadata_cache.clear()


def run_report(prop: str, body: Mapping[str, Any], session: Session) -> dict[str, Any]:
    """``properties/{prop}:runReport``."""
    return _call(
        "POST",
        f"{session.settings.data_api_url}/properties/{property_id(prop)}:runReport",
        session,
        body=body,
    )


def run_realtime_report(prop: str, body: Mapping[str, Any], session: Session) -> dict[str, Any]:
    """``properties/{prop}:runRealtimeReport``."""
    return _call(
        "POST",
        f"{session.settings.data_api_url}/properties/{property_id(prop)}:runRealtimeReport",
        session,
        body=body,
    )


def check_compatibility(prop: str, body: Mapping[str, Any], session: Session) -> dict[str, Any]:
    """``properties/{prop}:checkCompatibility``."""
    return _call(
        "POST",
        f"{session.settings.data_api_url}/properties/{property_id(prop)}:checkCompatibility",
        session,
        body=body,
    )
