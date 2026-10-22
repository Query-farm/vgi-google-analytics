"""GA4 reports as table functions: ``report()`` and ``realtime_report()``.

``report()`` is the Data API's ``runReport``: pick dimensions (how to break the
numbers down) and metrics (what to count) over a date range, and get one row per
distinct combination of dimension values::

    SELECT date, sessionDefaultChannelGroup, sessions, keyEvents
    FROM ga4.main.report('123456789',
        dimensions => ['date', 'sessionDefaultChannelGroup'],
        metrics    => ['sessions', 'keyEvents'],
        start_date => '28daysAgo', end_date => 'yesterday')
    ORDER BY date;

The result schema is built at bind time from the property's own metadata, so
columns are typed (``BIGINT`` counts, ``DOUBLE`` rates and money, ``DATE``
dates) and a misspelled name fails at bind with suggestions rather than at the
API. Rows stream one API page per ``process()`` tick, offset-paged, so a
``LIMIT`` stops early and a large report never sits in memory whole.

Pass ``''`` instead of a property id to run the same report on every property
the credential can read, one property after another; the leading
``property_id`` column says whose each row is, and ``WHERE property_id = ...``
visits only the properties it names. Columns are then typed from the standard
metadata every property shares, so custom definitions need a named property.

The aggregation happens in GA4, not DuckDB — this matters. ``totalUsers`` over
``['date']`` and summed in SQL is *not* the number of users over the range,
because a user active on two days is counted on both. Ask GA4 for the
breakdown you want to read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Annotated, Any

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams, TableFunctionGenerator, init_single_worker
from vgi_rpc import ArrowSerializableDataclass
from vgi_rpc.rpc import OutputCollector

from vgi_google_analytics import ga_api
from vgi_google_analytics.admin import current_property, next_property, skippable
from vgi_google_analytics.common import REQUIRED_SECRETS, FanoutState, emit, session
from vgi_google_analytics.meta import docs, examples, field
from vgi_google_analytics.pushdown import build_filtered, dimension_filters
from vgi_google_analytics.settings import MAX_PAGE_SIZE
from vgi_google_analytics.types import (
    DATE_RANGE_COLUMN,
    Catalogue,
    realtime_schema,
    report_schema,
)

#: Dates GA4 accepts: ISO dates, 'today', 'yesterday', or 'NdaysAgo'. Declared
#: on the arguments too, so agents can read the accepted shape from the catalog.
DATE_PATTERN = r"^(\d{4}-\d{2}-\d{2}|today|yesterday|\d+daysAgo)$"
_DATE = re.compile(DATE_PATTERN)
_OPTIONAL_DATE_PATTERN = r"^(|\d{4}-\d{2}-\d{2}|today|yesterday|\d+daysAgo)$"
_DATE_DOC = "a GA4 date: an ISO date, today, yesterday, or NdaysAgo (such as 28daysAgo)"

#: Names of the two ranges when ``compare_start_date`` is given; they become the
#: values of the ``dateRange`` column.
CURRENT_RANGE = "current"
COMPARISON_RANGE = "comparison"


#: The first column of every report: which property the row came from.
PROPERTY_ID_FIELD = field(
    "property_id",
    pa.string(),
    "Property the row belongs to: the property argument, or each readable property in turn when it is ''.",
)

#: The property argument of the report functions.
_PROPERTY_DOC = "GA4 property id, e.g. '123456789'; '' runs the report on every readable property."


def _with_property(schema: pa.Schema) -> pa.Schema:
    return schema.insert(0, PROPERTY_ID_FIELD)


def _list_arg(name: str, doc: str) -> Arg[list[str]]:
    return Arg(name, doc=doc, default=[], arrow_type=pa.list_(pa.string()))


def check_date(name: str, value: str) -> str:
    """Validate a GA4 date expression, so a typo fails at bind with the accepted forms."""
    cleaned = value.strip()
    if not _DATE.match(cleaned):
        raise ValueError(
            f"{name} => {value!r} is not a GA4 date: use 'YYYY-MM-DD', 'today', 'yesterday' "
            "or 'NdaysAgo' (e.g. '28daysAgo')"
        )
    return cleaned


def parse_filter(name: str, raw: str) -> dict[str, Any] | None:
    """Parse a ``FilterExpression`` JSON argument, or ``None`` when empty."""
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON: {exc.msg} at position {exc.pos}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object (a GA4 FilterExpression)")
    return parsed


def _and(expressions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not expressions:
        return None
    if len(expressions) == 1:
        return expressions[0]
    return {"andGroup": {"expressions": expressions}}


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class ReportArgs:
    """Arguments of ``report()``; everything but the property is named."""

    property: Annotated[str, Arg(0, doc=_PROPERTY_DOC)]
    dimensions: Annotated[
        list[str],
        _list_arg(
            "dimensions",
            "Dimension api names to group by, each becoming a column; search dimensions(property) "
            "for valid names.",
        ),
    ] = dataclass_field(default_factory=list)
    metrics: Annotated[
        list[str],
        _list_arg(
            "metrics", "Metric api names to aggregate, each becoming a column; search metrics(property)."
        ),
    ] = dataclass_field(default_factory=list)
    start_date: Annotated[
        str,
        Arg(
            "start_date",
            doc=f"First day of the range, inclusive — {_DATE_DOC}.",
            default="28daysAgo",
            pattern=DATE_PATTERN,
        ),
    ] = "28daysAgo"
    end_date: Annotated[
        str,
        Arg(
            "end_date",
            doc=f"Last day of the range, inclusive, in the property's time zone — {_DATE_DOC}.",
            default="yesterday",
            pattern=DATE_PATTERN,
        ),
    ] = "yesterday"
    compare_start_date: Annotated[
        str,
        Arg(
            "compare_start_date",
            doc="Start of a second range to compare against, which adds a dateRange column; "
            "empty for no comparison.",
            default="",
            pattern=_OPTIONAL_DATE_PATTERN,
        ),
    ] = ""
    compare_end_date: Annotated[
        str,
        Arg(
            "compare_end_date",
            doc="End of the comparison range, inclusive.",
            default="",
            pattern=_OPTIONAL_DATE_PATTERN,
        ),
    ] = ""
    dimension_filter: Annotated[
        str,
        Arg(
            "dimension_filter",
            doc="GA4 FilterExpression JSON applied before aggregation; may reference dimensions "
            "that are not in the output.",
            default="",
        ),
    ] = ""
    metric_filter: Annotated[
        str,
        Arg(
            "metric_filter",
            doc="GA4 FilterExpression JSON on metrics, applied after aggregation.",
            default="",
        ),
    ] = ""
    keep_empty_rows: Annotated[
        bool,
        Arg("keep_empty_rows", doc="Also return rows whose metrics are all zero.", default=False),
    ] = False
    currency_code: Annotated[
        str,
        Arg(
            "currency_code", doc="ISO 4217 code for currency metrics; default is the property's.", default=""
        ),
    ] = ""
    max_rows: Annotated[
        int,
        Arg("max_rows", doc="Stop after this many rows (0 = every row the report has).", default=0, ge=0),
    ] = 0
    quality_columns: Annotated[
        bool,
        Arg(
            "quality_columns",
            doc="Append subject_to_thresholding, data_loss_from_other_row and sampling_ratio.",
            default=False,
        ),
    ] = False
    cache_ttl: Annotated[
        int,
        Arg("cache_ttl", doc="Seconds DuckDB may reuse this result (0 = always re-query).", default=0, ge=0),
    ] = 0


@dataclass(kw_only=True)
class ReportState(ArrowSerializableDataclass):
    """Offset paging through a report, one property at a time.

    ``request_json`` freezes the request built on the first tick. Pushed
    filters are refreshed before every tick, and an offset is only meaningful
    against the query that produced it — continuing with a different filter
    would silently skip or repeat rows. ``targets_json`` is the queue of
    properties still to run (one, unless the property argument is ``''``);
    ``offset`` belongs to its head, ``emitted`` to the whole scan.
    """

    offset: int = 0
    emitted: int = 0
    done: bool = False
    request_json: str = ""
    targets_json: str = ""
    started: bool = False
    fanout: bool = False


def _comparison(args: ReportArgs) -> bool:
    if bool(args.compare_start_date.strip()) != bool(args.compare_end_date.strip()):
        raise ValueError("compare_start_date and compare_end_date must be given together")
    return bool(args.compare_start_date.strip())


def build_report_request(args: ReportArgs, pushed: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``runReport`` body for these arguments, minus offset and limit."""
    ranges = [
        {
            "startDate": check_date("start_date", args.start_date),
            "endDate": check_date("end_date", args.end_date),
        }
    ]
    if _comparison(args):
        ranges[0]["name"] = CURRENT_RANGE
        ranges.append(
            {
                "startDate": check_date("compare_start_date", args.compare_start_date),
                "endDate": check_date("compare_end_date", args.compare_end_date),
                "name": COMPARISON_RANGE,
            }
        )
    body: dict[str, Any] = {
        "dateRanges": ranges,
        "dimensions": [{"name": name} for name in args.dimensions],
        "metrics": [{"name": name} for name in args.metrics],
        "keepEmptyRows": args.keep_empty_rows,
    }
    user_filter = parse_filter("dimension_filter", args.dimension_filter)
    combined = _and([*([user_filter] if user_filter else []), *pushed])
    if combined is not None:
        body["dimensionFilter"] = combined
    metric_filter = parse_filter("metric_filter", args.metric_filter)
    if metric_filter is not None:
        body["metricFilter"] = metric_filter
    if args.currency_code.strip():
        body["currencyCode"] = args.currency_code.strip().upper()
    return body


