"""The HTTP chokepoint: identifiers, allow-list, retries, errors, paging."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests import fake_ga
from vgi_google_analytics import auth, ga_api
from vgi_google_analytics.settings import Settings


class TestIdentifiers:
    @pytest.mark.parametrize(("raw", "expected"), [("123", "123"), ("properties/456", "456"), (" 7 ", "7")])
    def test_property_ids_normalise(self, raw: str, expected: str) -> None:
        assert ga_api.property_id(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            ("G-ABC123", "measurement id"),
            ("UA-1234-1", "Universal Analytics"),
            ("../../accounts", "not a GA4 property id"),
            ("123/dataStreams", "not a GA4 property id"),
            ("", "not a GA4 property id"),
        ],
    )
    def test_bad_property_ids_explain_themselves(self, raw: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            ga_api.property_id(raw)

    def test_account_ids(self) -> None:
        assert ga_api.account_id("accounts/99") == "99"
        with pytest.raises(ValueError, match="not a Google Analytics account id"):
            ga_api.account_id("99; DROP")

    def test_trailing_id(self) -> None:
        assert ga_api.trailing_id("properties/1/dataStreams/2") == "2"
        assert ga_api.trailing_id(None) is None


SETTINGS = Settings(data_api_url="https://data.example/v1beta", admin_api_url="https://admin.example/v1beta")


class TestAllowList:
    @pytest.mark.parametrize(
        ("method", "url"),
        [
            ("GET", "https://admin.example/v1beta/accountSummaries"),
            ("GET", "https://data.example/v1beta/properties/1/metadata"),
            ("POST", "https://data.example/v1beta/properties/1:runReport"),
            ("POST", "https://data.example/v1beta/properties/1:runRealtimeReport"),
            ("POST", "https://data.example/v1beta/properties/1:checkCompatibility"),
        ],
    )
    def test_reads_are_allowed(self, method: str, url: str) -> None:
        assert ga_api._allowed(method, url, SETTINGS)

    @pytest.mark.parametrize(
        ("method", "url"),
        [
            ("POST", "https://data.example/v1beta/properties/1/audienceExports"),
            ("POST", "https://admin.example/v1beta/properties/1:runAccessReport"),
            ("POST", "https://admin.example/v1beta/accounts/1:searchChangeHistoryEvents"),
            ("PATCH", "https://admin.example/v1beta/properties/1"),
            ("DELETE", "https://admin.example/v1beta/properties/1/customDimensions/2"),
            ("POST", "https://data.example/v1beta/properties/1:runReportX"),
            ("GET", "https://evil.example/v1beta/accountSummaries"),
        ],
    )
    def test_everything_else_is_refused(self, method: str, url: str) -> None:
        assert not ga_api._allowed(method, url, SETTINGS)

    def test_the_chokepoint_refuses_before_any_request(self) -> None:
        sess = ga_api.Session(credentials=auth.load({auth.ACCESS_TOKEN: "t"}), settings=SETTINGS)
        ga_api.use_transport(httpx.MockTransport(lambda _r: pytest.fail("a request was sent")))
        with pytest.raises(PermissionError):
            ga_api._call("POST", "https://admin.example/v1beta/properties/1:archive", sess)

    def test_a_lookalike_host_is_not_a_prefix_match(self) -> None:
        assert not ga_api._allowed("GET", "https://data.example/v1beta-evil/x", SETTINGS)


class TestCalls:
    def test_admin_paging_follows_tokens(self, fake: list, sess: ga_api.Session) -> None:
        items, token = ga_api.admin_page("/accounts", "accounts", sess)
        assert len(items) == 1
        assert token == "1"
        items, token = ga_api.admin_page("/accounts", "accounts", sess, page_token=token)
        assert len(items) == 1
        assert token is None
        assert fake[0][2]["pageSize"] == [str(ga_api.ADMIN_PAGE_SIZE)]

    def test_metadata_is_cached(self, fake: list, sess: ga_api.Session) -> None:
        ga_api.metadata("1001", sess)
        ga_api.metadata("properties/1001", sess)
        assert len(fake) == 1

    def test_bearer_token_is_sent(self, fake: list, sess: ga_api.Session) -> None:
        wrong = ga_api.Session(credentials=auth.load({auth.ACCESS_TOKEN: "nope"}), settings=sess.settings)
        with pytest.raises(ga_api.GoogleAnalyticsError) as info:
            ga_api.metadata("1001", wrong)
        assert info.value.status == 401
        assert "expired or revoked" in str(info.value)

    def test_permission_errors_say_how_to_grant_access(self, fake: list, sess: ga_api.Session) -> None:
        with pytest.raises(ga_api.GoogleAnalyticsError, match="Viewer role") as info:
            ga_api.run_report("9999", {"metrics": [{"name": "activeUsers"}]}, sess)
        assert info.value.reason == "PERMISSION_DENIED"

    def test_invalid_argument_passes_googles_message_through(self, fake: list, sess: ga_api.Session) -> None:
        with pytest.raises(ga_api.GoogleAnalyticsError, match="not a valid metric"):
            ga_api.run_report("1001", {"metrics": [{"name": "nope"}]}, sess)


def _scripted(responses: list[httpx.Response]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    return httpx.MockTransport(handler), seen


class TestRetries:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ga_api.time, "sleep", lambda _s: None)

    def test_transient_errors_are_retried(self, sess: ga_api.Session) -> None:
        transport, seen = _scripted(
            [httpx.Response(503, json={}), httpx.Response(429, json={}), httpx.Response(200, json={"ok": 1})]
        )
        ga_api.use_transport(transport)
        assert ga_api.run_report("1", {}, sess) == {"ok": 1}
        assert len(seen) == 3

    def test_retries_are_bounded(self, sess: ga_api.Session) -> None:
        error = {
            "error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Exhausted property tokens"}
        }
        transport, seen = _scripted([httpx.Response(429, json=error) for _ in range(10)])
        ga_api.use_transport(transport)
        with pytest.raises(ga_api.GoogleAnalyticsError, match="property_quota") as info:
            ga_api.run_report("1", {}, sess)
        assert info.value.status == 429
        assert len(seen) == ga_api._RETRY_ATTEMPTS

    def test_client_errors_are_not_retried(self, sess: ga_api.Session) -> None:
        transport, seen = _scripted([httpx.Response(400, json={"error": {"message": "bad"}})])
        ga_api.use_transport(transport)
        with pytest.raises(ga_api.GoogleAnalyticsError):
            ga_api.run_report("1", {}, sess)
        assert len(seen) == 1

    def test_retry_after_is_honoured(self) -> None:
        assert ga_api._retry_delay(httpx.Response(429, headers={"retry-after": "3"}), 0) == 3.0
        assert ga_api._retry_delay(httpx.Response(429, headers={"retry-after": "9999"}), 0) == 30.0

    def test_disabled_api_points_at_the_console(self, sess: ga_api.Session) -> None:
        payload: dict[str, Any] = {
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": "Google Analytics Data API has not been used in project 1 before.",
                "details": [{"reason": "SERVICE_DISABLED"}],
            }
        }
        transport, _ = _scripted([httpx.Response(403, json=payload)])
        ga_api.use_transport(transport)
        with pytest.raises(ga_api.GoogleAnalyticsError, match="Enable both") as info:
            ga_api.run_report("1", {}, sess)
        assert info.value.reason == "SERVICE_DISABLED"

    def test_non_json_success_is_an_error_not_a_crash(self, sess: ga_api.Session) -> None:
        transport, _ = _scripted([httpx.Response(200, text="<html>proxy</html>")])
        ga_api.use_transport(transport)
        with pytest.raises(ga_api.GoogleAnalyticsError, match="non-JSON"):
            ga_api.run_report("1", {}, sess)


class TestAuthTransport:
    def test_token_requests_go_through_the_shared_pool(self) -> None:
        transport, seen = _scripted([httpx.Response(200, json={"access_token": "x"})])
        ga_api.use_transport(transport)
        response = ga_api.AuthTransport()("https://oauth2.googleapis.com/token", method="POST", body=b"a=b")
        assert response.status == 200
        assert seen[0].content == b"a=b"


def test_fake_tracks_requests(fake: list, sess: ga_api.Session) -> None:
    """Guard the guard: the fixture really routes through the fake."""
    ga_api.metadata("0", sess)
    assert fake[-1][1] == "/data/v1beta/properties/0/metadata"
    assert fake_ga.TOKEN
