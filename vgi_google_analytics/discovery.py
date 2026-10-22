"""What a property can report on, and what it has left to spend.

* ``dimensions(property)`` / ``metrics(property)`` — every field ``report()``
  accepts for that property, custom definitions included, with the SQL type
  each column will have. Property ``'0'`` (or ``''``) is the universal set.
* ``check_compatibility(property, dimensions =>, metrics =>)`` — which fields
  can be combined in one report. GA4 refuses some combinations (most
  user-scoped dimensions with item-scoped metrics, for instance), and this says
  so before a report fails.
* ``property_quota(property)`` — the Data API token budget left this hour and
  today.

``check_compatibility`` and ``property_quota`` also take ``''`` for every
readable property, one call per property, stamped with ``property_id``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams, TableFunctionGenerator, init_single_worker
from vgi_rpc.rpc import OutputCollector

from vgi_google_analytics import ga_api
from vgi_google_analytics.admin import current_property, next_property, skippable
from vgi_google_analytics.common import (
    METADATA_TTL_SECONDS,
    REQUIRED_SECRETS,
    FanoutState,
    emit,
    session,
)
from vgi_google_analytics.meta import docs, examples
from vgi_google_analytics.pushdown import build_filtered
from vgi_google_analytics.reports import parse_filter
from vgi_google_analytics.schemas import (
    COMPATIBILITY_SCHEMA,
    DIMENSION_METADATA_SCHEMA,
    METRIC_METADATA_SCHEMA,
    QUOTA_SCHEMA,
)
from vgi_google_analytics.types import dimension_type, metric_type, sql_name


@dataclass(slots=True, frozen=True, kw_only=True)
class MetadataArgs:
    """``dimensions(property)`` / ``metrics(property)``."""

    property: Annotated[
        str,
        Arg(0, doc="GA4 property id; '0' or '' for the universal set shared by every property."),
    ]


def _metadata_property(raw: str) -> str:
    return ga_api.property_id(raw) if raw.strip() else "0"


def flatten_dimensions(prop: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """``getMetadata`` dimensions as rows, with the report column type each gets."""
    return [
        {
            "property_id": prop,
            "api_name": d.get("apiName"),
            "ui_name": d.get("uiName"),
            "description": d.get("description"),
            "category": d.get("category"),
            "custom_definition": bool(d.get("customDefinition", False)),
            "column_type": sql_name(dimension_type(str(d.get("apiName") or ""))),
            "deprecated_api_names": d.get("deprecatedApiNames") or [],
        }
        for d in payload.get("dimensions") or []
        if isinstance(d, dict)
    ]


def flatten_metrics(prop: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """``getMetadata`` metrics as rows, with the report column type each gets."""
    return [
        {
            "property_id": prop,
            "api_name": m.get("apiName"),
            "ui_name": m.get("uiName"),
            "description": m.get("description"),
            "category": m.get("category"),
            "type": m.get("type"),
            "column_type": sql_name(metric_type(m.get("type"))),
            "expression": m.get("expression"),
            "custom_definition": bool(m.get("customDefinition", False)),
            "blocked_reasons": m.get("blockedReasons") or [],
            "deprecated_api_names": m.get("deprecatedApiNames") or [],
        }
        for m in payload.get("metrics") or []
        if isinstance(m, dict)
    ]


class _MetadataFunction(TableFunctionGenerator[MetadataArgs, None]):
    """Shared body of ``dimensions()`` and ``metrics()``: one cached metadata fetch."""

    FIXED_SCHEMA: ClassVar[pa.Schema]

    @staticmethod
    def flatten(prop: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Turn the metadata payload into this function's rows."""
        raise NotImplementedError

    @classmethod
    def on_bind(cls, params: BindParams[MetadataArgs]) -> BindResponse:
        """Validate the property id and declare the fixed schema."""
        _metadata_property(params.args.property)
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(cls, params: ProcessParams[MetadataArgs], state: None, out: OutputCollector) -> None:
        """Fetch the property's metadata and emit it whole."""
        prop = _metadata_property(params.args.property)
        payload = ga_api.metadata(prop, session(params))
        emit(out, build_filtered(params, cls.flatten(prop, payload)), METADATA_TTL_SECONDS)
        out.finish()