def report_rows(response: dict[str, Any], dimensions: list[str], metrics: list[str]) -> list[dict[str, Any]]:
    """Turn a ``runReport``/``runRealtimeReport`` response into rows keyed by column name.

    Values are mapped by *position* to the names that were requested, not by
    the header names Google echoes back: a deprecated alias is answered under
    its new name, and the column must still be called what the caller asked
    for. Any extra dimension GA4 appends — ``dateRange`` for a comparison — is
    keyed by its header name.
    """
    headers = [str(h.get("name")) for h in response.get("dimensionHeaders") or []]
    dim_names = dimensions + [
        DATE_RANGE_COLUMN if h == DATE_RANGE_COLUMN else h for h in headers[len(dimensions) :]
    ]
    meta = response.get("metadata") or {}
    sampling = [
        int(s["samplesReadCount"]) / int(s["samplingSpaceSize"])
        for s in meta.get("samplingMetadatas") or []
        if s.get("samplesReadCount") and s.get("samplingSpaceSize") and int(s["samplingSpaceSize"]) > 0
    ]
    quality = {
        "subject_to_thresholding": bool(meta.get("subjectToThresholding", False)),
        "data_loss_from_other_row": bool(meta.get("dataLossFromOtherRow", False)),
        "sampling_ratio": min(sampling) if sampling else 1.0,
    }
    rows: list[dict[str, Any]] = []
    for raw in response.get("rows") or []:
        row: dict[str, Any] = dict(quality)
        for name, value in zip(dim_names, raw.get("dimensionValues") or [], strict=False):
            row[name] = value.get("value")
        for name, value in zip(metrics, raw.get("metricValues") or [], strict=False):
            row[name] = value.get("value")
        rows.append(row)
    return rows


