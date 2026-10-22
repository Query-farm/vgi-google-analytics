"""Pure transformations: typing, request building, response flattening, coercion."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pyarrow as pa
import pytest

from tests import fake_ga
from vgi_google_analytics import admin, discovery, schemas, types
from vgi_google_analytics.reports import (
    RealtimeArgs,
    ReportArgs,
    build_realtime_request,
    build_report_request,
    check_date,
    parse_filter,
    report_rows,
)

CATALOGUE = types.Catalogue.from_metadata(fake_ga.metadata("1001"))


class TestReportSchema:
    def test_columns_are_typed_from_metadata(self) -> None:
        schema = types.report_schema(
            ["date", "dateHour", "country"], ["activeUsers", "engagementRate", "totalRevenue"], CATALOGUE
        )
        assert [(f.name, str(f.type)) for f in schema] == [
            ("date", "date32[day]"),
            ("dateHour", "timestamp[us]"),
            ("country", "string"),
            ("activeUsers", "int64"),
            ("engagementRate", "double"),
            ("totalRevenue", "double"),
        ]

    def test_every_column_is_documented(self) -> None:
        schema = types.report_schema(["country"], ["sessions"], CATALOGUE, quality_columns=True)
        assert all(f.metadata and f.metadata.get(b"comment") for f in schema)

    def test_custom_dimension_keeps_its_api_name(self) -> None:
        schema = types.report_schema(["customEvent:plan_tier"], ["sessions"], CATALOGUE)
        assert schema.names == ["customEvent:plan_tier", "sessions"]

    def test_comparison_and_quality_columns(self) -> None:
        schema = types.report_schema(
            ["country"], ["sessions"], CATALOGUE, date_range_column=True, quality_columns=True
        )
        assert schema.names == [
            "country",
            "dateRange",
            "sessions",
            "subject_to_thresholding",
            "data_loss_from_other_row",
            "sampling_ratio",
        ]

    def test_typos_get_suggestions(self) -> None:
        with pytest.raises(ValueError, match="Did you mean 'country'"):
            types.report_schema(["contry"], ["sessions"], CATALOGUE)

    def test_a_metric_passed_as_a_dimension(self) -> None:
        with pytest.raises(ValueError, match="is a metric, not a dimension"):
            types.report_schema(["sessions"], [], CATALOGUE)

    def test_a_dimension_passed_as_a_metric(self) -> None:
        with pytest.raises(ValueError, match="is a dimension, not a metric"):
            types.report_schema([], ["country"], CATALOGUE)

    def test_duplicates_and_empties(self) -> None:
        with pytest.raises(ValueError, match="requested twice"):
            types.report_schema(["country", "country"], [], CATALOGUE)
        with pytest.raises(ValueError, match="at least one"):
            types.report_schema([], [], CATALOGUE)

    def test_deprecated_aliases_resolve(self) -> None:
        payload = fake_ga.metadata("0")
        payload["metrics"][0]["deprecatedApiNames"] = ["oldUsers"]
        catalogue = types.Catalogue.from_metadata(payload)
        assert types.report_schema([], ["oldUsers"], catalogue).field("oldUsers").type == pa.int64()

    def test_realtime_schema(self) -> None:
        schema = types.realtime_schema(["minutesAgo", "country"], ["activeUsers", "mystery"])
        assert [str(f.type) for f in schema] == ["int64", "string", "int64", "double"]


class TestRequests:
    def test_defaults(self) -> None:
        body = build_report_request(ReportArgs(property="1", metrics=["sessions"]), [])
        assert body == {
            "dateRanges": [{"startDate": "28daysAgo", "endDate": "yesterday"}],
            "dimensions": [],
            "metrics": [{"name": "sessions"}],
            "keepEmptyRows": False,
        }

    def test_comparison_names_both_ranges(self) -> None:
        args = ReportArgs(
            property="1",
            metrics=["sessions"],
            compare_start_date="2026-01-01",
            compare_end_date="2026-01-31",
        )
        ranges = build_report_request(args, [])["dateRanges"]
        assert [r["name"] for r in ranges] == ["current", "comparison"]

    def test_comparison_needs_both_ends(self) -> None:
        with pytest.raises(ValueError, match="together"):
            build_report_request(ReportArgs(property="1", compare_start_date="7daysAgo"), [])

    def test_user_and_pushed_filters_are_anded(self) -> None:
        user = {"filter": {"fieldName": "eventName", "stringFilter": {"value": "purchase"}}}
        pushed = [{"filter": {"fieldName": "country", "inListFilter": {"values": ["A", "B"]}}}]
        body = build_report_request(
            ReportArgs(property="1", metrics=["eventCount"], dimension_filter=json.dumps(user)), pushed
        )
        assert body["dimensionFilter"] == {"andGroup": {"expressions": [user, *pushed]}}

    def test_metric_filter_and_currency(self) -> None:
        metric = {"filter": {"fieldName": "sessions", "numericFilter": {"operation": "GREATER_THAN"}}}
        body = build_report_request(
            ReportArgs(
                property="1", metrics=["sessions"], metric_filter=json.dumps(metric), currency_code="eur"
            ),
            [],
        )
        assert body["metricFilter"] == metric
        assert body["currencyCode"] == "EUR"

    @pytest.mark.parametrize("good", ["2026-01-31", "today", "yesterday", "0daysAgo", "365daysAgo"])
    def test_good_dates(self, good: str) -> None:
        assert check_date("start_date", good) == good

    @pytest.mark.parametrize("bad", ["last week", "2026/01/31", "7 days ago", "", "-1daysAgo"])
    def test_bad_dates(self, bad: str) -> None:
        with pytest.raises(ValueError, match="not a GA4 date"):
            check_date("start_date", bad)

    def test_filter_json_must_be_an_object(self) -> None:
        assert parse_filter("f", "  ") is None
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_filter("f", "{")
        with pytest.raises(ValueError, match="JSON object"):
            parse_filter("f", "[]")

    def test_realtime_window(self) -> None:
        body = build_realtime_request(RealtimeArgs(property="1", metrics=["activeUsers"]), [])
        assert body["minuteRanges"] == [{"startMinutesAgo": 29, "endMinutesAgo": 0}]
        assert body["limit"] == 250_000
        with pytest.raises(ValueError, match="minutes_ago_end"):
            build_realtime_request(
                RealtimeArgs(property="1", metrics=["activeUsers"], minutes_ago_start=1, minutes_ago_end=5),
                [],
            )


class TestReportRows:
    def test_values_map_to_requested_names_by_position(self) -> None:
        response = {
            "dimensionHeaders": [{"name": "newName"}],
            "rows": [{"dimensionValues": [{"value": "x"}], "metricValues": [{"value": "3"}]}],
        }
        assert report_rows(response, ["oldName"], ["m"])[0] | {} == {
            "oldName": "x",
            "m": "3",
            "subject_to_thresholding": False,
            "data_loss_from_other_row": False,
            "sampling_ratio": 1.0,
        }

    def test_appended_date_range_dimension(self) -> None:
        response = {
            "dimensionHeaders": [{"name": "country"}, {"name": "dateRange"}],
            "rows": [{"dimensionValues": [{"value": "US"}, {"value": "current"}], "metricValues": []}],
        }
        assert report_rows(response, ["country"], [])[0]["dateRange"] == "current"

    def test_quality_metadata(self) -> None:
        response = {
            "metadata": {
                "subjectToThresholding": True,
                "dataLossFromOtherRow": True,
                "samplingMetadatas": [
                    {"samplesReadCount": "50", "samplingSpaceSize": "100"},
                    {"samplesReadCount": "25", "samplingSpaceSize": "100"},
                ],
            },
            "rows": [{"dimensionValues": [], "metricValues": [{"value": "1"}]}],
        }
        row = report_rows(response, [], ["m"])[0]
        assert row["subject_to_thresholding"] is True
        assert row["data_loss_from_other_row"] is True
        assert row["sampling_ratio"] == 0.25

    def test_empty_response(self) -> None:
        assert report_rows({}, ["a"], ["b"]) == []


class TestCoercion:
    def test_report_dates(self) -> None:
        assert schemas.to_report_date("20260131") == date(2026, 1, 31)
        assert schemas.to_report_date("(other)") is None
        assert schemas.to_report_date("20261341") is None

    def test_report_timestamps(self) -> None:
        assert schemas.to_report_timestamp("2026013113") == datetime(2026, 1, 31, 13)
        assert schemas.to_report_timestamp("202601311345") == datetime(2026, 1, 31, 13, 45)
        assert schemas.to_report_timestamp("(not set)") is None

    def test_ints(self) -> None:
        assert schemas.to_int("12") == 12
        assert schemas.to_int("12.0") == 12
        assert schemas.to_int("12.5") is None
        assert schemas.to_int(True) is None
        assert schemas.to_int(str(2**70)) is None

    def test_floats(self) -> None:
        assert schemas.to_float("0.25") == 0.25
        assert schemas.to_float("NaN") is None
        assert schemas.to_float("inf") is None

    def test_timestamps(self) -> None:
        assert schemas.to_timestamp("2024-01-02T03:04:05.123Z") == datetime(
            2024, 1, 2, 3, 4, 5, 123000, tzinfo=UTC
        )
        assert schemas.to_timestamp("0001-01-01T00:00:00Z") is None
        assert schemas.to_timestamp("garbage") is None

    def test_a_bad_value_becomes_null_not_an_error(self) -> None:
        schema = pa.schema([pa.field("n", pa.int64()), pa.field("d", pa.date32())])
        batch = schemas.batch_from_rows([{"n": "x", "d": "(other)"}, {"n": "2", "d": "20260101"}], schema)
        assert batch.to_pylist() == [{"n": None, "d": None}, {"n": 2, "d": date(2026, 1, 1)}]


class TestFlatteners:
    def test_account_summary_fans_out_per_property(self) -> None:
        rows = admin.flatten_account_summary(
            {
                "account": "accounts/1",
                "displayName": "A",
                "propertySummaries": [{"property": "properties/2"}, {"property": "properties/3"}],
            }
        )
        assert [r["property_id"] for r in rows] == ["2", "3"]
        assert admin.flatten_account_summary({"account": "accounts/9"}) == [
            {"account_id": "9", "account_name": None}
        ]

    @pytest.mark.parametrize(
        ("scope", "expected"),
        [("EVENT", "customEvent:p"), ("USER", "customUser:p"), ("ITEM", "customItem:p"), ("X", None)],
    )
    def test_custom_api_names(self, scope: str, expected: str | None) -> None:
        assert admin.custom_api_name(scope, "p") == expected

    def test_data_stream_merges_platform_blocks(self) -> None:
        [row] = admin.flatten_data_stream(
            {
                "name": "properties/1/dataStreams/5",
                "androidAppStreamData": {"firebaseAppId": "f", "packageName": "p"},
            }
        )
        assert (row["stream_id"], row["firebase_app_id"], row["package_name"]) == ("5", "f", "p")

    def test_key_event_default_value(self) -> None:
        [row] = admin.flatten_key_event(
            {"eventName": "e", "defaultValue": {"numericValue": 4.5, "currencyCode": "USD"}}
        )
        assert (row["default_value"], row["default_currency_code"]) == (4.5, "USD")

    def test_metadata_rows_carry_column_types(self) -> None:
        dims = {
            r["api_name"]: r["column_type"] for r in discovery.flatten_dimensions("0", fake_ga.metadata("0"))
        }
        mets = {
            r["api_name"]: r["column_type"] for r in discovery.flatten_metrics("0", fake_ga.metadata("0"))
        }
        assert dims["date"] == "DATE"
        assert dims["dateHour"] == "TIMESTAMP"
        assert dims["country"] == "VARCHAR"
        assert mets["activeUsers"] == "BIGINT"
        assert mets["totalRevenue"] == "DOUBLE"

    def test_compatibility_flags_requested(self) -> None:
        payload = {
            "dimensionCompatibilities": [
                {"dimensionMetadata": {"apiName": "a"}, "compatibility": "COMPATIBLE"}
            ],
            "metricCompatibilities": [{"metricMetadata": {"apiName": "m"}, "compatibility": "INCOMPATIBLE"}],
        }
        rows = discovery.flatten_compatibility("1", payload, ["a"], [])
        assert [(r["kind"], r["requested"]) for r in rows] == [("dimension", True), ("metric", False)]
        assert {r["property_id"] for r in rows} == {"1"}

    def test_quota_rows(self) -> None:
        rows = discovery.flatten_quota(
            "1", {"propertyQuota": {"tokensPerHour": {"consumed": 2, "remaining": 8}}}
        )
        assert rows[0]["quota"] == "tokensPerHour"
        assert rows[0]["remaining"] == 8
        assert rows[0]["description"]
