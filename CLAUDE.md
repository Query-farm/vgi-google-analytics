# CLAUDE.md — vgi-google-analytics

A **read-only VGI worker** (Python, `vgi-python`) exposing Google Analytics 4 to
DuckDB: the Data API (reports, realtime, metadata, compatibility, quota) and the
Admin API (accounts, properties, streams, custom definitions, key events). The
catalog is `ga4`, one schema `main`. README.md is the user documentation;
this file is for working on the code.

## Gates — all must pass before a commit

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                       # strict
uv run pytest                     # offline, ~20s, includes the SQL end-to-end tier
uv run --no-sources --with vgi-lint-check==0.82.0 vgi-lint lint "uv run --no-sources ga4_worker.py" \
    --no-execute --no-check-links --audit-waivers --fail-on warning   # run FROM the repo root
```

vgi-lint reads `vgi-lint.toml` (waivers) and `vgi-agent-tests.yaml` (graders)
from the current directory — run it from the repo root or the waivers vanish.

## Invariants (each has a test)

- **Only `ga_api.py` touches the network or imports `httpx`.** Every request
  goes through `ga_api._call`, whose allow-list permits GET on the two API base
  URLs and POST only to `READ_RPCS` (`runReport`, `runRealtimeReport`,
  `checkCompatibility`). Do not add a write RPC; the worker is read-only by
  design. google-auth's token refreshes go through `ga_api.AuthTransport`, so
  never import `google.auth.transport.requests`. Browser OAuth uses the same token
  transport and a temporary loopback callback listener in `ga_api.OAuthCallback`.
  After sign-in there is no loopback token server; each worker refreshes the
  grant from the client's opaque attach data through `ga_api.AuthTransport`.
- **Only the `analytics.readonly` scope.** Never request `analytics.edit` (that
  is why change history is not exposed).
- **Ids are validated, not escaped**: `ga_api.property_id()` / `account_id()`
  accept ASCII digits only. Every user-supplied id must pass through one before
  it reaches a path.
- **Filter pushdown ⇒ apply every filter.** Any function with
  `filter_pushdown = True` must also set `auto_apply_filters` and
  `projection_pushdown`, and emit through `pushdown.build_filtered`. Translating
  a predicate to GA4 (`pushdown.dimension_filters`) is an extra optimisation,
  only for string `=`/`IN` on a requested dimension.
- **Paged endpoints page per tick.** Use `ListState` / `ReportState`
  (`ArrowSerializableDataclass`, string/int fields only) and
  `@init_single_worker`. Never walk all pages inside one `process()` unless the
  list is tiny and bounded (`all_property_ids` for fan-out is the exception).
- **Secrets are found by type, not name.** The framework keys resolved secrets
  by the DuckDB secret *name*; `auth.secret_values()` searches by type. Do not
  use `params.secrets.get("google_analytics")` — it misses any secret not
  literally named that.
- **Browser OAuth is local only.** Only the stdio entry point enables browser
  sign-in. HTTP/TCP/Unix workers cannot use it, including with `--allow-adc`.
  Query Farm supplies the Desktop app registration (`oauth_app.py`, project
  `query-farm-analytics-console`); users have no configure command or OS
  credential store. Each ATTACH returns a versioned refreshable `OAuthGrant` in
  `attach_opaque_data`, including the client registration and refresh token.
  Treat these bytes as credentials, never log them, and omit secrets from reprs
  and error messages. They are not encrypted by the worker. Fresh workers must
  reconstruct credentials without the authorizing process or a token broker;
  test SQL with `POOL false`. Keep per-worker credential caching bounded and
  isolate it by the complete grant, including its random attachment ID. Detach
  only evicts this worker's cache; it cannot invalidate copies or revoke Google
  consent. OAuth mode must reject `data_api_url` / `admin_api_url` overrides
  before sign-in and when decoding opaque data. The refresh endpoint and scope
  must stay fixed, and malformed grants must require a new ATTACH rather than
  falling back to ADC.
- **Configuration is ATTACH options, not environment variables.** Per-catalog
  choices live in `settings.Settings`, validated in `GA4Catalog.catalog_attach`
  and delivered to every call as `attach_opaque_data`; `common.session(params)`
  decodes them. The one explicit process-level switch is the server's
  `--allow-adc` (`auth.permit_adc`), because ATTACH options are client-chosen.
  The entry point also restricts browser OAuth to stdio (`oauth.permit_local`).
  Overriding an API URL must keep disabling ADC (`Settings.may_use_adc`).
- **Property-scoped functions take `''` for every property.** A function whose
  first argument is a property id also accepts `''`, walks the readable
  properties one per tick (`admin.current_property` / `next_property` over a
  `FanoutState` or `ReportState`), stamps `property_id` on every row, and visits
  only the properties a pushed `WHERE property_id = / IN` names. A refusing
  property (403/404) is skipped only in that walk; a 400 always fails.
- **PEP-723 headers mirror pyproject.** `ga4_worker.py` and `serve.py` must list
  the same runtime deps at the same lower bounds (`tests/test_packaging.py`).
  Bump both together.

## Adding a function

1. Fixed schema → `schemas.py`, every field via `meta.field(name, type, comment)`.
2. Function class in the right module with a `Meta` carrying `name`,
   `description`, `categories`, `required_secrets = REQUIRED_SECRETS`, pushdown
   flags, `tags = docs(...)` (llm + md + `example_queries`, `result_schema` or
   `vgi.result_dynamic_columns_md`), and `examples` (each also present, with a
   description, in the tag). Examples pass `''` (every readable property) or
   `'0'` (the universal metadata), never a literal property id, so they run
   against any credential. Never a subquery: DuckDB rejects one as a
   table-function argument. `TestCatalogExamples` runs every example and
   grader query against the fake.
3. Register in the module's `*_FUNCTIONS` list.
4. Add a public task (`name`, `prompt`) to `_AGENT_TEST_TASKS` in `worker.py`
   **and** its grader to `vgi-agent-tests.yaml` (same name, same order).
5. Teach `tests/fake_ga.py` the endpoint, then add unit + end-to-end coverage.
6. Re-run all four gates; vgi-lint must stay at 0 findings.

## Testing notes

- `tests/fake_ga.py` is a deterministic GA4 stand-in (fixed `TODAY`, synthetic
  fact table). `handle()` plugs into `httpx.MockTransport`; `serve()` runs it
  on a socket for the end-to-end tier, which ATTACHes the worker subprocess with
  `data_api_url` / `admin_api_url` aimed at it and `report_page_size 10`.
- `conftest.py` resets the connection pool, credential caches (secret and
  OAuth) and metadata cache around every test, and disables ADC and browser
  sign-in so a developer's own Google login is never used by accident.
- `tests/test_oauth.py` drives the real loopback callback and code exchange
  with a stubbed browser, and a real SQL ATTACH (`POOL true` and `false`)
  through a launcher that replaces `_browser_login`.
- Live tests (`-m live`) need `VGI_GA4_TEST_SERVICE_ACCOUNT_JSON` or
  `VGI_GA4_TEST_ACCESS_TOKEN` (or ADC), optionally `VGI_GA4_TEST_PROPERTY`.
- When analysing data, go through SQL against an ATTACHed worker, and fix a
  missing capability in the worker rather than working around it.

## SDK quirks worth knowing (vgi-python 0.45.x)

- `params.secrets` at bind is a `SecretsAccessor` whose `.get(type)` looks up by
  secret name; use `.to_dict().of_type(...)` (wrapped by `auth.secret_values`).
- `OutputCollector.emit(..., cache_control=)` is typed on vgi-rpc's base class
  without that kwarg; use `common.emit()`, which casts to `VgiOutputCollector`.
- An `Arg(pattern=...)` / `ge=` / `le=` is enforced by the framework at bind and
  is what vgi-lint's VGI317 wants for constrained arguments.
