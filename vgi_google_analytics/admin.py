"""Configuration from the Admin API: accounts, properties, streams, custom definitions.

This is where you find the keys the reports need. ``account_summaries`` lists
every account and property the credential can read and takes no argument, so it
is the way in; the numeric ``property_id`` it returns is what every report and
per-property function takes.

Every list endpoint here is paged, and every scan pages with it — one API page
per ``process()`` tick, carried in :class:`~vgi_google_analytics.common.ListState`
— so a ``LIMIT`` stops early and a scan can be cancelled between pages.

The per-property functions also accept ``''`` for "every property this
credential can read", which turns a configuration audit (which properties are
missing a key event? which custom dimensions differ between them?) into one
query rather than one per property.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams, TableFunctionGenerator, init_single_worker
from vgi_rpc.rpc import OutputCollector

from vgi_google_analytics import ga_api
from vgi_google_analytics.common import (
    ADMIN_TTL_SECONDS,
    ANY_PROPERTY,
    REQUIRED_SECRETS,
    ListState,
    emit,
    session,
)
from vgi_google_analytics.ga_api import Session, trailing_id
from vgi_google_analytics.meta import docs, examples
from vgi_google_analytics.pushdown import build_filtered, equality
from vgi_google_analytics.schemas import (
    ACCOUNT_SCHEMA,
    ACCOUNT_SUMMARY_SCHEMA,
    CUSTOM_DIMENSION_SCHEMA,
    CUSTOM_METRIC_SCHEMA,
    DATA_STREAM_SCHEMA,
    KEY_EVENT_SCHEMA,
    PROPERTY_SCHEMA,
)

_log = logging.getLogger(__name__)

#: A property that refuses one of its configuration lists during an
#: every-property walk is skipped rather than failing the walk: rollup
#: properties, for instance, have no data streams of their own.
_SKIPPABLE_IN_FANOUT = frozenset({400, 403, 404})


# ---------------------------------------------------------------------------
# Flatteners: one Admin API resource → one or more rows
# ---------------------------------------------------------------------------


def flatten_account_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per property, or one property-less row for an account with none."""
    account = {
        "account_id": trailing_id(summary.get("account")),
        "account_name": summary.get("displayName"),
    }
    properties = [p for p in summary.get("propertySummaries") or [] if isinstance(p, dict)]
    if not properties:
        return [account]
    return [
        {
            **account,
            "property_id": trailing_id(p.get("property")),
            "property_name": p.get("displayName"),
            "property_type": p.get("propertyType"),
            "parent": p.get("parent"),
        }
        for p in properties
    ]


def flatten_account(account: dict[str, Any]) -> list[dict[str, Any]]:
    """An ``Account`` resource as a row."""
    return [
        {
            "account_id": trailing_id(account.get("name")),
            "display_name": account.get("displayName"),
            "region_code": account.get("regionCode"),
            "create_time": account.get("createTime"),
            "update_time": account.get("updateTime"),
            "gmp_organization": account.get("gmpOrganization"),
        }
    ]


def flatten_property(prop: dict[str, Any]) -> list[dict[str, Any]]:
    """A ``Property`` resource as a row."""
    return [
        {
            "property_id": trailing_id(prop.get("name")),
            "account_id": trailing_id(prop.get("account")),
            "display_name": prop.get("displayName"),
            "property_type": prop.get("propertyType"),
            "parent": prop.get("parent"),
            "time_zone": prop.get("timeZone"),
            "currency_code": prop.get("currencyCode"),
            "industry_category": prop.get("industryCategory"),
            "service_level": prop.get("serviceLevel"),
            "create_time": prop.get("createTime"),
            "update_time": prop.get("updateTime"),
        }
    ]


