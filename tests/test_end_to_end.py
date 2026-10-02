"""SQL executed against a real ATTACH, offline.

The worker runs as a subprocess launched by the vgi DuckDB extension (shipped in
Haybarn), exactly as a user runs it, and talks over a real socket to
:mod:`tests.fake_ga`. That exercises what unit tests cannot: argument binding,
bind-time schemas, secret delivery, filter pushdown, projection, and paging
across ``process()`` ticks — while every request the worker makes is visible
in ``fake_ga.REQUESTS`` for assertions.

The live counterpart, against real Google Analytics, is ``test_live.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests import fake_ga

ROOT = Path(__file__).resolve().parent.parent

#: Small pages so a modest synthetic report spans several ticks.
PAGE_SIZE = 10


def attach_sql(url: str, **extra: Any) -> str:
    """An ATTACH aimed at the fake through ATTACH options — no environment variables."""
    options = {
        "data_api_url": f"{url}/data/v1beta",
        "admin_api_url": f"{url}/admin/v1beta",
        "report_page_size": PAGE_SIZE,
        **extra,
    }
    rendered = ", ".join(f"{k} {v!r}" if isinstance(v, str) else f"{k} {v}" for k, v in options.items())
    return f"ATTACH 'ga4' (TYPE vgi, LOCATION '{sys.executable} {ROOT / 'ga4_worker.py'}', {rendered})"


@pytest.fixture(scope="module")
def fake_url() -> Iterator[str]:
    with fake_ga.serve() as url:
        yield url


@pytest.fixture(scope="module")
def con(fake_url: str) -> Iterator[Any]:
    """A Haybarn connection with the worker attached to the fake GA4 and a secret set."""
    haybarn = pytest.importorskip("haybarn")
    connection = haybarn.connect()
    try:
        connection.execute(attach_sql(fake_url))
        connection.execute(f"CREATE SECRET my_ga (TYPE google_analytics, access_token '{fake_ga.TOKEN}')")
        yield connection
    finally:
        connection.close()


def rows(con: Any, sql: str) -> list[tuple[Any, ...]]:
    return list(con.execute(sql).fetchall())


def report_requests() -> list[dict[str, Any]]:
    return [body or {} for method, path, _q, body in fake_ga.REQUESTS if path.endswith(":runReport")]


@pytest.fixture(autouse=True)
def _clear_log() -> None:
    fake_ga.REQUESTS.clear()


class TestDiscovery:
    def test_account_summaries(self, con: Any) -> None:
        got = rows(con, "SELECT account_id, property_id FROM ga4.main.account_summaries ORDER BY ALL")
        assert got == [("5001", "1001"), ("5001", "1002"), ("5002", None)]

    def test_table_and_scan_function_agree(self, con: Any) -> None:
        assert rows(
            con,
            "SELECT (SELECT count(*) FROM ga4.main.account_summaries) "
            "= (SELECT count(*) FROM ga4.main.all_account_summaries())",
        ) == [(True,)]

    def test_accounts_and_properties(self, con: Any) -> None:
        assert rows(con, "SELECT count(*) FROM ga4.main.accounts") == [(2,)]
        assert rows(con, "SELECT property_id, time_zone FROM ga4.main.properties ORDER BY 1") == [
            ("1001", "America/New_York"),
            ("1002", "America/New_York"),
        ]

    def test_account_filter_lists_one_account(self, con: Any) -> None:
        assert rows(con, "SELECT count(*) FROM ga4.main.properties WHERE account_id = '5001'") == [(2,)]
        listed = [q.get("filter") for m, p, q, _ in fake_ga.REQUESTS if p.endswith("/properties")]
        assert listed == [["ancestor:accounts/5001"]] * len(listed)
        assert not any(p.endswith("/accounts") for _, p, _, _ in fake_ga.REQUESTS)

    def test_metadata_functions(self, con: Any) -> None:
        assert rows(con, "SELECT column_type FROM ga4.main.dimensions('0') WHERE api_name = 'date'") == [
            ("DATE",)
        ]
        assert rows(con, "SELECT column_type FROM ga4.main.metrics('') WHERE api_name = 'activeUsers'") == [
            ("BIGINT",)
        ]
        assert rows(con, "SELECT api_name FROM ga4.main.dimensions('1001') WHERE custom_definition") == [
            ("customEvent:plan_tier",)
        ]

    def test_compatibility_and_quota(self, con: Any) -> None:
        assert rows(
            con,
            "SELECT api_name, compatibility FROM ga4.main.check_compatibility('1001', "
            "dimensions => ['itemName'], metrics => ['sessions']) WHERE requested",
        ) == [("sessions", "INCOMPATIBLE")]
        assert rows(
            con, "SELECT remaining FROM ga4.main.property_quota('1001') WHERE quota = 'tokensPerHour'"
        ) == [(39_990,)]


class TestConfiguration:
    def test_one_property(self, con: Any) -> None:
        assert rows(con, "SELECT measurement_id FROM ga4.main.data_streams('properties/1001')") == [
            ("G-TEST1001",)
        ]

    def test_every_property(self, con: Any) -> None:
        got = rows(con, "SELECT property_id, api_name FROM ga4.main.custom_dimensions('') ORDER BY ALL")
        assert got == [
            ("1001", "customEvent:plan_tier"),
            ("1001", "customUser:signup_cohort"),
            ("1002", "customEvent:plan_tier"),
            ("1002", "customUser:signup_cohort"),
        ]

    def test_custom_metrics_and_key_events(self, con: Any) -> None:
        assert rows(con, "SELECT api_name FROM ga4.main.custom_metrics('1001')") == [("customEvent:credits",)]
        assert rows(con, "SELECT event_name, counting_method FROM ga4.main.key_events('1002')") == [
            ("purchase", "ONCE_PER_EVENT")
        ]

    def test_a_named_property_that_refuses_is_an_error(self, con: Any) -> None:
        with pytest.raises(Exception, match="Viewer role"):
            rows(con, "SELECT * FROM ga4.main.data_streams('9999')")


class TestReport:
    def test_columns_are_typed(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT typeof(date), typeof(dateHour), typeof(country), typeof(activeUsers), "
            "typeof(engagementRate) FROM ga4.main.report('1001', "
            "dimensions => ['date', 'dateHour', 'country'], metrics => ['activeUsers', 'engagementRate']) "
            "LIMIT 1",
        )
        assert got == [("DATE", "TIMESTAMP", "VARCHAR", "BIGINT", "DOUBLE")]

    def test_daily_totals(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT count(*), min(date), max(date), sum(sessions) FROM ga4.main.report('1001', "
            "dimensions => ['date'], metrics => ['sessions'], start_date => '2026-09-01', "
            "end_date => '2026-09-28')",
        )
        expected = sum(f["sessions"] for f in fake_ga.FACTS)
        assert got[0][:3] == (28, fake_ga.FACT_DATES[0], fake_ga.FACT_DATES[-1])
        assert got[0][3] == expected

    def test_pages_are_walked_in_order(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT count(*), count(DISTINCT dateHour) FROM ga4.main.report('1001', "
            "dimensions => ['dateHour'], metrics => ['activeUsers'], start_date => '2026-09-01')",
        )
        assert got[0][0] == got[0][1] > PAGE_SIZE * 3
        offsets = [b["offset"] for b in report_requests()]
        assert offsets == list(range(0, got[0][0] + 1, PAGE_SIZE))[: len(offsets)]

    def test_a_limit_stops_the_walk_early(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT * FROM ga4.main.report('1001', dimensions => ['dateHour'], metrics => ['activeUsers'], "
            "start_date => '2026-09-01') LIMIT 3",
        )
        assert len(got) == 3
        assert len(report_requests()) <= 2

    def test_max_rows(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT count(*) FROM ga4.main.report('1001', dimensions => ['dateHour'], "
            "metrics => ['activeUsers'], start_date => '2026-09-01', max_rows => 15)",
        )
        assert got == [(15,)]
        assert [b["limit"] for b in report_requests()] == [10, 5]

    def test_comparison(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT DISTINCT dateRange FROM ga4.main.report('1001', dimensions => ['country'], "
            "metrics => ['sessions'], start_date => '7daysAgo', compare_start_date => '14daysAgo', "
            "compare_end_date => '8daysAgo') ORDER BY 1",
        )
        assert got == [("comparison",), ("current",)]

    def test_hidden_dimension_filter(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT sum(eventCount) FROM ga4.main.report('1001', dimensions => ['country'], "
            "metrics => ['eventCount'], start_date => '2026-09-01', "
            'dimension_filter => \'{"filter": {"fieldName": "eventName", '
            '"stringFilter": {"value": "purchase"}}}\')',
        )
        assert got == [(sum(f["eventCount"] for f in fake_ga.FACTS if f["eventName"] == "purchase"),)]

    def test_quality_columns(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT DISTINCT subject_to_thresholding, sampling_ratio FROM ga4.main.report('1001', "
            "dimensions => ['country'], metrics => ['sessions'], quality_columns => true)",
        )
        assert got == [(True, 1.0)]

    def test_custom_dimension_column_needs_quotes(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT \"customEvent:plan_tier\" FROM ga4.main.report('1001', "
            "dimensions => ['customEvent:plan_tier'], metrics => ['sessions'])",
        )
        assert got == [("(not set)",)]

    @pytest.mark.parametrize(
        ("sql", "message"),
        [
            ("ga4.main.report('1001', dimensions => ['contry'], metrics => ['sessions'])", "Did you mean"),
            ("ga4.main.report('G-XYZ', metrics => ['sessions'])", "measurement id"),
            (
                "ga4.main.report('1001', metrics => ['sessions'], start_date => 'last week')",
                "does not match the required pattern",
            ),
            ("ga4.main.report('1001', metrics => ['sessions'], dimension_filter => '{')", "not valid JSON"),
            ("ga4.main.report('1001')", "at least one metric"),
        ],
    )
    def test_mistakes_fail_at_bind_with_a_reason(self, con: Any, sql: str, message: str) -> None:
        with pytest.raises(Exception, match=message):
            rows(con, f"SELECT * FROM {sql}")


class TestFiltersAreApplied:
    """With filter_pushdown declared, the engine drops its own filter — so the worker must apply it."""

    BASE = (
        "FROM ga4.main.report('1001', dimensions => ['country', 'deviceCategory'], "
        "metrics => ['activeUsers'], start_date => '2026-09-01')"
    )

    def test_equality_is_pushed_to_ga4(self, con: Any) -> None:
        got = rows(con, f"SELECT DISTINCT country {self.BASE} WHERE country = 'Germany'")
        assert got == [("Germany",)]
        [body] = report_requests()
        assert body["dimensionFilter"] == {
            "filter": {
                "fieldName": "country",
                "stringFilter": {"matchType": "EXACT", "value": "Germany", "caseSensitive": True},
            }
        }

    def test_in_is_pushed_as_an_in_list(self, con: Any) -> None:
        got = rows(
            con,
            f"SELECT DISTINCT country {self.BASE} WHERE country IN ('Germany', 'United Kingdom') ORDER BY 1",
        )
        assert got == [("Germany",), ("United Kingdom",)]
        assert "inListFilter" in report_requests()[0]["dimensionFilter"]["filter"]

    def test_unpushable_predicates_are_still_applied(self, con: Any) -> None:
        got = rows(con, f"SELECT DISTINCT country {self.BASE} WHERE country LIKE 'United%' ORDER BY 1")
        assert got == [("United Kingdom",), ("United States",)]

    @pytest.mark.parametrize(
        "predicate",
        [
            "country = 'Atlantis'",
            "activeUsers > 999999999",
            "country LIKE 'Z%'",
            "deviceCategory <> deviceCategory",
        ],
    )
    def test_an_impossible_predicate_returns_nothing(self, con: Any, predicate: str) -> None:
        assert rows(con, f"SELECT count(*) {self.BASE} WHERE {predicate}") == [(0,)]

    def test_a_metric_predicate_selects_matching_rows(self, con: Any) -> None:
        got = rows(con, f"SELECT activeUsers {self.BASE} WHERE activeUsers > 300")
        assert got
        assert all(value > 300 for (value,) in got)


class TestRealtime:
    def test_realtime(self, con: Any) -> None:
        got = rows(
            con,
            "SELECT minutesAgo, activeUsers FROM ga4.main.realtime_report('1001', "
            "dimensions => ['minutesAgo'], metrics => ['activeUsers']) ORDER BY 1",
        )
        assert got == [(0, 10), (1, 11), (2, 12)]


class TestCredentials:
    def test_no_secret_is_a_clear_error(self, con: Any) -> None:
        con.execute("DROP SECRET my_ga")
        try:
            with pytest.raises(Exception, match="CREATE SECRET"):
                rows(con, "SELECT * FROM ga4.main.accounts")
        finally:
            con.execute(f"CREATE SECRET my_ga (TYPE google_analytics, access_token '{fake_ga.TOKEN}')")

    def test_a_wrong_token_is_rejected_with_context(self, con: Any) -> None:
        con.execute("CREATE OR REPLACE SECRET my_ga (TYPE google_analytics, access_token 'wrong')")
        try:
            with pytest.raises(Exception, match="401"):
                rows(con, "SELECT * FROM ga4.main.accounts")
        finally:
            con.execute(
                f"CREATE OR REPLACE SECRET my_ga (TYPE google_analytics, access_token '{fake_ga.TOKEN}')"
            )

    def test_secret_values_are_redacted(self, con: Any) -> None:
        [(secret,)] = rows(con, "SELECT secret_string FROM duckdb_secrets() WHERE name = 'my_ga'")
        assert fake_ga.TOKEN not in secret


class TestAttachOptions:
    @pytest.mark.parametrize(
        ("option", "message"),
        [
            ("credentials 'required'", "not one of auto, secret"),
            ("report_page_size 0", "between 1 and 250000"),
            ("data_api_url 'file:///etc/passwd'", "http"),
        ],
    )
    def test_a_bad_option_fails_the_attach(self, fake_url: str, option: str, message: str) -> None:
        haybarn = pytest.importorskip("haybarn")
        connection = haybarn.connect()
        try:
            with pytest.raises(Exception, match=message):
                connection.execute(
                    f"ATTACH 'ga4' (TYPE vgi, LOCATION '{sys.executable} {ROOT / 'ga4_worker.py'}', {option})"
                )
        finally:
            connection.close()

    def test_page_size_option_controls_paging(self, con: Any) -> None:
        rows(
            con,
            "SELECT count(*) FROM ga4.main.report('1001', dimensions => ['dateHour'], "
            "metrics => ['activeUsers'], start_date => '2026-09-01')",
        )
        assert {b["limit"] for b in report_requests()} == {PAGE_SIZE}
