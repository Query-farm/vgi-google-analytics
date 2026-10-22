"""Arrow schemas for the fixed-shape functions, and total value coercion.

Report functions build their schema at bind time from the dimensions and
metrics requested (see :mod:`vgi_google_analytics.types`); everything else here
has one fixed shape, declared once with a comment per column. Those comments are
the single source of a column's documentation: DuckDB shows them in
``DESCRIBE`` and ``duckdb_columns()``, and :mod:`vgi_google_analytics.meta`
reads them back to declare each function's result schema for vgi-lint.

Every coercer is **total**: a value Arrow would reject becomes NULL instead of
raising, because one odd value in a 100,000-row page must not fail the page.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import pyarrow as pa

from vgi_google_analytics.meta import field

#: Admin API timestamps are RFC 3339 instants.
TIMESTAMP = pa.timestamp("us", tz="UTC")

#: Report timestamps (``dateHour``, ``dateHourMinute``) are wall-clock times in
#: the *property's* reporting time zone, with no offset — so they are naive.
LOCAL_TIMESTAMP = pa.timestamp("us")

#: The window nanosecond-resolution clients (pandas, numpy) can hold; anything
#: outside it raises at materialization, which is worse than NULL.
_NS_FLOOR = datetime(1678, 1, 1, tzinfo=UTC)
_NS_CEILING = datetime(2262, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Total coercers
# ---------------------------------------------------------------------------


def to_int(value: Any) -> int | None:
    """An int64, from an int or a numeric string; NULL otherwise.

    Google encodes int64 as a JSON *string* (``"1234"``) to survive JavaScript
    number precision, and report metric values are always strings. A float
    string that is integral (``"12.0"``) is accepted.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    else:
        try:
            result = int(str(value))
        except ValueError:
            try:
                as_float = float(str(value))
            except ValueError:
                return None
            if not as_float.is_integer():
                return None
            result = int(as_float)
    return result if -(2**63) <= result < 2**63 else None


def to_float(value: Any) -> float | None:
    """A double, from a number or numeric string; NULL for anything else or non-finite."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def to_bool(value: Any) -> bool | None:
    """A boolean, from a JSON bool or ``'true'``/``'false'``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def to_timestamp(value: Any) -> datetime | None:
    """An aware UTC datetime from an RFC 3339 string, or NULL."""
    if not value or not isinstance(value, str):
        return value if isinstance(value, datetime) else None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    return parsed if _NS_FLOOR <= parsed < _NS_CEILING else None


def to_report_date(value: Any) -> date | None:
    """A ``YYYYMMDD`` report dimension value as a DATE.

    Anything else — ``(other)`` when GA4 folds low-volume rows together, or
    ``(not set)`` — becomes NULL: the row still carries its metrics, and a
    sentinel string cannot live in a DATE column.
    """
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def to_report_timestamp(value: Any) -> datetime | None:
    """A ``YYYYMMDDHH`` or ``YYYYMMDDHHMM`` report dimension value as a naive TIMESTAMP."""
    if not isinstance(value, str) or len(value) not in {10, 12} or not value.isdigit():
        return None
    try:
        return datetime(
            int(value[:4]),
            int(value[4:6]),
            int(value[6:8]),
            int(value[8:10]),
            int(value[10:12]) if len(value) == 12 else 0,
        )
    except ValueError:
        return None


def _coerce(value: Any, kind: pa.DataType) -> Any:  # noqa: PLR0911 - one return per type family
    """Convert one payload value to what ``kind`` declares, or NULL."""
    if value is None:
        return None
    if pa.types.is_string(kind):
        return value if isinstance(value, str) else str(value)
    if pa.types.is_int64(kind):
        return to_int(value)
    if pa.types.is_float64(kind):
        return to_float(value)
    if pa.types.is_boolean(kind):
        return to_bool(value)
    if pa.types.is_date32(kind):
        return value if isinstance(value, date) else to_report_date(value)
    if pa.types.is_timestamp(kind):
        if isinstance(value, datetime):
            return value
        return to_timestamp(value) if kind.tz else to_report_timestamp(value)
    if pa.types.is_list(kind):
        if not isinstance(value, (list, tuple)):
            return None
        return [_coerce(item, kind.value_type) for item in value]
    return value


def column(rows: Sequence[dict[str, Any]], name: str, kind: pa.DataType) -> pa.Array:
    """One column of ``rows`` as an Arrow array of ``kind``."""
    return pa.array([_coerce(row.get(name), kind) for row in rows], type=kind)


def batch_from_rows(rows: Sequence[dict[str, Any]], schema: pa.Schema) -> pa.RecordBatch:
    """Build one RecordBatch by pulling each schema field out of ``rows`` by name."""
    return pa.RecordBatch.from_arrays([column(rows, f.name, f.type) for f in schema], schema=schema)


# ---------------------------------------------------------------------------
# Admin API shapes
# ---------------------------------------------------------------------------

_PROPERTY_ID = "Numeric GA4 property id — the first argument every report and per-property function takes."

