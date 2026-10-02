"""A small, deterministic stand-in for the GA4 Admin and Data APIs.

It speaks just enough of both REST APIs for the worker's real request and
response code to run unchanged against it — in-process through
``httpx.MockTransport`` (:func:`handle`) or out-of-process over a real socket
(:class:`FakeServer`), which is how the end-to-end tests drive a worker
subprocess attached from DuckDB.

The data is synthetic but internally consistent: a fact table of
(date, country, channel, eventName, device) rows with integer counts, which
``runReport`` groups by the requested dimensions and sums. Relative dates are
resolved against a fixed :data:`TODAY`, so results never depend on the clock.

Every request is recorded in :data:`REQUESTS`, so tests can assert what the
worker actually asked Google for — the pushed-down filter, the page offsets.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import product
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

TOKEN = "test-token"
TODAY = date(2026, 9, 29)

ACCOUNT = "5001"
PROPERTIES = {"1001": "Main Website", "1002": "Mobile App"}

#: Every request seen, as (method, path, query, body).
REQUESTS: list[tuple[str, str, dict[str, list[str]], dict[str, Any] | None]] = []
_lock = threading.Lock()

DIMENSIONS = [
    ("date", "Date", "The date of the event, formatted as YYYYMMDD.", "Time"),
    ("dateHour", "Date + hour (YYYYMMDDHH)", "The combined values of date and hour.", "Time"),
    ("country", "Country", "The country from which the user activity originated.", "Geography"),
    ("city", "City", "The city from which the user activity originated.", "Geography"),
    (
        "sessionDefaultChannelGroup",
        "Session default channel group",
        "The session's default channel group.",
        "Traffic source",
    ),
    ("eventName", "Event name", "The name of the event.", "Event"),
    ("deviceCategory", "Device category", "Desktop, tablet or mobile.", "Platform / device"),
    ("landingPage", "Landing page", "The page path of the first pageview in a session.", "Page / screen"),
]
METRICS = [
    ("activeUsers", "Active users", "Distinct users who visited.", "TYPE_INTEGER", "User"),
    ("totalUsers", "Total users", "Distinct users who logged an event.", "TYPE_INTEGER", "User"),
    ("sessions", "Sessions", "Number of sessions.", "TYPE_INTEGER", "Session"),
    ("eventCount", "Event count", "Number of events.", "TYPE_INTEGER", "Event"),
    ("keyEvents", "Key events", "Number of key events.", "TYPE_INTEGER", "Event"),
    ("engagementRate", "Engagement rate", "Engaged sessions / sessions.", "TYPE_FLOAT", "Session"),
    ("totalRevenue", "Total revenue", "Revenue from purchases.", "TYPE_CURRENCY", "Revenue"),
]

FACT_DATES = [date(2026, 9, 1) + timedelta(days=i) for i in range(28)]
COUNTRIES = ["United States", "United Kingdom", "Germany"]
CHANNELS = ["Organic Search", "Direct"]
EVENTS = ["page_view", "purchase"]
DEVICES = ["desktop", "mobile"]


def _facts() -> list[dict[str, Any]]:
    rows = []
    for i, (day, country, channel, event, device) in enumerate(
        product(FACT_DATES, COUNTRIES, CHANNELS, EVENTS, DEVICES)
    ):
        users = 1 + (i * 7) % 13
        rows.append(
            {
                "date": day.strftime("%Y%m%d"),
                "dateHour": day.strftime("%Y%m%d") + f"{i % 24:02d}",
                "country": country,
                "city": {"United States": "New York", "United Kingdom": "London"}.get(country, "Berlin"),
                "sessionDefaultChannelGroup": channel,
                "eventName": event,
                "deviceCategory": device,
                "landingPage": "/" if i % 3 else "/pricing",
                "activeUsers": users,
                "totalUsers": users,
                "sessions": users + 1,
                "eventCount": users * 3,
                "keyEvents": users if event == "purchase" else 0,
                "engagementRate": 0.5,
                "totalRevenue": 9.99 * users if event == "purchase" else 0.0,
            }
        )
    return rows


FACTS = _facts()


def resolve_date(expr: str) -> date:
    """A GA4 date expression against :data:`TODAY`."""
    if expr == "today":
        return TODAY
    if expr == "yesterday":
        return TODAY - timedelta(days=1)
    if m := re.fullmatch(r"(\d+)daysAgo", expr):
        return TODAY - timedelta(days=int(m.group(1)))
    return date.fromisoformat(expr)


def _matches(row: dict[str, Any], expression: dict[str, Any] | None) -> bool:
    if not expression:
        return True
    if "andGroup" in expression:
        return all(_matches(row, e) for e in expression["andGroup"]["expressions"])
    if "orGroup" in expression:
        return any(_matches(row, e) for e in expression["orGroup"]["expressions"])
    if "notExpression" in expression:
        return not _matches(row, expression["notExpression"])
    flt = expression["filter"]
    value = str(row.get(flt["fieldName"], ""))
    if "stringFilter" in flt:
        sf = flt["stringFilter"]
        expected = sf["value"]
        if not sf.get("caseSensitive"):
            value, expected = value.lower(), expected.lower()
        match sf.get("matchType", "EXACT"):
            case "EXACT":
                return value == expected
            case "BEGINS_WITH":
                return value.startswith(expected)
            case "CONTAINS":
                return expected in value
        raise ValueError(sf)
    if "inListFilter" in flt:
        return value in flt["inListFilter"]["values"]
    raise ValueError(flt)


def _error(status: int, reason: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": status, "message": message, "status": reason}}


def run_report(prop: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Group the fact table by the requested dimensions and sum the metrics."""
    known_dims = {d[0] for d in DIMENSIONS} | ({"customEvent:plan_tier"} if prop == "1001" else set())
    known_metrics = {m[0]: m[3] for m in METRICS}
    dims = [d["name"] for d in body.get("dimensions", [])]
    metrics = [m["name"] for m in body.get("metrics", [])]
    for name in dims:
        if name not in known_dims:
            return _error(400, "INVALID_ARGUMENT", f"Field {name} is not a valid dimension.")
    for name in metrics:
        if name not in known_metrics:
            return _error(400, "INVALID_ARGUMENT", f"Field {name} is not a valid metric.")
    ranges = body.get("dateRanges") or [{"startDate": "28daysAgo", "endDate": "yesterday"}]
    multi = len(ranges) > 1
    groups: dict[tuple[str, ...], dict[str, float]] = {}
    for index, rng in enumerate(ranges):
        start, end = resolve_date(rng["startDate"]), resolve_date(rng["endDate"])
        label = rng.get("name") or f"date_range_{index}"
        for fact in FACTS:
            day = date(int(fact["date"][:4]), int(fact["date"][4:6]), int(fact["date"][6:]))
            if not start <= day <= end or not _matches(fact, body.get("dimensionFilter")):
                continue
            key = tuple(str(fact.get(d, "(not set)")) for d in dims) + ((label,) if multi else ())
            bucket = groups.setdefault(key, dict.fromkeys(metrics, 0.0))
            for m in metrics:
                bucket[m] += fact[m]
    rows = []
    for key in sorted(groups):
        values = groups[key]
        rows.append(
            {
                "dimensionValues": [{"value": v} for v in key],
                "metricValues": [
                    {"value": str(int(values[m])) if known_metrics[m] == "TYPE_INTEGER" else str(values[m])}
                    for m in metrics
                ],
            }
        )
    total = len(rows)
    offset = int(body.get("offset", 0))
    limit = int(body.get("limit", 10_000))
    page = rows[offset : offset + limit]
    response: dict[str, Any] = {
        "dimensionHeaders": [{"name": d} for d in dims] + ([{"name": "dateRange"}] if multi else []),
        "metricHeaders": [{"name": m, "type": known_metrics[m]} for m in metrics],
        "metadata": {"currencyCode": "USD", "timeZone": "America/New_York"},
        "kind": "analyticsData#runReport",
    }
    if page:
        response["rows"] = page
    if total:
        response["rowCount"] = total
    if "country" in dims:
        response["metadata"]["subjectToThresholding"] = True
    if body.get("returnPropertyQuota"):
        response["propertyQuota"] = {
            "tokensPerDay": {"consumed": 1, "remaining": 199_990},
            "tokensPerHour": {"consumed": 1, "remaining": 39_990},
            "concurrentRequests": {"consumed": 0, "remaining": 10},
        }
    return 200, response


