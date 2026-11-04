"""Per-attachment settings, chosen with ATTACH options.

Everything that changes how one attached catalog behaves is an ATTACH option,
not an environment variable — visible in the statement that made the choice,
and scoped to that one catalog::

    ATTACH 'ga4' (TYPE vgi, LOCATION 'uv run ga4_worker.py',
                  credentials 'secret', report_page_size 50000);

The options are validated in ``catalog_attach`` and travel to every function as
the catalog's attach bytes (``params.attach_opaque_data``), which is the only
route an ATTACH-time choice has into a function body.

Credential options are security-relevant, because on a shared HTTP worker ATTACH
options are chosen by the *client*:

* ``credentials => 'auto'`` may fall back to Application Default Credentials —
  but only when the operator allows it (always for a stdio worker, which runs
  as the user who attached it; only with ``--allow-adc`` for the HTTP server).
* Overriding ``data_api_url`` / ``admin_api_url`` disables that fallback
  outright, so no client can point the worker at its own host and receive the
  worker's own token.
* ``credentials => 'oauth'`` starts a local, in-memory Google login, explicitly
  enabled only by the stdio entry point and only with Google's API endpoints.
  The attach bytes carry the refreshable OAuth grant and must be treated as
  credentials by the client. No worker process owns the lifetime of the grant.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import pyarrow as pa
from vgi.catalog.attach_option import AttachOptionSpec

from .oauth_model import AUTO, CREDENTIAL_MODES, OAUTH, SECRET_ONLY, OAuthGrant, credential_mode

__all__ = ["AUTO", "CREDENTIAL_MODES", "OAUTH", "SECRET_ONLY", "OAuthGrant", "Settings"]

DEFAULT_DATA_API_URL = "https://analyticsdata.googleapis.com/v1beta"
DEFAULT_ADMIN_API_URL = "https://analyticsadmin.googleapis.com/v1beta"

#: ``runReport`` returns at most 250,000 rows per request. The default page is
#: smaller so the first rows reach DuckDB sooner and a ``LIMIT`` stops early;
#: a report's quota cost depends on its range and cardinality, not on paging.
DEFAULT_REPORT_PAGE_SIZE = 100_000
MAX_PAGE_SIZE = 250_000

OPTION_SPECS = [
    AttachOptionSpec(
        name="credentials",
        desc=(
            "Where credentials come from: 'auto' (default) uses the google_analytics secret and "
            "otherwise Application Default Credentials where the worker permits it; 'secret' "
            "requires the secret and never falls back; 'oauth' signs in locally through the browser "
            "to Query Farm's app and carries refreshable credentials in the client's opaque attach data."
        ),
        type=pa.string(),
        default=AUTO,
    ),
    AttachOptionSpec(
        name="report_page_size",
        desc="Rows fetched per report() API request, up to 250000 (default 100000).",
        type=pa.int64(),
        default=DEFAULT_REPORT_PAGE_SIZE,
    ),
    AttachOptionSpec(
        name="data_api_url",
        desc=(
            "Base URL of the GA4 Data API, for a proxy or a test double. Overriding it disables "
            "the Application Default Credentials fallback."
        ),
        type=pa.string(),
        default=DEFAULT_DATA_API_URL,
    ),
    AttachOptionSpec(
        name="admin_api_url",
        desc=(
            "Base URL of the GA4 Admin API, for a proxy or a test double. Overriding it disables "
            "the Application Default Credentials fallback."
        ),
        type=pa.string(),
        default=DEFAULT_ADMIN_API_URL,
    ),
]


@dataclass(frozen=True, slots=True)
class Settings:
    """The validated ATTACH options of one catalog."""

    credentials: str = AUTO
    report_page_size: int = DEFAULT_REPORT_PAGE_SIZE
    data_api_url: str = DEFAULT_DATA_API_URL
    admin_api_url: str = DEFAULT_ADMIN_API_URL
    # Internal credentials, never an ATTACH option or part of a diagnostic repr.
    oauth_grant: OAuthGrant | None = field(default=None, repr=False)

    @property
    def custom_endpoints(self) -> bool:
        """Whether either API URL was overridden."""
        return self.data_api_url != DEFAULT_DATA_API_URL or self.admin_api_url != DEFAULT_ADMIN_API_URL

    @property
    def may_use_adc(self) -> bool:
        """Whether these settings permit the ADC fallback (the operator must also allow it)."""
        return self.credentials == AUTO and not self.custom_endpoints

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> Settings:
        """Validate raw ATTACH options.

        Raises:
            ValueError: An unknown credentials mode, an out-of-range page size, or
                an API URL that is not a plain http(s) base URL. Rejected at
                ATTACH rather than ignored, because every way of getting these
                wrong is otherwise silent.
        """
        mode = credential_mode(options)
        raw_size = options.get("report_page_size")
        size = DEFAULT_REPORT_PAGE_SIZE if raw_size is None else int(raw_size)
        if not 1 <= size <= MAX_PAGE_SIZE:
            raise ValueError(
                f"ATTACH option report_page_size => {size} must be between 1 and {MAX_PAGE_SIZE}"
            )
        data_api_url = _base_url("data_api_url", options.get("data_api_url") or DEFAULT_DATA_API_URL)
        admin_api_url = _base_url("admin_api_url", options.get("admin_api_url") or DEFAULT_ADMIN_API_URL)
        if mode == OAUTH and (data_api_url, admin_api_url) != (DEFAULT_DATA_API_URL, DEFAULT_ADMIN_API_URL):
            raise ValueError("OAuth login requires the standard Google Analytics API endpoints")
        return cls(
            credentials=mode,
            report_page_size=size,
            data_api_url=data_api_url,
            admin_api_url=admin_api_url,
        )

    def encode(self) -> bytes:
        """These settings as attach bytes; OAuth bytes contain sensitive credentials."""
        return json.dumps(asdict(self), sort_keys=True).encode()

    @classmethod
    def decode(cls, data: bytes | None) -> Settings:
        """Settings from attach bytes; defaults when there are none.

        No bytes means no ATTACH (a direct call, a test). Bytes that do not
        parse mean an older client; both get the defaults, which are the
        conservative choice — the real Google endpoints. An OAuth attachment
        that does not decode is an error, never a silent fallback to ADC.
        """
        if not data:
            return cls()
        try:
            raw = json.loads(data.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        try:
            decoded = cls.from_options(raw)
        except (TypeError, ValueError):
            if str(raw.get("credentials")).strip().lower() == OAUTH:
                raise ValueError("Invalid OAuth attachment; detach and attach again") from None
            return cls()
        if decoded.credentials == OAUTH:
            decoded = replace(decoded, oauth_grant=OAuthGrant.decode(raw.get("oauth_grant")))
        return decoded


def _base_url(name: str, value: Any) -> str:
    url = str(value).strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise ValueError(f"ATTACH option {name} => {value!r} must be an http(s) base URL without a query")
    return url
