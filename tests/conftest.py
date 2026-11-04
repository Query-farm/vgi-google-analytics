"""Shared fixtures: hermetic process state, and the in-process fake GA4.

The worker keeps four pieces of process-wide state — the HTTP connection
pool, the credential cache, the OAuth credential cache and the metadata cache.
Each is reset around every test so the order tests run in cannot decide whether
they pass. Browser sign-in starts disabled, as it is on a shared transport.
"""

from __future__ import annotations

from collections.abc import Generator

import haybarn
import httpx
import pytest

from tests import fake_ga
from vgi_google_analytics import auth, ga_api, oauth
from vgi_google_analytics.settings import Settings

FAKE_BASE = "http://fake.test"


@pytest.fixture(scope="session", autouse=True)
def _install_vgi_extension() -> None:
    """Provision the SQL client's extension once, including on a fresh CI runner."""
    with haybarn.connect() as connection:
        connection.execute("INSTALL vgi FROM community")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Reset process-wide caches, and never fall back to the developer's real ADC."""
    auth.permit_adc(False)
    oauth.clear_cache()
    oauth.permit_local(False)
    ga_api.use_transport(None)
    auth.reset_cache()
    ga_api.clear_metadata_cache()
    yield
    oauth.clear_cache()
    ga_api.use_transport(None)
    auth.reset_cache()
    ga_api.clear_metadata_cache()
    auth.permit_adc(True)
    oauth.permit_local(False)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> list:
    """Route the worker's HTTP through the in-process fake; returns its request log."""
    monkeypatch.setattr(ga_api, "_RETRY_BASE_SECONDS", 0.0)
    fake_ga.REQUESTS.clear()
    ga_api.use_transport(httpx.MockTransport(fake_ga.handle))
    return fake_ga.REQUESTS


#: Settings pointing both APIs at the fake, as the equivalent ATTACH options would.
FAKE_SETTINGS = Settings(data_api_url=f"{FAKE_BASE}/data/v1beta", admin_api_url=f"{FAKE_BASE}/admin/v1beta")


@pytest.fixture
def sess() -> ga_api.Session:
    """A session with the token the fake accepts, aimed at the fake."""
    return ga_api.Session(credentials=auth.load({auth.ACCESS_TOKEN: fake_ga.TOKEN}), settings=FAKE_SETTINGS)
