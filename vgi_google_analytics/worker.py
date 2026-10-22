"""VGI worker exposing Google Analytics 4 to DuckDB/SQL (read-only).

    ATTACH 'ga4' (TYPE vgi, LOCATION 'uv run ga4_worker.py');
    CREATE SECRET ga (TYPE google_analytics, service_account_json '...');
    SELECT * FROM ga4.account_summaries;
    SELECT date, activeUsers FROM ga4.report('123456789',
        dimensions => ['date'], metrics => ['activeUsers']);

Function names are bare (``report``, not ``ga4_report``) because they are
already qualified by the catalog they live in.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from typing import Any

from vgi import Worker
from vgi.catalog import Catalog, ReadOnlyCatalogInterface, Schema
from vgi.catalog.catalog_interface import AttachOpaqueData, CatalogAttachResult, CatalogInfo
from vgi.catalog.descriptors import ForeignKeyDef, Table

from vgi_google_analytics import __version__, auth, oauth, settings
from vgi_google_analytics.admin import (
    ADMIN_FUNCTIONS,
    AllAccountsFunction,
    AllAccountSummariesFunction,
    AllPropertiesFunction,
)
from vgi_google_analytics.common import ANY_PROPERTY
from vgi_google_analytics.discovery import DISCOVERY_FUNCTIONS
from vgi_google_analytics.meta import column_comments, docs, examples, keywords
from vgi_google_analytics.reports import REPORT_FUNCTIONS
from vgi_google_analytics.schemas import ACCOUNT_SCHEMA, ACCOUNT_SUMMARY_SCHEMA, PROPERTY_SCHEMA

IMPLEMENTATION_VERSION = __version__
DATA_VERSION_SPEC = f"=={__version__}"
SOURCE_URL = "https://github.com/Query-farm/vgi-google-analytics"

_FUNCTIONS = [*ADMIN_FUNCTIONS, *DISCOVERY_FUNCTIONS, *REPORT_FUNCTIONS]

_EXAMPLE_QUERIES = examples(
    (
        "Every property this credential can read",
        "SELECT account_name, property_id, property_name FROM ga4.main.account_summaries "
        "ORDER BY account_name, property_name",
    ),
    (
        "Daily active users and sessions for the last 28 days",
        "SELECT date, activeUsers, sessions FROM ga4.main.report("
        f"{ANY_PROPERTY}, dimensions => ['date'], metrics => ['activeUsers', 'sessions']) ORDER BY date",
    ),
    (
        "Sessions and key events by channel",
        "SELECT sessionDefaultChannelGroup, sessions, keyEvents FROM ga4.main.report("
        f"{ANY_PROPERTY}, dimensions => ['sessionDefaultChannelGroup'], "
        "metrics => ['sessions', 'keyEvents']) ORDER BY sessions DESC",
    ),
    (
        "Find the api name of a dimension by meaning",
        "SELECT api_name, ui_name FROM ga4.main.dimensions('0') WHERE description ILIKE '%landing page%'",
    ),
    (
        "Who is on the site right now, by country",
        "SELECT country, activeUsers FROM ga4.main.realtime_report("
        f"{ANY_PROPERTY}, dimensions => ['country'], metrics => ['activeUsers']) ORDER BY activeUsers DESC",
    ),
    (
        "Custom dimensions across every property",
        "SELECT property_id, api_name, display_name FROM ga4.main.custom_dimensions('') "
        "ORDER BY property_id, api_name",
    ),
)

#: Run by `vgi-lint --execute` against a live credential, so each must hold for
#: *any* account: no property id literal, no assumption about traffic volume.
_EXECUTABLE_EXAMPLES = json.dumps(
    [
        {
            "name": "summaries_and_scan_function_agree",
            "description": "The account_summaries table and the function behind it return the same rows.",
            "sql": (
                "SELECT (SELECT count(*) FROM ga4.main.account_summaries) "
                "= (SELECT count(*) FROM ga4.main.all_account_summaries())"
            ),
            "expected_result": [[True]],
        },
        {
            "name": "universal_metadata_is_populated",
            "description": "The universal dimension list includes the date dimension, typed DATE.",
            "sql": "SELECT column_type FROM ga4.main.dimensions('0') WHERE api_name = 'date'",
            "expected_result": [["DATE"]],
        },
        {
            "name": "integer_metrics_are_bigint",
            "description": "activeUsers is declared TYPE_INTEGER and so becomes BIGINT.",
            "sql": "SELECT column_type FROM ga4.main.metrics('0') WHERE api_name = 'activeUsers'",
            "expected_result": [["BIGINT"]],
        },
        {
            "name": "report_columns_are_typed",
            "description": "A report's date column is a DATE and its count metric a BIGINT.",
            "sql": (
                "SELECT typeof(date), typeof(activeUsers) FROM ga4.main.report("
                f"{ANY_PROPERTY}, dimensions => ['date'], metrics => ['activeUsers'], "
                "keep_empty_rows => true) LIMIT 1"
            ),
            "expected_result": [["DATE", "BIGINT"]],
        },
        {
            "name": "a_report_respects_max_rows",
            "description": "max_rows caps the rows a report returns.",
            "sql": (
                "SELECT count(*) <= 3 FROM ga4.main.report("
                f"{ANY_PROPERTY}, dimensions => ['date'], metrics => ['activeUsers'], max_rows => 3)"
            ),
            "expected_result": [[True]],
        },
    ]
)

#: The agent-suitability suite: questions an analyst should be able to answer
#: from the catalog metadata alone. Only the public half lives here; the graders
#: (reference SQL, success criteria) are in vgi-agent-tests.yaml, so an agent
#: under evaluation cannot read the answers off the catalog.
_AGENT_TEST_TASKS = json.dumps(
    [
        {
            "name": "find_properties",
            "prompt": "Which Google Analytics properties can I query? Give their ids and names.",
        },
        {
            "name": "daily_users",
            "prompt": "How many active users did my first GA4 property have on each of the last 7 days?",
        },
        {
            "name": "total_users_not_summed",
            "prompt": "How many distinct users visited my first GA4 property in the last 28 days?",
        },
        {
            "name": "channel_breakdown",
            "prompt": "Which marketing channels drove the most sessions to my first property last month?",
        },
        {
            "name": "filter_on_hidden_dimension",
            "prompt": (
                "In my first property, how many purchase events came from each country in the last 28 days?"
            ),
        },
        {
            "name": "find_a_dimension_name",
            "prompt": "What is the GA4 api name for the landing page dimension?",
        },
        {
            "name": "right_now",
            "prompt": "How many people are on my first property's site or app right now?",
        },
        {
            "name": "custom_dimension_names",
            "prompt": (
                "What custom dimensions does my first property have, and how do I use them in a report?"
            ),
        },
        {
            "name": "account_regions",
            "prompt": "Which Google Analytics accounts can I see, and which country is each registered in?",
        },
        {
            "name": "property_time_zones",
            "prompt": "What reporting time zone and currency does each of my GA4 properties use?",
        },
        {
            "name": "summaries_via_function",
            "prompt": "Count my readable GA4 properties per account.",
        },
        {
            "name": "valid_combination",
            "prompt": "Can I break item revenue down by session source in my first property?",
        },
        {
            "name": "custom_metric_units",
            "prompt": "What custom metrics does my first property track, and in what units?",
        },
        {
            "name": "measurement_id_lookup",
            "prompt": "Which GA4 property does each of my website measurement ids (G-...) belong to?",
        },
        {
            "name": "conversion_events",
            "prompt": "Which events count as conversions (key events) in my first property?",
        },
        {
            "name": "revenue_metrics",
            "prompt": "Which GA4 metrics measure revenue, and are any hidden from my credential?",
        },
        {
            "name": "quota_left",
            "prompt": "How much GA4 Data API quota does my first property have left this hour?",
        },
    ]
)

_CATEGORIES = json.dumps(
    [
        {
            "name": "discovery",
            "title": "Discovery",
            "description": (
                "Which accounts and properties you can read, and which dimensions and metrics each "
                "supports — where the arguments to every report come from."
            ),
            "keywords": ["accounts", "properties", "dimensions", "metrics", "metadata", "quota"],
        },
        {
            "name": "configuration",
            "title": "Property Configuration",
            "description": "Data streams, custom definitions and key events configured on a property.",
            "keywords": ["data streams", "custom dimensions", "custom metrics", "key events", "conversions"],
        },
        {
            "name": "reports",
            "title": "Reports",
            "description": "Aggregated GA4 data: historical reports over date ranges, and realtime activity.",
            "keywords": ["report", "traffic", "users", "sessions", "events", "realtime", "ecommerce"],
        },
    ]
)

_DOC_LLM = (
    "Google Analytics 4 data for every property the configured Google credential can read. Use "
    "it for website and app analytics: traffic, users, sessions, page views, acquisition "
    "channels, events, key events (conversions) and ecommerce revenue — historically by date "
    "range, or live over the last 30 minutes. Start with the `account_summaries` table to get a "
    "numeric property id; that id is the first argument of `report()`, which takes lists of "
    "`dimensions` and `metrics`. If unsure of a name, search `dimensions(property)` / "
    "`metrics(property)`. Aggregation happens in GA4: never sum user counts across rows. "
    "Read-only; use local browser sign-in with ATTACH credentials 'oauth', a `google_analytics` "
    "secret, or permitted Application Default Credentials."
)

_CATALOG_TAGS = {
    "provider": "google-analytics",
    "domain": "web-analytics",
    "vgi.title": "Google Analytics 4",
    "vgi.source_url": SOURCE_URL,
    "vgi.author": "Query Farm LLC <hello@query.farm>",
    "vgi.copyright": (
        "Worker (c) 2026 Query Farm LLC - https://query.farm. Analytics data belongs to the "
        "property owner and is accessed under the Google Analytics terms of service."
    ),
    "vgi.license": "MIT",
    "vgi.support_contact": "https://github.com/Query-farm/vgi-google-analytics/issues",
    "vgi.support_policy_url": "https://github.com/Query-farm/vgi-google-analytics/blob/main/README.md",
    "vgi.keywords": keywords(
        "google analytics",
        "ga4",
        "web analytics",
        "traffic",
        "users",
        "sessions",
        "conversions",
        "key events",
        "realtime",
    ),
    "vgi.executable_examples": _EXECUTABLE_EXAMPLES,
    "vgi.agent_test_tasks": _AGENT_TEST_TASKS,
    "vgi.doc_llm": _DOC_LLM,
    "vgi.doc_md": (
        "Google Analytics 4 (GA4) reports and configuration, read through the GA4 Data API and "
        "Admin API.\n\n"
        "### How the data is shaped\n\n"
        "- **Account** → **property** → **data streams**. Reports always run against one "
        "*property*, identified by a numeric id (not the `G-XXXX` measurement id, which names a "
        "stream).\n"
        "- A **report** is dimensions × metrics over a date range: GA4 groups by the dimensions "
        "and aggregates the metrics, then returns one row per group.\n\n"
        "### Getting started\n\n"
        "1. Take a numeric property id from the `account_summaries` table.\n"
        "2. Look up the exact dimension and metric names with `dimensions(id)` and "
        "`metrics(id)` (`'0'` lists the standard ones every property shares).\n"
        "3. Pass the id and the names to `report()`. The schema's example queries show each "
        "step.\n\n"
        "### Credentials\n\n"
        "For a local stdio worker, ATTACH with `credentials 'oauth'` to sign in through your browser "
        "to Query Farm's Google OAuth app. Each attachment carries its refreshable grant in "
        "the client's opaque attach data, so authorization survives worker restarts. Treat "
        "these bytes as credentials; do not log them. No Keychain, credential file or user "
        "app setup is needed. A new ATTACH starts a new sign-in.\n\n"
        "A `google_analytics` secret holding a service-account key (the account added to the "
        "property as a Viewer), or an OAuth client id, client secret and refresh token also works:\n\n"
        "```sql\n"
        "CREATE SECRET ga (TYPE google_analytics, service_account_json '<key file contents>');\n"
        "```\n\n"
        "A local worker also falls back to Application Default Credentials. Only the "
        "`analytics.readonly` scope is ever requested.\n\n"
        "### Quota\n\n"
        "Every report spends Data API tokens from the property's hourly and daily budget. "
        "The quota function shows what remains, and a report's `cache_ttl` argument lets DuckDB "
        "reuse an identical result instead of spending more."
    ),
}

_SCHEMA_TAGS = {
    "provider": "google-analytics",
    "domain": "web-analytics",
    "vgi.title": "GA4 Reports & Configuration",
    "vgi.categories": _CATEGORIES,
    "vgi.keywords": keywords("google analytics", "ga4", "report", "dimensions", "metrics", "realtime"),
    "vgi.example_queries": _EXAMPLE_QUERIES,
    "vgi.doc_llm": (
        "Read-only access to Google Analytics 4 for every property the configured credential "
        "can read. Work in three steps: discover a numeric property id, look up the exact "
        "camelCase names of the dimensions (ways to break numbers down) and metrics (numbers to "
        "measure) that property supports, then ask for a report over a date range. Reports are "
        "aggregated by GA4 itself and come back with typed columns; configuration (streams, "
        "custom definitions, key events) can be read for one property or all at once."
    ),
    "vgi.doc_md": (
        "Google Analytics 4 reports and property configuration, read-only.\n\n"
        "### Concepts\n\n"
        "- A **property** is the unit everything is reported on; its id is numeric. A "
        "measurement id such as `G-XXXX` names a data stream, not a property.\n"
        "- A **dimension** is an attribute to group by (date, country, page, channel); a "
        "**metric** is a number GA4 aggregates within each group (users, sessions, revenue).\n"
        "- A **report** is a set of dimensions and metrics over a date range in the property's "
        "own time zone.\n\n"
        "### Reports are typed\n\n"
        "Columns come back as `BIGINT`, `DOUBLE`, `DATE` and `TIMESTAMP` rather than strings, "
        "because the property's metadata is read when the query is bound — which is also why a "
        "misspelled name fails immediately with suggestions.\n\n"
        "### The one real trap\n\n"
        "GA4 aggregates before SQL sees anything. Unique counts such as `totalUsers` and "
        "`activeUsers` are deduplicated *within* a row; adding them up across dates, pages or "
        "channels counts a returning user once per row they appear in. Ask GA4 for the "
        "breakdown you mean."
    ),
}

#: Classifying tags every table carries, for faceted discovery.
_CLASSIFY = {"provider": "google-analytics", "domain": "web-analytics"}

_SUMMARIES_DOCS = docs(
    category="discovery",
    llm=(
        "Every GA4 property the credential can read, one row per property with its account. "
        "This is the entry point: take `property_id` from here for report() and every "
        "per-property function. An empty result means the credential has not been granted "
        "access to any property."
    ),
    md=(
        "One row per readable property (plus one row with a NULL property for an account that "
        "has none the credential can read).\n\n"
        "### Joining\n\n"
        "`account_id` joins to `accounts.account_id` for the account's region and dates; "
        "`property_id` joins to `properties.property_id` for time zone, currency and service "
        "level.\n\n"
        "Cheap — a single Admin API call for most users — and cached for five minutes."
    ),
    example_queries=examples(
        (
            "Every property this credential can read",
            "SELECT account_name, property_id, property_name FROM ga4.main.account_summaries "
            "ORDER BY account_name, property_name",
        ),
        (
            "Properties per account",
            "SELECT account_name, count(property_id) AS properties FROM ga4.main.account_summaries "
            "GROUP BY account_name ORDER BY properties DESC",
        ),
    ),
    extra={
        **_CLASSIFY,
        "vgi.title": "Account & Property Summaries",
        "vgi.keywords": keywords("accounts", "properties", "discovery", "property id"),
    },
)

_ACCOUNTS_DOCS = docs(
    category="discovery",
    llm="Every Google Analytics account the credential can read, with region and timestamps.",
    md="One row per account. Reports run against properties, not accounts — see `account_summaries`.",
    example_queries=examples(
        (
            "Accounts by creation date",
            "SELECT account_id, display_name, create_time FROM ga4.main.accounts ORDER BY create_time",
        ),
        (
            "Accounts by region",
            "SELECT region_code, count(*) AS accounts FROM ga4.main.accounts GROUP BY region_code",
        ),
    ),
    extra={
        **_CLASSIFY,
        "vgi.title": "Google Analytics Accounts",
        "vgi.keywords": keywords("accounts", "organization"),
    },
)

_PROPERTIES_DOCS = docs(
    category="discovery",
    llm=(
        "Every readable GA4 property with its reporting time zone, currency and service level "
        "(standard or 360). Use it when a report's dates or currency need interpreting, or to "
        "compare property settings. Filter on account_id to list a single account."
    ),
    md=(
        "One row per property across every readable account.\n\n"
        "`time_zone` decides how GA4 buckets `date` and `dateHour`; `currency_code` is the unit "
        "of currency metrics; `service_level` sets the API quota (360 = 10x).\n\n"
        "### Joining\n\n"
        "`account_id` joins to `accounts.account_id`. `property_id` is the key every report and "
        "per-property function takes, and the column the configuration functions stamp on their "
        "rows, so it joins to their output too."
    ),
    example_queries=examples(
        (
            "Properties with their time zone and currency",
            "SELECT property_id, display_name, time_zone, currency_code FROM ga4.main.properties "
            "ORDER BY display_name",
        ),
        (
            "GA4 360 properties",
            "SELECT property_id, display_name FROM ga4.main.properties "
            "WHERE service_level = 'GOOGLE_ANALYTICS_360'",
        ),
    ),
    extra={
        **_CLASSIFY,
        "vgi.title": "GA4 Property Settings",
        "vgi.keywords": keywords("properties", "time zone", "currency"),
    },
)

_GA4_CATALOG = Catalog(
    name="ga4",
    default_schema="main",
    comment="Read-only Google Analytics 4: reports, realtime, metadata and property configuration",
    tags=_CATALOG_TAGS,
    schemas=[
        Schema(
            path=["main"],
            comment="GA4 Data API and Admin API (read-only, analytics.readonly scope)",
            tags=_SCHEMA_TAGS,
            functions=list(_FUNCTIONS),
            tables=[
                Table(
                    name="account_summaries",
                    function=AllAccountSummariesFunction,
                    comment="Every readable GA4 property with its account — the entry point",
                    tags=_SUMMARIES_DOCS,
                    column_comments=column_comments(ACCOUNT_SUMMARY_SCHEMA),
                    not_null=("account_id",),
                    unique=(("account_id", "property_id"),),
                    foreign_key=(
                        ForeignKeyDef(
                            columns=("account_id",),
                            referenced_table="accounts",
                            referenced_columns=("account_id",),
                        ),
                        ForeignKeyDef(
                            columns=("property_id",),
                            referenced_table="properties",
                            referenced_columns=("property_id",),
                        ),
                    ),
                ),
                Table(
                    name="accounts",
                    function=AllAccountsFunction,
                    comment="Every readable Google Analytics account",
                    tags=_ACCOUNTS_DOCS,
                    column_comments=column_comments(ACCOUNT_SCHEMA),
                    primary_key=(("account_id",),),
                    not_null=("account_id",),
                ),
                Table(
                    name="properties",
                    function=AllPropertiesFunction,
                    comment="Every readable GA4 property with its time zone, currency and service level",
                    tags=_PROPERTIES_DOCS,
                    column_comments=column_comments(PROPERTY_SCHEMA),
                    primary_key=(("property_id",),),
                    not_null=("property_id", "account_id"),
                    foreign_key=(
                        ForeignKeyDef(
                            columns=("account_id",),
                            referenced_table="accounts",
                            referenced_columns=("account_id",),
                        ),
                    ),
                ),
            ],
        ),
    ],
)


class GA4Catalog(ReadOnlyCatalogInterface):
    """Advertises the versions, the ``google_analytics`` secret type and the ATTACH options."""

    catalog = _GA4_CATALOG
    catalog_name = _GA4_CATALOG.name
    secret_types = [auth.SECRET_SPEC]
    attach_option_specs = settings.OPTION_SPECS

    def catalog_attach(self, *, name: str, options: dict[str, Any], **kwargs: Any) -> CatalogAttachResult:
        """Validate the ATTACH options and carry them to every function.

        A bad option fails the ATTACH itself rather than every later query.
        The validated settings become the catalog's attach bytes, which the
        framework hands back to each call as ``attach_opaque_data``.
        """
        validated = settings.Settings.from_options(options)
        result = super().catalog_attach(name=name, options=options, **kwargs)
        if validated.credentials == settings.OAUTH:
            validated = replace(validated, oauth_grant=oauth.authorize())
        return replace(
            result,
            attach_opaque_data=AttachOpaqueData(validated.encode()),
            attach_opaque_data_required=True,
        )

    def catalog_detach(self, *, attach_opaque_data: AttachOpaqueData) -> None:
        """Drop this worker's credential cache for the client's detached catalog."""
        attached = settings.Settings.decode(attach_opaque_data)
        if attached.credentials == settings.OAUTH:
            oauth.forget(attached.oauth_grant)

    def catalogs(self) -> list[CatalogInfo]:
        """Advertise the single read-only GA4 catalog."""
        return [
            CatalogInfo(
                name=self._effective_catalog_name,
                implementation_version=IMPLEMENTATION_VERSION,
                data_version_spec=DATA_VERSION_SPEC,
                source_url=SOURCE_URL,
                attach_option_specs=[spec.serialize() for spec in self.attach_option_specs],
            )
        ]


class GA4Worker(Worker):
    """Worker process hosting the read-only GA4 catalog."""

    catalog = _GA4_CATALOG
    catalog_interface = GA4Catalog


def main() -> None:
    """Run the worker (stdio by default; pass ``--http`` for the HTTP server)."""
    _run(http=False)


#: Operator flag for the HTTP server: permit the Application Default Credentials fallback.
ALLOW_ADC_FLAG = "--allow-adc"


def main_http() -> None:
    """Run the worker over HTTP.

    An HTTP worker may serve many clients, so it does **not** fall back to its
    own Application Default Credentials unless the operator starts it with
    ``--allow-adc`` — otherwise any client without a secret would query GA4 as
    the server. This is a command-line flag rather than an ATTACH option because
    ATTACH options are chosen by clients.
    """
    _run(http=True)


def _run(*, http: bool) -> None:
    """Restrict local credentials for every shared transport, including direct CLI flags."""
    argv = sys.argv[1:]
    shared = http or any(arg.split("=", maxsplit=1)[0] in {"--http", "--tcp", "--unix"} for arg in argv)
    oauth.permit_local(not shared)
    auth.permit_adc(not shared or ALLOW_ADC_FLAG in argv)
    argv = [arg for arg in argv if arg != ALLOW_ADC_FLAG]
    if http and "--http" not in argv:
        argv = ["--http", *argv]
    sys.argv = [sys.argv[0], *argv]
    GA4Worker.main()
