"""From requested dimensions and metrics to a typed Arrow schema.

A GA4 report's columns are whatever the caller asked for, so its schema is
decided at bind time. The Data API returns *every* value as a string; typing
them here is what makes ``SUM(sessions)`` and ``WHERE date >= DATE '…'`` work
without a cast in every query.

* **Metrics** take the type the property's metadata declares: ``TYPE_INTEGER``
  becomes ``BIGINT``; every other type — float, currency, seconds, distances —
  becomes ``DOUBLE``, because the API itself computes them in floating point.
* **Dimensions** are ``VARCHAR`` except the calendar ones: ``date`` and
  ``firstSessionDate`` (``YYYYMMDD``) become ``DATE``; ``dateHour`` and
  ``dateHourMinute`` become a naive ``TIMESTAMP`` in the property's time zone.
  Other date parts (``year``, ``month``, ``dayOfWeek``…) stay strings because
  GA4 zero-pads them (``'01'``) and that formatting is part of their meaning.

Column names are the API names, unchanged, so the name you request is the name
you select — including custom ones like ``"customEvent:plan_tier"``, which need
double quotes in SQL.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from vgi_google_analytics.meta import field
from vgi_google_analytics.schemas import LOCAL_TIMESTAMP

DATE_DIMENSIONS = frozenset({"date", "firstSessionDate"})
TIMESTAMP_DIMENSIONS = frozenset({"dateHour", "dateHourMinute"})

#: The dimension GA4 adds when a report has more than one date range.
DATE_RANGE_COLUMN = "dateRange"

#: Optional per-row data-quality columns (``quality_columns => true``).
QUALITY_FIELDS = (
    field(
        "subject_to_thresholding",
        pa.bool_(),
        "True when GA4 withheld rows to protect user privacy (Google signals / small counts); "
        "totals may be understated.",
    ),
    field(
        "data_loss_from_other_row",
        pa.bool_(),
        "True when some dimension values were folded into an '(other)' row because of high cardinality.",
    ),
    field(
        "sampling_ratio",
        pa.float64(),
        "Share of events the report was computed from: 1.0 when unsampled, below 1.0 when "
        "GA4 sampled (minimum across date ranges).",
    ),
)

#: Realtime reports have no metadata endpoint. Their metrics are all counts.
REALTIME_INTEGER_METRICS = frozenset(
    {"activeUsers", "eventCount", "keyEvents", "conversions", "screenPageViews"}
)
REALTIME_INTEGER_DIMENSIONS = frozenset({"minutesAgo"})


def dimension_type(api_name: str) -> pa.DataType:
    """The Arrow type a report gives the dimension ``api_name``."""
    if api_name in DATE_DIMENSIONS:
        return pa.date32()
    if api_name in TIMESTAMP_DIMENSIONS:
        return LOCAL_TIMESTAMP
    return pa.string()


def metric_type(google_type: str | None) -> pa.DataType:
    """The Arrow type for a metric of Google's ``MetricType``."""
    return pa.int64() if google_type == "TYPE_INTEGER" else pa.float64()


def sql_name(kind: pa.DataType) -> str:
    """The DuckDB type name for ``kind``, for the metadata functions' ``column_type``."""
    if pa.types.is_int64(kind):
        return "BIGINT"
    if pa.types.is_float64(kind):
        return "DOUBLE"
    if pa.types.is_date32(kind):
        return "DATE"
    if pa.types.is_timestamp(kind):
        return "TIMESTAMP"
    return "VARCHAR"


@dataclass(frozen=True, slots=True)
class Catalogue:
    """One property's dimensions and metrics, indexed by every accepted name."""

    dimensions: Mapping[str, dict[str, Any]]
    metrics: Mapping[str, dict[str, Any]]

    @classmethod
    def from_metadata(cls, payload: Mapping[str, Any]) -> Catalogue:
        """Index a ``getMetadata`` response by api name and deprecated aliases."""

        def index(items: Iterable[Any]) -> dict[str, dict[str, Any]]:
            out: dict[str, dict[str, Any]] = {}
            for item in items:
                if not isinstance(item, dict) or not item.get("apiName"):
                    continue
                out[str(item["apiName"])] = item
                for alias in item.get("deprecatedApiNames") or []:
                    out.setdefault(str(alias), item)
            return out

        return cls(
            dimensions=index(payload.get("dimensions") or []),
            metrics=index(payload.get("metrics") or []),
        )