ACCOUNT_SUMMARY_SCHEMA = pa.schema(
    [
        field("account_id", pa.string(), "Numeric Google Analytics account id."),
        field("account_name", pa.string(), "Display name of the account."),
        field(
            "property_id",
            pa.string(),
            _PROPERTY_ID + " NULL for an account the credential can see but that has no property "
            "it can read.",
        ),
        field("property_name", pa.string(), "Display name of the property."),
        field(
            "property_type",
            pa.string(),
            "PROPERTY_TYPE_ORDINARY, PROPERTY_TYPE_SUBPROPERTY or PROPERTY_TYPE_ROLLUP.",
        ),
        field(
            "parent",
            pa.string(),
            "Resource name of the property's parent: 'accounts/…' for an ordinary property, "
            "'properties/…' for a subproperty.",
        ),
    ]
)

ACCOUNT_SCHEMA = pa.schema(
    [
        field("account_id", pa.string(), "Numeric Google Analytics account id."),
        field("display_name", pa.string(), "Display name of the account."),
        field("region_code", pa.string(), "Country of the business, as a CLDR region code (e.g. 'US')."),
        field("create_time", TIMESTAMP, "When the account was created."),
        field("update_time", TIMESTAMP, "When the account's settings were last changed."),
        field(
            "gmp_organization",
            pa.string(),
            "Google Marketing Platform organization resource the account belongs to, if any.",
        ),
    ]
)

PROPERTY_SCHEMA = pa.schema(
    [
        field("property_id", pa.string(), _PROPERTY_ID),
        field("account_id", pa.string(), "Account the property belongs to."),
        field("display_name", pa.string(), "Display name of the property."),
        field(
            "property_type",
            pa.string(),
            "PROPERTY_TYPE_ORDINARY, PROPERTY_TYPE_SUBPROPERTY or PROPERTY_TYPE_ROLLUP.",
        ),
        field("parent", pa.string(), "Resource name of the property's parent account or property."),
        field(
            "time_zone",
            pa.string(),
            "Reporting time zone (IANA, e.g. 'America/New_York'). Report dates and hours are in this zone.",
        ),
        field(
            "currency_code",
            pa.string(),
            "ISO 4217 reporting currency; currency metrics are in this unless a report overrides it.",
        ),
        field("industry_category", pa.string(), "Industry the property was categorised under at setup."),
        field(
            "service_level",
            pa.string(),
            "GOOGLE_ANALYTICS_STANDARD or GOOGLE_ANALYTICS_360 — which sets the API quota.",
        ),
        field("create_time", TIMESTAMP, "When the property was created."),
        field("update_time", TIMESTAMP, "When the property's settings were last changed."),
    ]
)

DATA_STREAM_SCHEMA = pa.schema(
    [
        field("property_id", pa.string(), "Property the stream sends data to."),
        field("stream_id", pa.string(), "Numeric data stream id."),
        field(
            "type",
            pa.string(),
            "WEB_DATA_STREAM, ANDROID_APP_DATA_STREAM or IOS_APP_DATA_STREAM.",
        ),
        field("display_name", pa.string(), "Display name of the stream."),
        field(
            "measurement_id",
            pa.string(),
            "The 'G-XXXXXXX' tag id a website sends hits with (web streams only). Not a property id.",
        ),
        field("default_uri", pa.string(), "Domain of the website the stream measures (web streams only)."),
        field("firebase_app_id", pa.string(), "Firebase app id linked to the stream, if any."),
        field("package_name", pa.string(), "Android package name (Android streams only)."),
        field("bundle_id", pa.string(), "Apple bundle id (iOS streams only)."),
        field("create_time", TIMESTAMP, "When the stream was created."),
        field("update_time", TIMESTAMP, "When the stream was last changed."),
    ]
)

CUSTOM_DIMENSION_SCHEMA = pa.schema(
    [
        field("property_id", pa.string(), "Property the custom dimension is defined on."),
        field(
            "api_name",
            pa.string(),
            "The name to pass in report(dimensions => [...]): 'customEvent:<parameter>', "
            "'customUser:<parameter>' or 'customItem:<parameter>' depending on scope.",
        ),
        field("parameter_name", pa.string(), "Event, user-property or item parameter the dimension reads."),
        field("display_name", pa.string(), "Name shown in the GA4 interface."),
        field("description", pa.string(), "Free-text description entered by whoever created it."),
        field("scope", pa.string(), "EVENT, USER or ITEM."),
        field(
            "disallow_ads_personalization",
            pa.bool_(),
            "Whether the dimension is excluded from ads personalization (user scope only).",
        ),
    ]
)

CUSTOM_METRIC_SCHEMA = pa.schema(
    [
        field("property_id", pa.string(), "Property the custom metric is defined on."),
        field(
            "api_name",
            pa.string(),
            "The name to pass in report(metrics => [...]): 'customEvent:<parameter>'.",
        ),
        field("parameter_name", pa.string(), "Event parameter the metric sums."),
        field("display_name", pa.string(), "Name shown in the GA4 interface."),
        field("description", pa.string(), "Free-text description entered by whoever created it."),
        field(
            "measurement_unit",
            pa.string(),
            "STANDARD, CURRENCY, FEET, METERS, KILOMETERS, MILES, MILLISECONDS, SECONDS, MINUTES or HOURS.",
        ),
        field("scope", pa.string(), "Always EVENT for custom metrics."),
        field(
            "restricted_metric_types",
            pa.list_(pa.string()),
            "COST_DATA and/or REVENUE_DATA when the metric is hidden from users without that access.",
        ),
    ]
)