def flatten_data_stream(stream: dict[str, Any]) -> list[dict[str, Any]]:
    """A ``DataStream`` resource as a row; the per-platform blocks are merged."""
    web = stream.get("webStreamData") or {}
    android = stream.get("androidAppStreamData") or {}
    ios = stream.get("iosAppStreamData") or {}
    return [
        {
            "stream_id": trailing_id(stream.get("name")),
            "type": stream.get("type"),
            "display_name": stream.get("displayName"),
            "measurement_id": web.get("measurementId"),
            "default_uri": web.get("defaultUri"),
            "firebase_app_id": web.get("firebaseAppId")
            or android.get("firebaseAppId")
            or ios.get("firebaseAppId"),
            "package_name": android.get("packageName"),
            "bundle_id": ios.get("bundleId"),
            "create_time": stream.get("createTime"),
            "update_time": stream.get("updateTime"),
        }
    ]


#: How a custom definition's scope turns into the prefix a report accepts.
_SCOPE_PREFIX = {"EVENT": "customEvent", "USER": "customUser", "ITEM": "customItem"}


def custom_api_name(scope: Any, parameter: Any) -> str | None:
    """The report api name for a custom definition, e.g. ``customEvent:plan_tier``."""
    prefix = _SCOPE_PREFIX.get(str(scope or ""))
    return f"{prefix}:{parameter}" if prefix and parameter else None


def flatten_custom_dimension(dim: dict[str, Any]) -> list[dict[str, Any]]:
    """A ``CustomDimension`` resource as a row, with the api name reports take."""
    return [
        {
            "api_name": custom_api_name(dim.get("scope"), dim.get("parameterName")),
            "parameter_name": dim.get("parameterName"),
            "display_name": dim.get("displayName"),
            "description": dim.get("description"),
            "scope": dim.get("scope"),
            "disallow_ads_personalization": dim.get("disallowAdsPersonalization", False),
        }
    ]


def flatten_custom_metric(metric: dict[str, Any]) -> list[dict[str, Any]]:
    """A ``CustomMetric`` resource as a row, with the api name reports take."""
    return [
        {
            "api_name": custom_api_name(metric.get("scope") or "EVENT", metric.get("parameterName")),
            "parameter_name": metric.get("parameterName"),
            "display_name": metric.get("displayName"),
            "description": metric.get("description"),
            "measurement_unit": metric.get("measurementUnit"),
            "scope": metric.get("scope"),
            "restricted_metric_types": metric.get("restrictedMetricType") or [],
        }
    ]


def flatten_key_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """A ``KeyEvent`` resource as a row."""
    default = event.get("defaultValue") or {}
    return [
        {
            "event_name": event.get("eventName"),
            "counting_method": event.get("countingMethod"),
            "custom": event.get("custom"),
            "deletable": event.get("deletable"),
            "default_value": default.get("numericValue"),
            "default_currency_code": default.get("currencyCode"),
            "create_time": event.get("createTime"),
        }
    ]


# ---------------------------------------------------------------------------
# Paging drivers
# ---------------------------------------------------------------------------


def all_property_ids(sess: Session) -> list[str]:
    """Every property id the credential can read, in account-summary order."""
    ids: list[str] = []
    token: str | None = None
    while True:
        items, token = ga_api.admin_page("/accountSummaries", "accountSummaries", sess, page_token=token)
        for summary in items:
            ids.extend(
                row["property_id"] for row in flatten_account_summary(summary) if row.get("property_id")
            )
        if not token:
            return ids


def _emit_simple_page(
    params: ProcessParams[Any],
    state: ListState,
    out: OutputCollector,
    *,
    path: str,
    key: str,
    flatten: Callable[[dict[str, Any]], list[dict[str, Any]]],
    query: dict[str, Any] | None = None,
) -> None:
    """One page of an unparented Admin list (``/accountSummaries``, ``/accounts``)."""
    if state.done:
        out.finish()
        return
    items, token = ga_api.admin_page(
        path, key, session(params), page_token=state.page_token or None, params=query
    )
    state.page_token = token or ""
    state.done = token is None
    rows = [row for item in items for row in flatten(item)]
    emit(out, build_filtered(params, rows), ADMIN_TTL_SECONDS)