def metadata(prop: str) -> dict[str, Any]:
    """``getMetadata`` for ``prop`` ('0' = universal; 1001 also has customs)."""
    dims = [
        {"apiName": a, "uiName": u, "description": d, "category": c, "customDefinition": False}
        for a, u, d, c in DIMENSIONS
    ]
    mets = [
        {"apiName": a, "uiName": u, "description": d, "type": t, "category": c} for a, u, d, t, c in METRICS
    ]
    mets[-2]["expression"] = "engagedSessions / sessions"
    if prop == "1001":
        dims.append(
            {
                "apiName": "customEvent:plan_tier",
                "uiName": "Plan tier",
                "description": "An event scoped custom dimension.",
                "category": "Custom",
                "customDefinition": True,
            }
        )
    return {"name": f"properties/{prop}/metadata", "dimensions": dims, "metrics": mets}


ADMIN_LISTS: dict[str, tuple[str, list[dict[str, Any]]]] = {
    "dataStreams": (
        "dataStreams",
        [
            {
                "name": "properties/{p}/dataStreams/77",
                "type": "WEB_DATA_STREAM",
                "displayName": "Website",
                "webStreamData": {"measurementId": "G-TEST{p}", "defaultUri": "https://example.com"},
                "createTime": "2024-01-02T03:04:05.123Z",
            }
        ],
    ),
    "customDimensions": (
        "customDimensions",
        [
            {
                "name": "properties/{p}/customDimensions/1",
                "parameterName": "plan_tier",
                "displayName": "Plan tier",
                "scope": "EVENT",
            },
            {
                "name": "properties/{p}/customDimensions/2",
                "parameterName": "signup_cohort",
                "displayName": "Signup cohort",
                "scope": "USER",
            },
        ],
    ),
    "customMetrics": (
        "customMetrics",
        [
            {
                "name": "properties/{p}/customMetrics/1",
                "parameterName": "credits",
                "displayName": "Credits",
                "measurementUnit": "STANDARD",
                "scope": "EVENT",
            }
        ],
    ),
    "keyEvents": (
        "keyEvents",
        [
            {
                "name": "properties/{p}/keyEvents/1",
                "eventName": "purchase",
                "custom": False,
                "deletable": False,
                "countingMethod": "ONCE_PER_EVENT",
                "createTime": "2024-01-02T03:04:05Z",
            }
        ],
    ),
}


