"""ATTACH options: validation, encoding, and the ADC safety rules."""

from __future__ import annotations

from typing import Any

import pytest

from vgi_google_analytics import settings
from vgi_google_analytics.settings import Settings


class TestFromOptions:
    def test_defaults(self) -> None:
        assert Settings.from_options({}) == Settings()

    def test_every_option(self) -> None:
        got = Settings.from_options(
            {
                "credentials": "SECRET",
                "report_page_size": 500,
                "data_api_url": "http://localhost:9/data/",
                "admin_api_url": "https://proxy.example/admin",
            }
        )
        assert got == Settings(
            credentials="secret",
            report_page_size=500,
            data_api_url="http://localhost:9/data",
            admin_api_url="https://proxy.example/admin",
        )

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"credentials": "require"}, "not one of auto, secret"),
            ({"report_page_size": 0}, "between 1 and 250000"),
            ({"report_page_size": 250_001}, "between 1 and 250000"),
            ({"data_api_url": "ftp://x/y"}, "http"),
            ({"admin_api_url": "https://x/y?key=1"}, "without a query"),
            ({"data_api_url": "not a url"}, "http"),
        ],
    )
    def test_mistakes_are_rejected(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            Settings.from_options(options)

    def test_every_option_is_advertised(self) -> None:
        assert {spec.name for spec in settings.OPTION_SPECS} == {
            "credentials",
            "report_page_size",
            "data_api_url",
            "admin_api_url",
        }


class TestEncoding:
    def test_round_trip(self) -> None:
        original = Settings(credentials="secret", report_page_size=7, data_api_url="http://a/b")
        assert Settings.decode(original.encode()) == original

    @pytest.mark.parametrize("data", [None, b"", b"\xff", b"[]", b'{"report_page_size": -3}'])
    def test_anything_unreadable_decodes_to_the_safe_defaults(self, data: bytes | None) -> None:
        assert Settings.decode(data) == Settings()


class TestAdcRules:
    def test_auto_with_google_endpoints_may_use_adc(self) -> None:
        assert Settings().may_use_adc

    def test_secret_mode_never_uses_adc(self) -> None:
        assert not Settings(credentials="secret").may_use_adc

    @pytest.mark.parametrize("field", ["data_api_url", "admin_api_url"])
    def test_a_custom_endpoint_disables_adc(self, field: str) -> None:
        """Otherwise a client could aim the worker at its own host and collect the worker's token."""
        custom = Settings.from_options({field: "https://attacker.example/v1beta"})
        assert custom.custom_endpoints
        assert not custom.may_use_adc
