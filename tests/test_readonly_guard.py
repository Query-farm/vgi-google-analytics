"""Structural guard: this worker must never be able to change a GA4 account.

The Admin API that lists properties is the same API that deletes them. The
worker is read-only by construction — one chokepoint with an allow-list, and a
read-only OAuth scope — and these tests fail the build if either stops being
true.
"""

from __future__ import annotations

import ast
from pathlib import Path

from vgi_google_analytics import auth, ga_api

PACKAGE = Path(__file__).resolve().parent.parent / "vgi_google_analytics"

#: httpx calls that would issue a request outside the chokepoint.
_HTTP_CALLS = {"get", "post", "put", "patch", "delete", "stream", "send"}


def _sources() -> dict[str, str]:
    return {path.name: path.read_text() for path in PACKAGE.rglob("*.py")}


class TestChokepoint:
    def test_only_ga_api_touches_httpx(self) -> None:
        users = sorted(name for name, text in _sources().items() if "httpx" in text)
        assert users == ["ga_api.py"], users

    def test_no_module_imports_another_http_client(self) -> None:
        for name, text in _sources().items():
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import | ast.ImportFrom):
                    modules = (
                        [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    )
                    for module in modules:
                        assert not module.startswith(("requests", "urllib3", "aiohttp", "http.client")), (
                            f"{name} imports {module}"
                        )
                        assert module != "google.auth.transport.requests", (
                            f"{name} uses the requests transport"
                        )

    def test_ga_api_issues_requests_only_from_the_chokepoints(self) -> None:
        """Every HTTP call in ga_api lives in `_call` or the token transport.

        An HTTP call is a verb on ``httpx`` itself, on a name containing
        ``client``, or on ``shared_client()`` — which excludes ``dict.get``.
        """
        tree = ast.parse((PACKAGE / "ga_api.py").read_text())

        def is_client(node: ast.expr) -> bool:
            if isinstance(node, ast.Name):
                return node.id == "httpx" or "client" in node.id
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "shared_client"
            )

        offenders = []
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _HTTP_CALLS | {"request"}
                    and is_client(node.func.value)
                ):
                    offenders.append((func.name, node.func.attr))
        assert offenders, "the guard found no HTTP calls at all, so it is not looking at the right thing"
        offenders = [f"{name}: .{attr}()" for name, attr in offenders if name not in {"_call", "__call__"}]
        assert offenders == [], offenders

    def test_only_read_rpcs_may_post(self) -> None:
        assert frozenset({"runReport", "runRealtimeReport", "checkCompatibility"}) == ga_api.READ_RPCS

    def test_no_write_rpc_names_appear_in_the_package(self) -> None:
        forbidden = (
            "audienceExports",
            ":archive",
            ":acknowledgeUserDataCollection",
            ":provisionAccountTicket",
            "searchChangeHistoryEvents",
            ":batchCreate",
            ":batchDelete",
            ":batchUpdate",
        )
        for name, text in _sources().items():
            for fragment in forbidden:
                assert fragment not in text, f"{name} references {fragment}"


class TestScope:
    def test_only_the_readonly_scope_is_ever_requested(self) -> None:
        assert auth.SCOPE == "https://www.googleapis.com/auth/analytics.readonly"
        for name, text in _sources().items():
            for scope in (
                "auth/analytics.edit",
                "auth/analytics.manage",
                '"https://www.googleapis.com/auth/analytics"',
            ):
                assert scope not in text, f"{name} mentions {scope}"


class TestPathInjection:
    """Ids from user SQL must never shape a URL path beyond one numeric segment."""

    HOSTILE = (
        "../../accounts",
        "1/dataStreams/2:archive",
        "1:runAccessReport",
        "1?x=y",
        "1#frag",
        "1%2F..%2F",
        "１２３",  # full-width digits
    )

    def test_hostile_property_ids_are_rejected(self) -> None:
        for value in self.HOSTILE:
            try:
                result = ga_api.property_id(value)
            except ValueError:
                continue
            raise AssertionError(f"{value!r} was accepted as {result!r}")

    def test_hostile_account_ids_are_rejected(self) -> None:
        for value in self.HOSTILE:
            try:
                result = ga_api.account_id(value)
            except ValueError:
                continue
            raise AssertionError(f"{value!r} was accepted as {result!r}")