#: The shape of report(), which depends on its arguments.
REPORT_COLUMNS_MD = (
    "Columns follow the arguments, in this order.\n\n"
    "#### Always first\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| `property_id` | VARCHAR | Property the row belongs to. |\n\n"
    "#### One column per requested dimension\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| `date`, `firstSessionDate` | DATE | Calendar day in the property's time zone. |\n"
    "| `dateHour`, `dateHourMinute` | TIMESTAMP | Wall-clock time in the property's time zone. |\n"
    "| any other dimension | VARCHAR | The dimension's value, named exactly as requested. |\n\n"
    "#### When a comparison range is given\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| `dateRange` | VARCHAR | `current` or `comparison`. |\n\n"
    "#### One column per requested metric\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| integer metric (`TYPE_INTEGER`) | BIGINT | Counts such as activeUsers or sessions. |\n"
    "| any other metric | DOUBLE | Rates, durations, currency and distances. |\n\n"
    "#### With `quality_columns => true`\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| `subject_to_thresholding` | BOOLEAN | GA4 withheld rows for privacy. |\n"
    "| `data_loss_from_other_row` | BOOLEAN | Rare values were folded into an (other) row. |\n"
    "| `sampling_ratio` | DOUBLE | Share of events used; 1.0 when unsampled. |"
)