@init_single_worker
class DimensionsFunction(_MetadataFunction):
    """Every dimension a property's reports accept."""

    FIXED_SCHEMA = DIMENSION_METADATA_SCHEMA

    @staticmethod
    def flatten(prop: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Dimension rows."""
        return flatten_dimensions(prop, payload)

    class Meta:
        name = "dimensions"
        description = "Every dimension report() accepts for a property, custom ones included"
        categories = ["discovery"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="discovery",
            result_schema=DIMENSION_METADATA_SCHEMA,
            llm=(
                "Every dimension (a way to break numbers down: date, country, pagePath, "
                "sessionSource, eventName, ...) that report() accepts for a property, with "
                "Google's description and the SQL type the column will have. Search it before "
                "guessing a name — GA4 names are camelCase and not always obvious "
                "(sessionDefaultChannelGroup, not channel). Pass '0' for the universal list."
            ),
            md=(
                "One row per dimension. `api_name` is what to pass in "
                "`report(dimensions => [...])` and what the column will be called.\n\n"
                "Custom dimensions appear with `custom_definition = true` and names like "
                "`customEvent:plan_tier`. Property `'0'` lists only the standard dimensions "
                "every property shares.\n\n"
                "Cached for an hour: the list only changes when a custom definition does."
            ),
            example_queries=examples(
                (
                    "Traffic-source dimensions",
                    "SELECT api_name, ui_name, description FROM ga4.main.dimensions('0') "
                    "WHERE category = 'Traffic source' ORDER BY api_name",
                ),
                (
                    "Find a dimension by what it means",
                    "SELECT api_name, ui_name FROM ga4.main.dimensions('0') "
                    "WHERE description ILIKE '%landing page%'",
                ),
                (
                    "Dimensions that are new names for a deprecated one",
                    "SELECT api_name, deprecated_api_names FROM ga4.main.dimensions('0') "
                    "WHERE len(deprecated_api_names) > 0",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT api_name, ui_name, description FROM ga4.main.dimensions('0') "
                    "WHERE category = 'Traffic source' ORDER BY api_name"
                ),
                description="Traffic-source dimensions",
            ),
        ]


@init_single_worker
class MetricsFunction(_MetadataFunction):
    """Every metric a property's reports accept."""

    FIXED_SCHEMA = METRIC_METADATA_SCHEMA

    @staticmethod
    def flatten(prop: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Metric rows."""
        return flatten_metrics(prop, payload)

    class Meta:
        name = "metrics"
        description = "Every metric report() accepts for a property, custom ones included"
        categories = ["discovery"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="discovery",
            result_schema=METRIC_METADATA_SCHEMA,
            llm=(
                "Every metric (a number to measure: activeUsers, sessions, screenPageViews, "
                "keyEvents, totalRevenue, engagementRate, ...) that report() accepts for a "
                "property, with its type, unit and the SQL column type. `blocked_reasons` "
                "non-empty means this credential will see zeros for it (no cost/revenue "
                "access). Pass '0' for the universal list."
            ),
            md=(
                "One row per metric. `api_name` is what to pass in `report(metrics => [...])`.\n\n"
                "`type` is Google's (`TYPE_INTEGER`, `TYPE_CURRENCY`, `TYPE_SECONDS`, ...) and "
                "`column_type` is what that becomes in SQL. `expression` shows how a derived "
                "metric is computed, e.g. `engagedSessions / sessions`.\n\n"
                "Cached for an hour."
            ),
            example_queries=examples(
                (
                    "Ecommerce metrics and their types",
                    "SELECT api_name, ui_name, type FROM ga4.main.metrics('0') "
                    "WHERE category = 'Ecommerce' ORDER BY api_name",
                ),
                (
                    "Derived metrics and how they are computed",
                    "SELECT api_name, expression FROM ga4.main.metrics('0') "
                    "WHERE expression IS NOT NULL AND expression <> '' ORDER BY api_name",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT api_name, ui_name, type FROM ga4.main.metrics('0') "
                    "WHERE category = 'Ecommerce' ORDER BY api_name"
                ),
                description="Ecommerce metrics and their types",
            ),
        ]


# ---------------------------------------------------------------------------
# check_compatibility()
# ---------------------------------------------------------------------------


def _list_arg(name: str, doc: str) -> Arg[list[str]]:
    return Arg(name, doc=doc, default=[], arrow_type=pa.list_(pa.string()))


@dataclass(slots=True, frozen=True, kw_only=True)
class CompatibilityArgs:
    """``check_compatibility(property, dimensions =>, metrics =>)``."""

    property: Annotated[
        str, Arg(0, doc="GA4 property id, e.g. '123456789'; '' checks every readable property.")
    ]
    dimensions: Annotated[list[str], _list_arg("dimensions", "Dimensions you intend to report on.")] = field(
        default_factory=list
    )
    metrics: Annotated[list[str], _list_arg("metrics", "Metrics you intend to report on.")] = field(
        default_factory=list
    )
    dimension_filter: Annotated[
        str, Arg("dimension_filter", doc="FilterExpression JSON the report would use.", default="")
    ] = ""
    metric_filter: Annotated[
        str, Arg("metric_filter", doc="Metric FilterExpression JSON the report would use.", default="")
    ] = ""


def flatten_compatibility(
    prop: str, payload: dict[str, Any], dimensions: list[str], metrics: list[str]
) -> list[dict[str, Any]]:
    """``checkCompatibility`` results as rows, flagging the requested fields."""
    rows: list[dict[str, Any]] = []
    for kind, key, meta_key, requested in (
        ("dimension", "dimensionCompatibilities", "dimensionMetadata", set(dimensions)),
        ("metric", "metricCompatibilities", "metricMetadata", set(metrics)),
    ):
        for item in payload.get(key) or []:
            meta = item.get(meta_key) or {}
            name = meta.get("apiName")
            rows.append(
                {
                    "property_id": prop,
                    "kind": kind,
                    "api_name": name,
                    "ui_name": meta.get("uiName"),
                    "compatibility": item.get("compatibility"),
                    "requested": name in requested,
                }
            )
    return rows


@init_single_worker
class CheckCompatibilityFunction(TableFunctionGenerator[CompatibilityArgs, FanoutState]):
    """``checkCompatibility``: which fields can join the ones you have chosen."""

    class Meta:
        name = "check_compatibility"
        description = "Which dimensions and metrics can be combined with the ones given"
        categories = ["discovery"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="discovery",
            result_schema=COMPATIBILITY_SCHEMA,
            llm=(
                "Before running an unusual report, check whether its dimensions and metrics can "
                "be combined: GA4 rejects some pairings (item-scoped metrics with session-scoped "
                "dimensions, for example). Returns every dimension and metric with COMPATIBLE or "
                "INCOMPATIBLE given the ones passed in; rows with requested = true are the ones "
                "you asked about. Also answers 'what else could I add to this report?'. Costs no "
                "report quota. Pass a property id as a literal, or '' to check every property."
            ),
            md=(
                "The Data API's `checkCompatibility`. One row for every dimension and metric of "
                "the property, saying whether it could be added to a report containing the "
                "fields passed in.\n\n"
                "If any requested field is `INCOMPATIBLE`, `report()` with the same fields will "
                "fail; drop it, or split the question into two reports.\n\n"
                "Pass `''` to check every readable property; `property_id` says which property "
                "each row describes."
            ),
            example_queries=examples(
                (
                    "Is the requested combination valid?",
                    "SELECT property_id, api_name, compatibility FROM ga4.main.check_compatibility("
                    "'', dimensions => ['itemName'], metrics => ['sessions']) "
                    "WHERE requested ORDER BY property_id, api_name",
                ),
                (
                    "Which dimensions can break down item revenue?",
                    "SELECT DISTINCT api_name FROM ga4.main.check_compatibility("
                    "'', metrics => ['itemRevenue']) "
                    "WHERE kind = 'dimension' AND compatibility = 'COMPATIBLE' ORDER BY api_name",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT property_id, api_name, compatibility FROM ga4.main.check_compatibility("
                    "'', dimensions => ['itemName'], metrics => ['sessions']) "
                    "WHERE requested ORDER BY property_id, api_name"
                ),
                description="Is the requested combination valid?",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[CompatibilityArgs]) -> BindResponse:
        """Validate arguments and declare the fixed schema."""
        if params.args.property.strip():
            ga_api.property_id(params.args.property)
        parse_filter("dimension_filter", params.args.dimension_filter)
        parse_filter("metric_filter", params.args.metric_filter)
        if not params.args.dimensions and not params.args.metrics:
            raise ValueError("pass at least one of dimensions => [...] or metrics => [...]")
        return BindResponse(output_schema=COMPATIBILITY_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[CompatibilityArgs]) -> FanoutState:
        """Start before the first property."""
        return FanoutState()

    @classmethod
    def process(
        cls, params: ProcessParams[CompatibilityArgs], state: FanoutState, out: OutputCollector
    ) -> None:
        """Ask GA4 about one property and emit one row per field."""
        args = params.args
        prop = None if state.done else current_property(params, args.property, state)
        if prop is None:
            out.finish()
            return
        body: dict[str, Any] = {
            "dimensions": [{"name": d} for d in args.dimensions],
            "metrics": [{"name": m} for m in args.metrics],
        }
        if dimension_filter := parse_filter("dimension_filter", args.dimension_filter):
            body["dimensionFilter"] = dimension_filter
        if metric_filter := parse_filter("metric_filter", args.metric_filter):
            body["metricFilter"] = metric_filter
        try:
            payload = ga_api.check_compatibility(prop, body, session(params))
        except ga_api.GoogleAnalyticsError as exc:
            if not skippable(state, prop, exc):
                raise
            payload = {}
        rows = flatten_compatibility(prop, payload, list(args.dimensions), list(args.metrics))
        next_property(state)
        emit(out, build_filtered(params, rows), METADATA_TTL_SECONDS)


# ---------------------------------------------------------------------------
# property_quota()
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class QuotaArgs:
    """``property_quota(property)``."""

    property: Annotated[
        str, Arg(0, doc="GA4 property id, e.g. '123456789'; '' reads every readable property's quota.")
    ]


#: What each quota limits; Google's names are terse.
QUOTA_DESCRIPTIONS = {
    "tokensPerDay": "Data API tokens per property per day (resets at midnight Pacific).",
    "tokensPerHour": "Data API tokens per property per hour.",
    "tokensPerProjectPerHour": "Tokens this Google Cloud project may spend on the property per hour.",
    "concurrentRequests": "Requests that may run against the property at the same time.",
    "serverErrorsPerProjectPerHour": "Server errors this project may cause on the property per hour.",
    "potentiallyThresholdedRequestsPerHour": (
        "Requests per hour that may include Google-signals dimensions subject to thresholding."
    ),
}

#: The cheapest request that still returns a quota snapshot.
QUOTA_PROBE = {
    "dateRanges": [{"startDate": "yesterday", "endDate": "yesterday"}],
    "metrics": [{"name": "activeUsers"}],
    "limit": 1,
    "returnPropertyQuota": True,
}


def flatten_quota(prop: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """A ``propertyQuota`` object as one row per quota."""
    quota = payload.get("propertyQuota") or {}
    return [
        {
            "property_id": prop,
            "quota": name,
            "consumed": (status or {}).get("consumed", 0),
            "remaining": (status or {}).get("remaining"),
            "description": QUOTA_DESCRIPTIONS.get(name, ""),
        }
        for name, status in quota.items()
        if isinstance(status, dict)
    ]


@init_single_worker
class PropertyQuotaFunction(TableFunctionGenerator[QuotaArgs, FanoutState]):
    """The property's remaining Data API quota."""

    class Meta:
        name = "property_quota"
        description = "Remaining Data API quota (tokens per hour/day, concurrency) for a property"
        categories = ["discovery"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="discovery",
            result_schema=QUOTA_SCHEMA,
            llm=(
                "How much Data API quota a property has left this hour and today. Check it when "
                "report() fails with RESOURCE_EXHAUSTED, or before running many large reports. "
                "Reading it runs one tiny report, which itself costs a token or so. Pass a "
                "property id as a literal, or '' for every readable property."
            ),
            md=(
                "One row per quota. Standard properties get 200,000 tokens a day and 40,000 an "
                "hour; GA4 360 ten times that. A report's cost grows with its date range, "
                "dimensions and rows.\n\n"
                "The snapshot is read from a one-row probe report, so `consumed` is that probe's "
                "own cost and `remaining` is what is left after it.\n\n"
                "Pass `''` to read every readable property's quota (one probe each); "
                "`WHERE property_id = ...` reads only those."
            ),
            example_queries=examples(
                (
                    "Tokens left this hour and today, per property",
                    "SELECT property_id, quota, remaining FROM ga4.main.property_quota('') "
                    "WHERE quota IN ('tokensPerHour', 'tokensPerDay') ORDER BY property_id, quota",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT property_id, quota, remaining FROM ga4.main.property_quota('') "
                    "WHERE quota IN ('tokensPerHour', 'tokensPerDay') ORDER BY property_id, quota"
                ),
                description="Tokens left this hour and today, per property",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[QuotaArgs]) -> BindResponse:
        """Validate the property id and declare the fixed schema."""
        if params.args.property.strip():
            ga_api.property_id(params.args.property)
        return BindResponse(output_schema=QUOTA_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[QuotaArgs]) -> FanoutState:
        """Start before the first property."""
        return FanoutState()

    @classmethod
    def process(cls, params: ProcessParams[QuotaArgs], state: FanoutState, out: OutputCollector) -> None:
        """Run one property's probe report and emit its quota snapshot."""
        prop = None if state.done else current_property(params, params.args.property, state)
        if prop is None:
            out.finish()
            return
        try:
            payload = ga_api.run_report(prop, QUOTA_PROBE, session(params))
        except ga_api.GoogleAnalyticsError as exc:
            if not skippable(state, prop, exc):
                raise
            payload = {}
        next_property(state)
        out.emit(build_filtered(params, flatten_quota(prop, payload)))


DISCOVERY_FUNCTIONS: list[type] = [
    DimensionsFunction,
    MetricsFunction,
    CheckCompatibilityFunction,
    PropertyQuotaFunction,
]