def _suggest(name: str, candidates: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.6)
    return f" Did you mean {', '.join(repr(c) for c in close)}?" if close else ""


def _validate_names(dimensions: Sequence[str], metrics: Sequence[str]) -> None:
    if not dimensions and not metrics:
        raise ValueError(
            "a report needs at least one metric or dimension, e.g. metrics => ['activeUsers'], "
            "dimensions => ['date']"
        )
    seen: set[str] = set()
    for name in [*dimensions, *metrics]:
        if not name or not name.strip():
            raise ValueError("dimension and metric names must not be empty")
        if name in seen:
            raise ValueError(f"{name!r} is requested twice; each dimension and metric becomes one column")
        seen.add(name)


def report_schema(
    dimensions: Sequence[str],
    metrics: Sequence[str],
    catalogue: Catalogue,
    *,
    date_range_column: bool = False,
    quality_columns: bool = False,
) -> pa.Schema:
    """The result schema of ``report()`` for this request.

    Raises:
        ValueError: A name the property does not know (with close matches), a
            dimension passed as a metric or vice versa, or a duplicate.
    """
    _validate_names(dimensions, metrics)
    fields: list[pa.Field] = []
    for name in dimensions:
        meta = catalogue.dimensions.get(name)
        if meta is None:
            if name in catalogue.metrics:
                raise ValueError(f"{name!r} is a metric, not a dimension; pass it in metrics => [...]")
            raise ValueError(
                f"{name!r} is not a dimension of this property.{_suggest(name, catalogue.dimensions)} "
                "List them with ga4.main.dimensions(<property>)."
            )
        fields.append(field(name, dimension_type(str(meta["apiName"])), _comment(meta)))
    if date_range_column:
        fields.append(
            field(
                DATE_RANGE_COLUMN,
                pa.string(),
                "Which date range the row belongs to: 'current' or 'comparison'.",
            )
        )
    for name in metrics:
        meta = catalogue.metrics.get(name)
        if meta is None:
            if name in catalogue.dimensions:
                raise ValueError(f"{name!r} is a dimension, not a metric; pass it in dimensions => [...]")
            raise ValueError(
                f"{name!r} is not a metric of this property.{_suggest(name, catalogue.metrics)} "
                "List them with ga4.main.metrics(<property>)."
            )
        fields.append(field(name, metric_type(meta.get("type")), _comment(meta)))
    if quality_columns:
        fields.extend(QUALITY_FIELDS)
    return pa.schema(fields)


def realtime_schema(dimensions: Sequence[str], metrics: Sequence[str]) -> pa.Schema:
    """The result schema of ``realtime_report()``.

    The Realtime API has no metadata endpoint to type against, and its surface
    is small and fixed: every standard realtime metric is a count, and
    ``minutesAgo`` is the only numeric dimension. Unknown names are left for the
    API to reject with its own message, which names the valid alternatives.
    """
    _validate_names(dimensions, metrics)
    fields = [
        field(
            name,
            pa.int64() if name in REALTIME_INTEGER_DIMENSIONS else pa.string(),
            f"Realtime dimension {name}.",
        )
        for name in dimensions
    ]
    fields.extend(
        field(
            name,
            pa.int64() if name in REALTIME_INTEGER_METRICS else pa.float64(),
            f"Realtime metric {name}.",
        )
        for name in metrics
    )
    return pa.schema(fields)


def _comment(meta: Mapping[str, Any]) -> str:
    ui = str(meta.get("uiName") or meta.get("apiName"))
    description = str(meta.get("description") or "").strip()
    return f"{ui}: {description}" if description else ui