REALTIME_COLUMNS_MD = (
    "Columns follow the arguments, in this order.\n\n"
    "#### Always first\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| `property_id` | VARCHAR | Property the row belongs to. |\n\n"
    "#### One column per requested dimension\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| `minutesAgo` | BIGINT | Minutes before now; 0 is the current minute. |\n"
    "| any other dimension | VARCHAR | The dimension's value, named exactly as requested. |\n\n"
    "#### One column per requested metric\n\n"
    "| Name | Type | Description |\n"
    "|---|---|---|\n"
    "| activeUsers, eventCount, keyEvents, screenPageViews | BIGINT | Counts in the window. |\n"
    "| any other metric | DOUBLE | The metric's value. |"
)

_REPORT_DOCS = docs(
    category="reports",
    extra={"vgi.result_dynamic_columns_md": REPORT_COLUMNS_MD},
    llm=(
        "Run a GA4 report: one row per combination of the requested `dimensions`, with the "
        "requested `metrics` aggregated by Google over the date range (default: the last 28 "
        "complete days). This is the function for almost every traffic, engagement, acquisition "
        "or ecommerce question. Pass the property id from `account_summaries` as a literal, or '' "
        "to run the report on every readable property (the property_id column says whose each "
        "row is; custom definitions then need a named property). Find valid names "
        "with `dimensions(property)` and `metrics(property)`; custom definitions are "
        "'customEvent:<param>'. Columns are named exactly as requested — quote names containing "
        "a colon. GA4 does the aggregation: never SUM a user-count metric (totalUsers, "
        "activeUsers) across dates or other breakdowns, because the same user appears in "
        "several rows — request the coarser breakdown instead. To filter on a dimension you do "
        "not want as a column, use the `dimension_filter` JSON argument."
    ),
    md=(
        "The GA4 Data API's `runReport`, as a table.\n\n"
        "### Shape\n\n"
        "`property_id` first, then one column per requested dimension, in order, then one per "
        "metric. Metrics are "
        "`BIGINT` when GA4 declares them integers and `DOUBLE` otherwise (rates, durations, "
        "currency). `date`/`firstSessionDate` are `DATE`; `dateHour`/`dateHourMinute` are "
        "`TIMESTAMP` in the property's time zone; other dimensions are `VARCHAR`.\n\n"
        "### Dates\n\n"
        "`start_date` and `end_date` take `YYYY-MM-DD`, `today`, `yesterday` or `NdaysAgo`, "
        "inclusive, in the property's reporting time zone. Today's data is incomplete for up "
        "to a day or two while GA4 processes it.\n\n"
        "### Every property at once\n\n"
        "The property is a literal argument — DuckDB does not accept a subquery there. Pass "
        "`''` to run the same report on every property the credential can read, one after "
        "another; `WHERE property_id = '...'` (or `IN`) runs it on only those. Columns are "
        "then typed from the standard metadata every property shares, so a custom definition "
        "needs a named property. Each property reports in its own time zone and currency, and "
        "`max_rows` caps the rows of the whole scan. A property whose data the credential "
        "cannot read is skipped.\n\n"
        "### Comparing two periods\n\n"
        "Set `compare_start_date`/`compare_end_date` and a `dateRange` column is added, "
        "reading `current` or `comparison`, so period-over-period is a `PIVOT` or a self-join "
        "away.\n\n"
        "### Filtering\n\n"
        "A SQL `WHERE` on a string dimension column (`=` or `IN`) is sent to GA4 as a "
        "`dimensionFilter`, so fewer rows come back; every other predicate is applied by the "
        "worker. To filter on a dimension that is *not* a column — count purchases by country "
        "without an eventName column — pass a GA4 FilterExpression as JSON:\n\n"
        "```sql\n"
        'dimension_filter => \'{"filter": {"fieldName": "eventName", '
        '"stringFilter": {"value": "purchase"}}}\'\n'
        "```\n\n"
        "`metric_filter` is the post-aggregation (`HAVING`-like) counterpart.\n\n"
        "### Totals are GA4's, not SQL's\n\n"
        "Unique-count metrics (`totalUsers`, `activeUsers`, `newUsers`, `sessions` per user) "
        "are deduplicated within each row. Summing them across rows double-counts anyone who "
        "appears in more than one row. Request the breakdown you intend to read.\n\n"
        "### Data quality\n\n"
        "GA4 may withhold rows for privacy (thresholding), fold rare values into `(other)`, or "
        "sample. Pass `quality_columns => true` to see all three on every row.\n\n"
        "### Quota\n\n"
        "Each request costs Data API tokens from the property's hourly and daily budget; long "
        "ranges and high-cardinality dimensions cost more. `cache_ttl` lets DuckDB reuse an "
        "identical query's result, and `property_quota()` shows what is left."
    ),
    example_queries=examples(
        (
            "Daily active users and sessions over the last 28 days, per property",
            "SELECT property_id, date, activeUsers, sessions FROM ga4.main.report("
            "'', dimensions => ['date'], metrics => ['activeUsers', 'sessions']) "
            "ORDER BY property_id, date",
        ),
        (
            "Sessions and key events by channel, per property",
            "SELECT property_id, sessionDefaultChannelGroup, sessions, keyEvents FROM ga4.main.report("
            "'', dimensions => ['sessionDefaultChannelGroup'], "
            "metrics => ['sessions', 'keyEvents']) ORDER BY property_id, sessions DESC",
        ),
        (
            "Top pages last week against the week before",
            "SELECT property_id, pagePath, dateRange, screenPageViews FROM ga4.main.report("
            "'', dimensions => ['pagePath'], metrics => ['screenPageViews'], "
            "start_date => '7daysAgo', end_date => 'yesterday', "
            "compare_start_date => '14daysAgo', compare_end_date => '8daysAgo') "
            "ORDER BY screenPageViews DESC LIMIT 20",
        ),
        (
            "Purchase events by country, filtering on a dimension that is not a column",
            "SELECT property_id, country, eventCount FROM ga4.main.report("
            "'', dimensions => ['country'], metrics => ['eventCount'], "
            'dimension_filter => \'{"filter": {"fieldName": "eventName", '
            '"stringFilter": {"value": "purchase"}}}\') ORDER BY eventCount DESC',
        ),
    ),
)


