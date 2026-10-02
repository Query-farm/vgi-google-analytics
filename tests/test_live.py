"""Live tests against real Google Analytics, through a real ATTACH.

Opt in with ``pytest -m live``. They need a credential and a property to read:

* ``VGI_GA4_TEST_SERVICE_ACCOUNT_JSON`` — a service-account key (the JSON text),
  **or** ``VGI_GA4_TEST_ACCESS_TOKEN`` (e.g. from
  ``gcloud auth application-default print-access-token``), **or** neither, in
  which case the worker's Application Default Credentials are used;
* ``VGI_GA4_TEST_PROPERTY`` — a numeric GA4 property id that credential can read.
  When unset, the first property in ``account_summaries`` is used.

Every assertion holds for any property with any traffic — none depend on
volume — so the suite can run against a quiet test property.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parent.parent


def _escape(value: str) -> str:
    return value.replace("'", "''")


@pytest.fixture(scope="module")
def con() -> Iterator[Any]:
    """A Haybarn connection with the worker attached and the test credential configured."""
    haybarn = pytest.importorskip("haybarn")
    service_account = os.environ.get("VGI_GA4_TEST_SERVICE_ACCOUNT_JSON", "")
    token = os.environ.get("VGI_GA4_TEST_ACCESS_TOKEN", "")
    # With a test credential configured, insist on it: falling back to the
    # developer's own ADC would test a different identity than CI uses.
    mode = "secret" if (service_account or token) else "auto"
    connection = haybarn.connect()
    try:
        location = f"{sys.executable} {ROOT / 'ga4_worker.py'}"
        connection.execute(f"ATTACH 'ga4' (TYPE vgi, LOCATION '{location}', credentials '{mode}')")
        if service_account:
            connection.execute(
                f"CREATE SECRET ga (TYPE google_analytics, service_account_json '{_escape(service_account)}')"
            )
        elif token:
            connection.execute(f"CREATE SECRET ga (TYPE google_analytics, access_token '{_escape(token)}')")
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="module")
def prop(con: Any) -> str:
    explicit = os.environ.get("VGI_GA4_TEST_PROPERTY", "").strip()
    if explicit:
        return explicit
    row = con.execute(
        "SELECT property_id FROM ga4.main.account_summaries WHERE property_id IS NOT NULL "
        "ORDER BY property_id LIMIT 1"
    ).fetchone()
    if row is None:
        pytest.skip("the credential can read no GA4 property")
    return str(row[0])


def test_account_summaries_lists_the_property(con: Any, prop: str) -> None:
    ids = {r[0] for r in con.execute("SELECT property_id FROM ga4.main.account_summaries").fetchall()}
    assert prop in ids


def test_properties_table(con: Any, prop: str) -> None:
    row = con.execute(
        "SELECT time_zone, currency_code FROM ga4.main.properties WHERE property_id = ?", [prop]
    ).fetchone()
    assert row is not None
    assert row[0]


def test_accounts_table(con: Any) -> None:
    assert con.execute("SELECT count(*) FROM ga4.main.accounts").fetchone()[0] >= 1


def test_universal_metadata(con: Any) -> None:
    dims = dict(con.execute("SELECT api_name, column_type FROM ga4.main.dimensions('0')").fetchall())
    mets = dict(con.execute("SELECT api_name, column_type FROM ga4.main.metrics('0')").fetchall())
    assert dims["date"] == "DATE"
    assert dims["dateHour"] == "TIMESTAMP"
    assert mets["activeUsers"] == "BIGINT"
    assert mets["engagementRate"] == "DOUBLE"
    assert len(dims) > 100
    assert len(mets) > 50


def test_property_metadata(con: Any, prop: str) -> None:
    assert con.execute(f"SELECT count(*) FROM ga4.main.metrics('{prop}')").fetchone()[0] > 50


def test_configuration_functions_run(con: Any, prop: str) -> None:
    for fn in ("data_streams", "custom_dimensions", "custom_metrics", "key_events"):
        con.execute(f"SELECT * FROM ga4.main.{fn}('{prop}')").fetchall()


def test_daily_report_is_typed(con: Any, prop: str) -> None:
    rows = con.execute(
        "SELECT date, activeUsers, sessions, engagementRate FROM ga4.main.report("
        f"'{prop}', dimensions => ['date'], metrics => ['activeUsers', 'sessions', 'engagementRate'], "
        "start_date => '7daysAgo', end_date => 'yesterday', keep_empty_rows => true) ORDER BY date"
    ).fetchall()
    assert len(rows) <= 8
    kinds = con.execute(
        "SELECT typeof(date), typeof(activeUsers), typeof(engagementRate) FROM ga4.main.report("
        f"'{prop}', dimensions => ['date'], metrics => ['activeUsers', 'engagementRate'], "
        "keep_empty_rows => true) LIMIT 1"
    ).fetchall()
    assert kinds in ([], [("DATE", "BIGINT", "DOUBLE")])


def test_report_filter_pushdown_is_correct(con: Any, prop: str) -> None:
    every = con.execute(
        f"SELECT deviceCategory, sessions FROM ga4.main.report('{prop}', dimensions => ['deviceCategory'], "
        "metrics => ['sessions'])"
    ).fetchall()
    if not every:
        pytest.skip("no sessions in the last 28 days")
    device, sessions = every[0]
    filtered = con.execute(
        f"SELECT deviceCategory, sessions FROM ga4.main.report('{prop}', dimensions => ['deviceCategory'], "
        "metrics => ['sessions']) WHERE deviceCategory = ?",
        [device],
    ).fetchall()
    assert filtered == [(device, sessions)]


def test_comparison_and_quality(con: Any, prop: str) -> None:
    rows = con.execute(
        f"SELECT dateRange, sessions, sampling_ratio FROM ga4.main.report('{prop}', metrics => ['sessions'], "
        "start_date => '7daysAgo', compare_start_date => '14daysAgo', compare_end_date => '8daysAgo', "
        "quality_columns => true)"
    ).fetchall()
    assert {r[0] for r in rows} <= {"current", "comparison"}


def test_a_bad_name_fails_at_bind(con: Any, prop: str) -> None:
    with pytest.raises(Exception, match="Did you mean"):
        con.execute(f"SELECT * FROM ga4.main.report('{prop}', metrics => ['activeUser'])").fetchall()


def test_compatibility(con: Any, prop: str) -> None:
    rows = con.execute(
        f"SELECT compatibility FROM ga4.main.check_compatibility('{prop}', dimensions => ['country'], "
        "metrics => ['activeUsers']) WHERE requested"
    ).fetchall()
    assert rows == [("COMPATIBLE",), ("COMPATIBLE",)]


def test_quota(con: Any, prop: str) -> None:
    quotas = dict(con.execute(f"SELECT quota, remaining FROM ga4.main.property_quota('{prop}')").fetchall())
    assert quotas["tokensPerHour"] > 0


def test_realtime(con: Any, prop: str) -> None:
    rows = con.execute(
        f"SELECT minutesAgo, activeUsers FROM ga4.main.realtime_report('{prop}', "
        "dimensions => ['minutesAgo'], metrics => ['activeUsers'])"
    ).fetchall()
    assert all(0 <= m <= 59 and u >= 0 for m, u in rows)