def _emit_fanout_page(
    params: ProcessParams[Any],
    state: ListState,
    out: OutputCollector,
    *,
    initial_targets: Callable[[Session], list[str]],
    fetch: Callable[[str, Session, str | None], tuple[list[dict[str, Any]], str | None]],
    flatten: Callable[[dict[str, Any]], list[dict[str, Any]]],
    stamp: str,
    fanout: bool,
) -> None:
    """One page of a list scoped to a parent, walking a queue of parents.

    Args:
        params: The tick's parameters.
        state: Queue of parents still to walk, and the head's page token.
        out: Collector.
        initial_targets: Builds the parent queue on the first tick.
        fetch: ``(parent, credentials, page_token) -> (items, next_token)``.
        flatten: Turns one API resource into rows.
        stamp: Column to stamp the parent id onto, or ``''`` for none.
        fanout: True when walking every parent rather than one the caller
            named — only then is a refusing parent skipped instead of fatal.
    """
    if state.done:
        out.finish()
        return
    sess = session(params)
    if not state.started:
        state.targets_json = json.dumps(initial_targets(sess))
        state.started = True
    targets: list[str] = json.loads(state.targets_json)
    if not targets:
        state.done = True
        out.finish()
        return
    parent = targets[0]
    try:
        items, token = fetch(parent, sess, state.page_token or None)
    except ga_api.GoogleAnalyticsError as exc:
        if not fanout or exc.status not in _SKIPPABLE_IN_FANOUT:
            raise
        _log.warning("skipping %s during an every-property walk: %s", parent, exc)
        items, token = [], None
    rows = [row for item in items for row in flatten(item)]
    if stamp:
        for row in rows:
            row[stamp] = parent
    if token:
        state.page_token = token
    else:
        state.page_token = ""
        targets.pop(0)
        state.targets_json = json.dumps(targets)
        state.done = not targets
    emit(out, build_filtered(params, rows), ADMIN_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Unparented scans (backing the catalog tables)
# ---------------------------------------------------------------------------


@init_single_worker
class AllAccountSummariesFunction(TableFunctionGenerator[None, ListState]):
    """Every account and property the credential can read — the scan behind ``account_summaries``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ACCOUNT_SUMMARY_SCHEMA

    class Meta:
        name = "all_account_summaries"
        description = "Every readable account and property (the scan backing `account_summaries`)"
        categories = ["discovery"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="discovery",
            result_schema=ACCOUNT_SUMMARY_SCHEMA,
            llm=(
                "Every Google Analytics account and GA4 property the configured credential can "
                "read, one row per property. Prefer the `account_summaries` table, which scans "
                "this. It is the starting point for everything else: the `property_id` column is "
                "the first argument of report(), realtime_report() and every per-property function."
            ),
            md=(
                "One row per readable property, with its account alongside.\n\n"
                "### Prefer the table\n\n"
                "The `account_summaries` table is backed by this function and returns exactly the "
                "same rows; this name exists only so the function and the table can share a "
                "schema.\n\n"
                "### An empty result\n\n"
                "means the credential works but has been granted access to nothing. Add its "
                "email (for a service account, the `client_email` in its key) to a property "
                "under Admin → Property access management."
            ),
            example_queries=examples(
                (
                    "Every readable property, by account",
                    "SELECT account_name, property_id, property_name "
                    "FROM ga4.main.all_account_summaries() ORDER BY account_name, property_name",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT account_name, property_id, property_name "
                    "FROM ga4.main.all_account_summaries() ORDER BY account_name, property_name"
                ),
                description="Every readable property, by account",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[None]) -> BindResponse:
        """Declare the fixed schema."""
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[None]) -> ListState:
        """Start before the first page."""
        return ListState()

    @classmethod
    def process(cls, params: ProcessParams[None], state: ListState, out: OutputCollector) -> None:
        """Emit one page of account summaries."""
        _emit_simple_page(
            params,
            state,
            out,
            path="/accountSummaries",
            key="accountSummaries",
            flatten=flatten_account_summary,
        )


@init_single_worker
class AllAccountsFunction(TableFunctionGenerator[None, ListState]):
    """Every readable account — the scan behind the ``accounts`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ACCOUNT_SCHEMA

    class Meta:
        name = "all_accounts"
        description = "Every readable Google Analytics account (the scan backing `accounts`)"
        categories = ["discovery"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="discovery",
            result_schema=ACCOUNT_SCHEMA,
            llm=(
                "Every Google Analytics account the credential can read, with its region and "
                "timestamps. Prefer the `accounts` table, which scans this. For properties, use "
                "`account_summaries` or `properties` instead."
            ),
            md=(
                "One row per account. Accounts group properties; reports are always run "
                "against a property, never an account.\n\n"
                "The `accounts` table is backed by this function and returns the same rows."
            ),
            example_queries=examples(
                (
                    "Accounts and when they were created",
                    "SELECT account_id, display_name, region_code, create_time "
                    "FROM ga4.main.all_accounts() ORDER BY create_time",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT account_id, display_name, region_code, create_time "
                    "FROM ga4.main.all_accounts() ORDER BY create_time"
                ),
                description="Accounts and when they were created",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[None]) -> BindResponse:
        """Declare the fixed schema."""
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[None]) -> ListState:
        """Start before the first page."""
        return ListState()

    @classmethod
    def process(cls, params: ProcessParams[None], state: ListState, out: OutputCollector) -> None:
        """Emit one page of accounts."""
        _emit_simple_page(params, state, out, path="/accounts", key="accounts", flatten=flatten_account)


def _account_ids(sess: Session) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    while True:
        items, token = ga_api.admin_page("/accounts", "accounts", sess, page_token=token)
        ids.extend(i for item in items if (i := trailing_id(item.get("name"))))
        if not token:
            return ids


def _properties_page(
    account: str, sess: Session, token: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    # `ancestor:` rather than `parent:` so subproperties, whose parent is a
    # property, are listed under their account too.
    return ga_api.admin_page(
        "/properties",
        "properties",
        sess,
        page_token=token,
        params={"filter": f"ancestor:accounts/{account}"},
    )


@init_single_worker
class AllPropertiesFunction(TableFunctionGenerator[None, ListState]):
    """Every readable property with its settings — the scan behind ``properties``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = PROPERTY_SCHEMA

    class Meta:
        name = "all_properties"
        description = "Every readable GA4 property with its settings (the scan backing `properties`)"
        categories = ["discovery"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        #: `WHERE account_id = '…'` lists one account instead of walking all of them.
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="discovery",
            result_schema=PROPERTY_SCHEMA,
            llm=(
                "Every GA4 property the credential can read, with the settings that change how "
                "reports read: `time_zone` (report dates are in it), `currency_code` (currency "
                "metrics are in it) and `service_level` (360 properties get 10x the API quota). "
                "Prefer the `properties` table, which scans this; filter on `account_id` to "
                "list one account."
            ),
            md=(
                "One row per property, across every readable account.\n\n"
                "### Why time zone matters\n\n"
                "GA4 buckets `date` and `dateHour` in the property's reporting time zone, not "
                "UTC, so two properties' daily numbers line up only if their zones match.\n\n"
                "The `properties` table is backed by this function and returns the same rows."
            ),
            example_queries=examples(
                (
                    "Properties with their reporting time zone and currency",
                    "SELECT property_id, display_name, time_zone, currency_code "
                    "FROM ga4.main.all_properties() ORDER BY display_name",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT property_id, display_name, time_zone, currency_code "
                    "FROM ga4.main.all_properties() ORDER BY display_name"
                ),
                description="Properties with their reporting time zone and currency",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[None]) -> BindResponse:
        """Declare the fixed schema."""
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[None]) -> ListState:
        """Start before the first page."""
        return ListState()

    @classmethod
    def process(cls, params: ProcessParams[None], state: ListState, out: OutputCollector) -> None:
        """Emit one page of one account's properties."""
        pushed = equality(params, "account_id")

        def targets(sess: Session) -> list[str]:
            if pushed is not None:
                try:
                    return [ga_api.account_id(pushed)]
                except ValueError:
                    return []  # no account has that id, so no row can match
            return _account_ids(sess)

        _emit_fanout_page(
            params,
            state,
            out,
            initial_targets=targets,
            fetch=_properties_page,
            flatten=flatten_property,
            stamp="",
            fanout=pushed is None,
        )


# ---------------------------------------------------------------------------
# Per-property configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class PropertyArgs:
    """``f(property)`` — one property, or ``''`` for every readable property."""

    property: Annotated[
        str,
        Arg(
            0,
            doc="GA4 property id, e.g. '123456789' (or 'properties/123456789'). Empty string = every "
            "property the credential can read.",
        ),
    ]


def _per_property_targets(params: ProcessParams[PropertyArgs]) -> Callable[[Session], list[str]]:
    requested = params.args.property.strip()
    if requested:
        prop = ga_api.property_id(requested)
        return lambda _sess: [prop]
    return all_property_ids


def _property_list_fetch(
    suffix: str, key: str
) -> Callable[[str, Session, str | None], tuple[list[dict[str, Any]], str | None]]:
    def fetch(prop: str, sess: Session, token: str | None) -> tuple[list[dict[str, Any]], str | None]:
        return ga_api.admin_page(f"/properties/{prop}/{suffix}", key, sess, page_token=token)

    return fetch


class _PropertyListFunction(TableFunctionGenerator[PropertyArgs, ListState]):
    """Shared driver for the per-property configuration lists."""

    FIXED_SCHEMA: ClassVar[pa.Schema]
    PATH_SUFFIX: ClassVar[str]
    RESPONSE_KEY: ClassVar[str]

    @staticmethod
    def flatten(item: dict[str, Any]) -> list[dict[str, Any]]:
        """Turn one API resource into rows."""
        raise NotImplementedError

    @classmethod
    def on_bind(cls, params: BindParams[PropertyArgs]) -> BindResponse:
        """Validate the property id up front, then declare the fixed schema."""
        if params.args.property.strip():
            ga_api.property_id(params.args.property)
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[PropertyArgs]) -> ListState:
        """Start before the first property."""
        return ListState()

    @classmethod
    def process(cls, params: ProcessParams[PropertyArgs], state: ListState, out: OutputCollector) -> None:
        """Emit one page of one property's list."""
        _emit_fanout_page(
            params,
            state,
            out,
            initial_targets=_per_property_targets(params),
            fetch=_property_list_fetch(cls.PATH_SUFFIX, cls.RESPONSE_KEY),
            flatten=cls.flatten,
            stamp="property_id",
            fanout=not params.args.property.strip(),
        )


_ALL_PROPERTIES_NOTE = (
    "\n\n### Every property at once\n\n"
    "Pass `''` instead of a property id to walk every property the credential can read — the "
    "`property_id` column says which each row came from. A property that refuses the listing "
    "(a rollup property has no streams of its own, for example) is skipped."
)


@init_single_worker
class DataStreamsFunction(_PropertyListFunction):
    """The web and app data streams feeding a property."""

    FIXED_SCHEMA = DATA_STREAM_SCHEMA
    PATH_SUFFIX = "dataStreams"
    RESPONSE_KEY = "dataStreams"

    @staticmethod
    def flatten(item: dict[str, Any]) -> list[dict[str, Any]]:
        """Rows for one resource."""
        return flatten_data_stream(item)

    class Meta:
        name = "data_streams"
        description = "Web and app data streams sending data to a GA4 property"
        categories = ["configuration"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="configuration",
            result_schema=DATA_STREAM_SCHEMA,
            llm=(
                "The data streams (websites and apps) that send data into a GA4 property. Use it "
                "to map a measurement id ('G-XXXX', what a website's tag uses) back to its "
                "property, or to find which streams a property has before filtering a report on "
                "the streamId dimension. Takes a property id, or '' for every property."
            ),
            md=(
                "One row per stream. A property can collect from several websites and apps at "
                "once; `streamId` / `streamName` are report dimensions that split them apart."
                + _ALL_PROPERTIES_NOTE
            ),
            example_queries=examples(
                (
                    "Which property does each website measurement id belong to?",
                    "SELECT measurement_id, default_uri, property_id FROM ga4.main.data_streams('') "
                    "WHERE type = 'WEB_DATA_STREAM' ORDER BY default_uri",
                ),
                (
                    "The streams of one property",
                    f"SELECT stream_id, type, display_name FROM ga4.main.data_streams({ANY_PROPERTY})",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT measurement_id, default_uri, property_id FROM ga4.main.data_streams('') "
                    "WHERE type = 'WEB_DATA_STREAM' ORDER BY default_uri"
                ),
                description="Which property does each website measurement id belong to?",
            ),
        ]


@init_single_worker
class CustomDimensionsFunction(_PropertyListFunction):
    """A property's custom dimensions, with the api names reports accept."""

    FIXED_SCHEMA = CUSTOM_DIMENSION_SCHEMA
    PATH_SUFFIX = "customDimensions"
    RESPONSE_KEY = "customDimensions"

    @staticmethod
    def flatten(item: dict[str, Any]) -> list[dict[str, Any]]:
        """Rows for one resource."""
        return flatten_custom_dimension(item)

    class Meta:
        name = "custom_dimensions"
        description = "Custom dimensions defined on a GA4 property, with their report api names"
        categories = ["configuration"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="configuration",
            result_schema=CUSTOM_DIMENSION_SCHEMA,
            llm=(
                "The custom dimensions registered on a property. The `api_name` column is the "
                "exact string to pass in report(dimensions => [...]) — e.g. "
                "'customEvent:plan_tier' — which is otherwise easy to get wrong because the "
                "prefix depends on the dimension's scope. Takes a property id, or '' for every "
                "property."
            ),
            md=(
                "One row per custom dimension.\n\n"
                "### From a definition to a report column\n\n"
                "GA4 names a custom dimension in reports by scope and parameter: "
                "`customEvent:<parameter>` for event scope, `customUser:<parameter>` for user "
                "scope, `customItem:<parameter>` for item scope. `api_name` is that string, ready "
                "to pass. The report column has the same name, and because of the colon it must "
                'be double-quoted when selected: `"customEvent:plan_tier"`.' + _ALL_PROPERTIES_NOTE
            ),
            example_queries=examples(
                (
                    "Custom dimensions of one property, as report api names",
                    "SELECT api_name, display_name, scope "
                    f"FROM ga4.main.custom_dimensions({ANY_PROPERTY}) ORDER BY api_name",
                ),
                (
                    "Custom dimensions defined on some properties but not others",
                    "SELECT api_name, count(*) AS properties FROM ga4.main.custom_dimensions('') "
                    "GROUP BY api_name ORDER BY properties",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT api_name, display_name, scope "
                    f"FROM ga4.main.custom_dimensions({ANY_PROPERTY}) ORDER BY api_name"
                ),
                description="Custom dimensions of one property, as report api names",
            ),
        ]


@init_single_worker
class CustomMetricsFunction(_PropertyListFunction):
    """A property's custom metrics, with the api names reports accept."""

    FIXED_SCHEMA = CUSTOM_METRIC_SCHEMA
    PATH_SUFFIX = "customMetrics"
    RESPONSE_KEY = "customMetrics"

    @staticmethod
    def flatten(item: dict[str, Any]) -> list[dict[str, Any]]:
        """Rows for one resource."""
        return flatten_custom_metric(item)

    class Meta:
        name = "custom_metrics"
        description = "Custom metrics defined on a GA4 property, with their report api names"
        categories = ["configuration"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="configuration",
            result_schema=CUSTOM_METRIC_SCHEMA,
            llm=(
                "The custom metrics registered on a property, with `api_name` ready to pass in "
                "report(metrics => [...]) and the unit each is measured in. Takes a property id, "
                "or '' for every property."
            ),
            md=(
                "One row per custom metric. Custom metrics are always event-scoped, so their "
                "report name is `customEvent:<parameter>`; GA4 also derives "
                "`averageCustomEvent:<parameter>` and `countCustomEvent:<parameter>`, which "
                "`metrics()` lists." + _ALL_PROPERTIES_NOTE
            ),
            example_queries=examples(
                (
                    "Custom metrics of one property, with their units",
                    "SELECT api_name, display_name, measurement_unit "
                    f"FROM ga4.main.custom_metrics({ANY_PROPERTY}) ORDER BY api_name",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT api_name, display_name, measurement_unit "
                    f"FROM ga4.main.custom_metrics({ANY_PROPERTY}) ORDER BY api_name"
                ),
                description="Custom metrics of one property, with their units",
            ),
        ]


@init_single_worker
class KeyEventsFunction(_PropertyListFunction):
    """The events a property counts as key events (formerly conversions)."""

    FIXED_SCHEMA = KEY_EVENT_SCHEMA
    PATH_SUFFIX = "keyEvents"
    RESPONSE_KEY = "keyEvents"

    @staticmethod
    def flatten(item: dict[str, Any]) -> list[dict[str, Any]]:
        """Rows for one resource."""
        return flatten_key_event(item)

    class Meta:
        name = "key_events"
        description = "Events a GA4 property counts as key events (formerly conversions)"
        categories = ["configuration"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="configuration",
            result_schema=KEY_EVENT_SCHEMA,
            llm=(
                "Which event names a property counts as key events — what GA4 called "
                "'conversions' until 2024. The keyEvents metric in a report counts exactly "
                "these; to count one of them alone, report eventCount filtered on eventName. "
                "Takes a property id, or '' for every property."
            ),
            md=(
                "One row per key event.\n\n"
                "### Key events, not conversions\n\n"
                "GA4 renamed conversions to key events in 2024. The `keyEvents` metric is the "
                "current name; `conversions` is deprecated. `counting_method` says whether a "
                "session that fires the event three times counts once or three times." + _ALL_PROPERTIES_NOTE
            ),
            example_queries=examples(
                (
                    "Key events of one property",
                    "SELECT event_name, counting_method, custom "
                    f"FROM ga4.main.key_events({ANY_PROPERTY}) ORDER BY event_name",
                ),
                (
                    "Properties that do not track a purchase key event",
                    "SELECT s.property_id, s.property_name FROM ga4.main.account_summaries s "
                    "WHERE s.property_id IS NOT NULL AND s.property_id NOT IN ("
                    "SELECT property_id FROM ga4.main.key_events('') WHERE event_name = 'purchase')",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT event_name, counting_method, custom "
                    f"FROM ga4.main.key_events({ANY_PROPERTY}) ORDER BY event_name"
                ),
                description="Key events of one property",
            ),
        ]


ADMIN_FUNCTIONS: list[type] = [
    AllAccountSummariesFunction,
    AllAccountsFunction,
    AllPropertiesFunction,
    DataStreamsFunction,
    CustomDimensionsFunction,
    CustomMetricsFunction,
    KeyEventsFunction,
]