@init_single_worker
class ReportFunction(TableFunctionGenerator[ReportArgs, ReportState]):
    """``runReport`` as an offset-paged scan with a bind-time typed schema."""

    class Meta:
        name = "report"
        description = "Run a GA4 report: dimensions × metrics over a date range"
        categories = ["reports"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        #: Never `filters_exactly_applied`: only string `=`/`IN` on dimensions is
        #: sent to GA4, and every pushed predicate is also applied locally.
        filter_pushdown = True
        auto_apply_filters = True
        tags = _REPORT_DOCS
        examples = [
            FunctionExample(
                sql=(
                    "SELECT property_id, date, activeUsers, sessions FROM ga4.main.report("
                    "'', dimensions => ['date'], metrics => ['activeUsers', 'sessions']) "
                    "ORDER BY property_id, date"
                ),
                description="Daily active users and sessions over the last 28 days, per property",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[ReportArgs]) -> BindResponse:
        """Validate the request and type every column against the property's metadata."""
        args = params.args
        every = not args.property.strip()
        # '' types against the universal metadata: the fields every property shares.
        prop = "0" if every else ga_api.property_id(args.property)
        build_report_request(args, [])  # dates, filters and comparison pairing fail here, not mid-scan
        if args.max_rows < 0:
            raise ValueError("max_rows must be 0 (no limit) or positive")
        catalogue = Catalogue.from_metadata(ga_api.metadata(prop, session(params)))
        try:
            schema = report_schema(
                list(args.dimensions),
                list(args.metrics),
                catalogue,
                date_range_column=_comparison(args),
                quality_columns=args.quality_columns,
            )
        except ValueError as exc:
            if not every:
                raise
            raise ValueError(
                f"{exc} With '' (every property) only the standard fields every property shares "
                "are available; pass a property id to use its custom definitions."
            ) from None
        return BindResponse(output_schema=_with_property(schema))

    @classmethod
    def initial_state(cls, params: ProcessParams[ReportArgs]) -> ReportState:
        """Start at offset zero."""
        return ReportState()

    @classmethod
    def process(cls, params: ProcessParams[ReportArgs], state: ReportState, out: OutputCollector) -> None:
        """Fetch and emit one page of one property's report."""
        args = params.args
        if state.done:
            out.finish()
            return
        prop = current_property(params, args.property, state)
        if prop is None:
            out.finish()
            return
        if not state.request_json:
            string_dimensions = [
                f.name
                for f in params.output_schema
                if f.name in args.dimensions and pa.types.is_string(f.type)
            ]
            state.request_json = json.dumps(
                build_report_request(args, dimension_filters(params, string_dimensions))
            )
        body: dict[str, Any] = json.loads(state.request_json)
        sess = session(params)
        page = sess.settings.report_page_size
        if args.max_rows:
            page = min(page, args.max_rows - state.emitted)
        body["offset"] = state.offset
        body["limit"] = page
        try:
            response = ga_api.run_report(prop, body, sess)
        except ga_api.GoogleAnalyticsError as exc:
            if not skippable(state, prop, exc):
                raise
            response = {}
        rows = report_rows(response, list(args.dimensions), list(args.metrics))
        for row in rows:
            row["property_id"] = prop
        total = int(response.get("rowCount") or 0)
        state.offset += len(rows)
        state.emitted += len(rows)
        if args.max_rows > 0 and state.emitted >= args.max_rows:
            state.done = True
        elif not rows or len(rows) < page or state.offset >= total:
            state.offset = 0
            next_property(state)
        emit(out, build_filtered(params, rows), args.cache_ttl)


# ---------------------------------------------------------------------------
# realtime_report()
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class RealtimeArgs:
    """Arguments of ``realtime_report()``."""

    property: Annotated[str, Arg(0, doc=_PROPERTY_DOC)]
    dimensions: Annotated[
        list[str],
        _list_arg(
            "dimensions",
            "Realtime dimension api names to group by (the realtime schema, not report()'s); "
            "see the function docs.",
        ),
    ] = dataclass_field(default_factory=list)
    metrics: Annotated[
        list[str],
        _list_arg(
            "metrics", "Realtime metric api names to aggregate; the function docs list the realtime set."
        ),
    ] = dataclass_field(default_factory=list)
    minutes_ago_start: Annotated[
        int,
        Arg(
            "minutes_ago_start",
            doc="Window start, in minutes before now; standard properties allow up to 29, GA4 360 up to 59.",
            default=29,
            ge=0,
            le=59,
        ),
    ] = 29
    minutes_ago_end: Annotated[
        int,
        Arg("minutes_ago_end", doc="Window end, in minutes before now (0 = now).", default=0, ge=0, le=59),
    ] = 0
    dimension_filter: Annotated[
        str, Arg("dimension_filter", doc="GA4 FilterExpression JSON on dimensions.", default="")
    ] = ""
    metric_filter: Annotated[
        str, Arg("metric_filter", doc="GA4 FilterExpression JSON on metrics.", default="")
    ] = ""
    max_rows: Annotated[
        int,
        Arg("max_rows", doc="Row limit (0 = the API maximum of 250,000).", default=0, ge=0, le=250_000),
    ] = 0


def build_realtime_request(args: RealtimeArgs, pushed: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``runRealtimeReport`` body for these arguments."""
    if not 0 <= args.minutes_ago_end <= args.minutes_ago_start:
        raise ValueError("need 0 <= minutes_ago_end <= minutes_ago_start")
    if args.max_rows < 0:
        raise ValueError("max_rows must be 0 (no limit) or positive")
    body: dict[str, Any] = {
        "dimensions": [{"name": name} for name in args.dimensions],
        "metrics": [{"name": name} for name in args.metrics],
        "minuteRanges": [{"startMinutesAgo": args.minutes_ago_start, "endMinutesAgo": args.minutes_ago_end}],
        "limit": min(args.max_rows or MAX_PAGE_SIZE, MAX_PAGE_SIZE),
    }
    user_filter = parse_filter("dimension_filter", args.dimension_filter)
    combined = _and([*([user_filter] if user_filter else []), *pushed])
    if combined is not None:
        body["dimensionFilter"] = combined
    metric_filter = parse_filter("metric_filter", args.metric_filter)
    if metric_filter is not None:
        body["metricFilter"] = metric_filter
    return body


@init_single_worker
class RealtimeReportFunction(TableFunctionGenerator[RealtimeArgs, FanoutState]):
    """``runRealtimeReport``: activity in the last 30 minutes."""

    class Meta:
        name = "realtime_report"
        description = "Run a GA4 realtime report over the last 30 minutes"
        categories = ["reports"]
        required_secrets = REQUIRED_SECRETS
        projection_pushdown = True
        filter_pushdown = True
        auto_apply_filters = True
        tags = docs(
            category="reports",
            extra={"vgi.result_dynamic_columns_md": REALTIME_COLUMNS_MD},
            llm=(
                "What is happening on a GA4 property right now: activity in the last 30 minutes "
                "(60 on GA4 360), seconds old rather than a day. Use for 'how many people are on "
                "the site now' and live monitoring; use report() for anything historical. The "
                "realtime API has its own, smaller set of dimensions and metrics — country, "
                "city, deviceCategory, platform, eventName, unifiedScreenName, minutesAgo, "
                "streamName, audienceName; activeUsers, eventCount, keyEvents, screenPageViews. "
                "Pass a property id as a literal, or '' for every readable property."
            ),
            md=(
                "The Data API's `runRealtimeReport`.\n\n"
                "### A different, smaller schema\n\n"
                "Realtime reports accept only the realtime dimensions and metrics (see Google's "
                "Realtime API schema). `minutesAgo` arrives as a `BIGINT` (0 = the current "
                "minute); every realtime metric is a count.\n\n"
                "### Window\n\n"
                "`minutes_ago_start => 4, minutes_ago_end => 0` is the last five minutes. "
                "Standard properties can look back 29 minutes, GA4 360 properties 59.\n\n"
                "### Every property at once\n\n"
                "Pass `''` to ask every readable property, one after another; `property_id` says "
                "whose each row is, and `WHERE property_id = ...` asks only those.\n\n"
                "Results are never cached — they would be stale by the time they were reused."
            ),
            example_queries=examples(
                (
                    "Active users right now, by property and country",
                    "SELECT property_id, country, activeUsers FROM ga4.main.realtime_report("
                    "'', dimensions => ['country'], metrics => ['activeUsers']) "
                    "ORDER BY activeUsers DESC",
                ),
                (
                    "Events per minute over the last half hour, per property",
                    "SELECT property_id, minutesAgo, eventCount FROM ga4.main.realtime_report("
                    "'', dimensions => ['minutesAgo'], metrics => ['eventCount']) "
                    "ORDER BY property_id, minutesAgo",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT property_id, country, activeUsers FROM ga4.main.realtime_report("
                    "'', dimensions => ['country'], metrics => ['activeUsers']) "
                    "ORDER BY activeUsers DESC"
                ),
                description="Active users right now, by property and country",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[RealtimeArgs]) -> BindResponse:
        """Validate the request and declare the schema (no network needed)."""
        if params.args.property.strip():
            ga_api.property_id(params.args.property)
        build_realtime_request(params.args, [])
        return BindResponse(
            output_schema=_with_property(
                realtime_schema(list(params.args.dimensions), list(params.args.metrics))
            )
        )

    @classmethod
    def initial_state(cls, params: ProcessParams[RealtimeArgs]) -> FanoutState:
        """Start before the first property."""
        return FanoutState()

    @classmethod
    def process(cls, params: ProcessParams[RealtimeArgs], state: FanoutState, out: OutputCollector) -> None:
        """Run one property's realtime report and emit it whole (it is bounded and unpaged)."""
        args = params.args
        prop = None if state.done else current_property(params, args.property, state)
        if prop is None:
            out.finish()
            return
        string_dimensions = [
            f.name for f in params.output_schema if f.name in args.dimensions and pa.types.is_string(f.type)
        ]
        body = build_realtime_request(args, dimension_filters(params, string_dimensions))
        try:
            response = ga_api.run_realtime_report(prop, body, session(params))
        except ga_api.GoogleAnalyticsError as exc:
            if not skippable(state, prop, exc):
                raise
            response = {}
        rows = report_rows(response, list(args.dimensions), list(args.metrics))
        for row in rows:
            row["property_id"] = prop
        next_property(state)
        out.emit(build_filtered(params, rows))


REPORT_FUNCTIONS: list[type] = [ReportFunction, RealtimeReportFunction]