def _paged(
    items: list[dict[str, Any]], key: str, query: dict[str, list[str]], size: int = 1
) -> dict[str, Any]:
    """Page ``items`` ``size`` at a time, so tests exercise the page-token loop."""
    start = int(query.get("pageToken", ["0"])[0])
    out: dict[str, Any] = {key: items[start : start + size]}
    if start + size < len(items):
        out["nextPageToken"] = str(start + size)
    return out


def route(
    method: str, path: str, query: dict[str, list[str]], body: dict[str, Any] | None
) -> tuple[int, Any]:
    """Dispatch one request; ``path`` starts at ``/admin`` or ``/data``."""
    with _lock:
        REQUESTS.append((method, path, query, body))
    if path.startswith("/admin/v1beta"):
        sub = path.removeprefix("/admin/v1beta")
        if method != "GET":
            return _error(405, "METHOD_NOT_ALLOWED", "admin is GET-only here")
        if sub == "/accountSummaries":
            summaries = [
                {
                    "name": f"accountSummaries/{ACCOUNT}",
                    "account": f"accounts/{ACCOUNT}",
                    "displayName": "Example Co",
                    "propertySummaries": [
                        {
                            "property": f"properties/{p}",
                            "displayName": n,
                            "propertyType": "PROPERTY_TYPE_ORDINARY",
                            "parent": f"accounts/{ACCOUNT}",
                        }
                        for p, n in PROPERTIES.items()
                    ],
                },
                {"name": "accountSummaries/5002", "account": "accounts/5002", "displayName": "Empty Co"},
            ]
            return 200, _paged(summaries, "accountSummaries", query)
        if sub == "/accounts":
            accounts = [
                {
                    "name": f"accounts/{ACCOUNT}",
                    "displayName": "Example Co",
                    "regionCode": "US",
                    "createTime": "2020-05-06T07:08:09Z",
                },
                {"name": "accounts/5002", "displayName": "Empty Co", "regionCode": "GB"},
            ]
            return 200, _paged(accounts, "accounts", query)
        if sub == "/properties":
            filt = query.get("filter", [""])[0]
            if not filt.startswith("ancestor:accounts/"):
                return _error(400, "INVALID_ARGUMENT", "filter required")
            account = filt.rsplit("/", 1)[-1]
            props = (
                [
                    {
                        "name": f"properties/{p}",
                        "account": f"accounts/{account}",
                        "parent": f"accounts/{account}",
                        "displayName": n,
                        "propertyType": "PROPERTY_TYPE_ORDINARY",
                        "timeZone": "America/New_York",
                        "currencyCode": "USD",
                        "serviceLevel": "GOOGLE_ANALYTICS_STANDARD",
                        "createTime": "2021-01-01T00:00:00Z",
                    }
                    for p, n in PROPERTIES.items()
                ]
                if account == ACCOUNT
                else []
            )
            return 200, _paged(props, "properties", query)
        if m := re.fullmatch(r"/properties/(\d+)/(\w+)", sub):
            prop, kind = m.groups()
            if prop not in PROPERTIES:
                return _error(
                    403, "PERMISSION_DENIED", "User does not have sufficient permissions for this property."
                )
            key, items = ADMIN_LISTS[kind]
            rendered = json.loads(json.dumps(items).replace("{p}", prop))
            return 200, _paged(rendered, key, query)
        return _error(404, "NOT_FOUND", sub)
    if path.startswith("/data/v1beta"):
        sub = path.removeprefix("/data/v1beta")
        if m := re.fullmatch(r"/properties/(\d+)/metadata", sub):
            return 200, metadata(m.group(1))
        if m := re.fullmatch(r"/properties/(\d+):(\w+)", sub):
            prop, rpc = m.groups()
            if prop not in PROPERTIES:
                return _error(
                    403, "PERMISSION_DENIED", "User does not have sufficient permissions for this property."
                )
            if rpc == "runReport":
                return run_report(prop, body or {})
            if rpc == "runRealtimeReport":
                dims = [d["name"] for d in (body or {}).get("dimensions", [])]
                mets = [x["name"] for x in (body or {}).get("metrics", [])]
                rows = [
                    {
                        "dimensionValues": [
                            {"value": f"{i:02d}" if d == "minutesAgo" else f"{d}-{i}"} for d in dims
                        ],
                        "metricValues": [{"value": str(10 + i)} for _ in mets],
                    }
                    for i in range(3)
                ]
                return 200, {
                    "dimensionHeaders": [{"name": d} for d in dims],
                    "metricHeaders": [{"name": x, "type": "TYPE_INTEGER"} for x in mets],
                    "rows": rows,
                    "rowCount": 3,
                }
            if rpc == "checkCompatibility":
                body = body or {}
                wants_item = any(d["name"] == "itemName" for d in body.get("dimensions", []))
                return 200, {
                    "dimensionCompatibilities": [
                        {"dimensionMetadata": {"apiName": a, "uiName": u}, "compatibility": "COMPATIBLE"}
                        for a, u, _, _ in DIMENSIONS
                    ],
                    "metricCompatibilities": [
                        {
                            "metricMetadata": {"apiName": a, "uiName": u},
                            "compatibility": "INCOMPATIBLE"
                            if wants_item and a == "sessions"
                            else "COMPATIBLE",
                        }
                        for a, u, _, _, _ in METRICS
                    ],
                }
        return _error(404, "NOT_FOUND", sub)
    return _error(404, "NOT_FOUND", path)


def handle(request: httpx.Request) -> httpx.Response:
    """``httpx.MockTransport`` handler; the worker's base URLs must point at ``http://fake/...``."""
    if request.headers.get("authorization") != f"Bearer {TOKEN}":
        status, payload = _error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        return httpx.Response(status, json=payload)
    body = json.loads(request.content) if request.content else None
    status, payload = route(request.method, request.url.path, parse_qs(request.url.query.decode()), body)
    return httpx.Response(status, json=payload)


class _Handler(BaseHTTPRequestHandler):
    def _serve(self, method: str) -> None:
        if self.headers.get("authorization") != f"Bearer {TOKEN}":
            status, payload = _error(
                401, "UNAUTHENTICATED", "Request had invalid authentication credentials."
            )
        else:
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            parts = urlsplit(self.path)
            status, payload = route(method, parts.path, parse_qs(parts.query), body)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def log_message(self, *_args: Any) -> None:
        pass


@contextmanager
def serve() -> Iterator[str]:
    """Run the fake on a free localhost port; yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