KEY_EVENT_SCHEMA = pa.schema(
    [
        field("property_id", pa.string(), "Property the key event is configured on."),
        field(
            "event_name",
            pa.string(),
            "Event name marked as a key event (formerly 'conversion'); filter reports on it with "
            "the eventName dimension.",
        ),
        field(
            "counting_method",
            pa.string(),
            "ONCE_PER_EVENT or ONCE_PER_SESSION — how keyEvents counts repeats.",
        ),
        field("custom", pa.bool_(), "False for key events Google created automatically (e.g. purchase)."),
        field("deletable", pa.bool_(), "Whether the key event can be removed."),
        field("default_value", pa.float64(), "Default monetary value assigned when the event carries none."),
        field("default_currency_code", pa.string(), "Currency of default_value."),
        field("create_time", TIMESTAMP, "When the event was marked as a key event."),
    ]
)

# ---------------------------------------------------------------------------
# Data API shapes
# ---------------------------------------------------------------------------

DIMENSION_METADATA_SCHEMA = pa.schema(
    [
        field("property_id", pa.string(), "Property the list was fetched for ('0' = the universal set)."),
        field(
            "api_name",
            pa.string(),
            "Name to pass in report(dimensions => [...]); also the result column's name.",
        ),
        field("ui_name", pa.string(), "Name shown in the GA4 interface."),
        field("description", pa.string(), "Google's description of the dimension."),
        field(
            "category", pa.string(), "Grouping in the GA4 interface, e.g. 'Geography' or 'Traffic source'."
        ),
        field("custom_definition", pa.bool_(), "True for a property's own custom dimension."),
        field(
            "column_type",
            pa.string(),
            "SQL type the column gets in report(): DATE for date/firstSessionDate, TIMESTAMP for "
            "dateHour/dateHourMinute, otherwise VARCHAR.",
        ),
        field(
            "deprecated_api_names",
            pa.list_(pa.string()),
            "Older names still accepted for this dimension.",
        ),
    ]
)

METRIC_METADATA_SCHEMA = pa.schema(
    [
        field("property_id", pa.string(), "Property the list was fetched for ('0' = the universal set)."),
        field(
            "api_name",
            pa.string(),
            "Name to pass in report(metrics => [...]); also the result column's name.",
        ),
        field("ui_name", pa.string(), "Name shown in the GA4 interface."),
        field("description", pa.string(), "Google's description of the metric."),
        field("category", pa.string(), "Grouping in the GA4 interface, e.g. 'User' or 'Ecommerce'."),
        field(
            "type",
            pa.string(),
            "Google's metric type: TYPE_INTEGER, TYPE_FLOAT, TYPE_SECONDS, TYPE_CURRENCY, ….",
        ),
        field(
            "column_type",
            pa.string(),
            "SQL type the column gets in report(): BIGINT for TYPE_INTEGER, otherwise DOUBLE.",
        ),
        field("expression", pa.string(), "Formula for a derived metric, e.g. 'sessions / totalUsers'."),
        field("custom_definition", pa.bool_(), "True for a property's own custom or calculated metric."),
        field(
            "blocked_reasons",
            pa.list_(pa.string()),
            "NO_REVENUE_METRICS / NO_COST_METRICS when this credential cannot see the metric's values.",
        ),
        field(
            "deprecated_api_names",
            pa.list_(pa.string()),
            "Older names still accepted for this metric.",
        ),
    ]
)

COMPATIBILITY_SCHEMA = pa.schema(
    [
        field("property_id", pa.string(), "Property the compatibility was checked against."),
        field("kind", pa.string(), "'dimension' or 'metric'."),
        field("api_name", pa.string(), "Dimension or metric name."),
        field("ui_name", pa.string(), "Name shown in the GA4 interface."),
        field(
            "compatibility",
            pa.string(),
            "COMPATIBLE if it can be added to a report with the requested fields, else INCOMPATIBLE.",
        ),
        field("requested", pa.bool_(), "Whether this field was one of those passed in."),
    ]
)

QUOTA_SCHEMA = pa.schema(
    [
        field("property_id", pa.string(), "Property the quota belongs to."),
        field(
            "quota",
            pa.string(),
            "tokensPerDay, tokensPerHour, tokensPerProjectPerHour, concurrentRequests, "
            "serverErrorsPerProjectPerHour or potentiallyThresholdedRequestsPerHour.",
        ),
        field("consumed", pa.int64(), "Amount used by the probe request that read this."),
        field("remaining", pa.int64(), "Amount left in the current window after that request."),
        field("description", pa.string(), "What the quota limits and when it resets."),
    ]
)
