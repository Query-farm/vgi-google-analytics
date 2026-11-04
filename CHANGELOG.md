# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- **Browser sign-in.** `ATTACH ... (credentials 'oauth')` signs in through
  Query Farm's shared Desktop OAuth app, with PKCE and a temporary loopback
  callback. Each attachment gets its own login; its refreshable grant travels
  in the client's opaque attach data, so any worker — including a replacement
  after the signing-in worker exits, or with `POOL false` — can refresh it.
  No credential files, OS keychain or token server. Query Farm supplies the
  app registration in `oauth_app.py`; end users configure nothing.

- **Every property at once for reports.** `report()`, `realtime_report()`,
  `check_compatibility()` and `property_quota()` accept `''` to run on every
  readable property, one property per tick. `WHERE property_id = ...` / `IN`
  visits only the named properties, for these and for the four configuration
  functions.

### Changed

- Browser sign-in responses use a self-contained Query Farm page with light
  and dark themes, connector-specific documentation, and distinct received,
  declined and invalid response states. Authorization receipt is shown before
  the SQL session completes the credential exchange.
- `report()`, `realtime_report()` and `check_compatibility()` gain a leading
  `property_id` column.
- Upgraded to `vgi-python` 0.48.2 and `vgi-rpc` 0.52.4, with canonical VGI 3
  table constraints. Clients now need a VGI 3 extension (Haybarn 2.0.0rc11 or
  newer). Workers use DuckDB 1.5.5 for the SDK's pinned filter-evaluation
  profile; SQL tests launch isolated workers from their PEP-723 headers.
- Updated the SQL test client to Haybarn 2.0.0rc11 and `vgi-lint-check` to 2.0.0.
- Browser sign-in is disabled on every shared transport (`--http`, `--tcp`,
  `--unix`), and those transports fall back to ADC only with `--allow-adc`,
  whichever entry point started them.
- OAuth mode rejects `data_api_url` / `admin_api_url` overrides, so a Google
  token is only ever sent to Google.

### Fixed

- Catalog examples, executable examples and agent graders passed the property
  as a scalar subquery, which DuckDB rejects as a table-function argument, so
  none of them ran. They now use `''`, and the offline suite executes every one
  against the fake.

## 0.1.0

First release: read-only Google Analytics 4 for DuckDB.

### Added

- **Reports.** `report()` runs the GA4 Data API's `runReport` with typed
  columns derived from the property's metadata at bind time (`BIGINT` / `DOUBLE`
  metrics; `DATE` / `TIMESTAMP` calendar dimensions), date-range comparison
  (`dateRange` column), JSON dimension and metric filters, `keep_empty_rows`,
  `currency_code`, `max_rows`, optional data-quality columns and an opt-in
  `cache_ttl`. Rows stream one offset page per tick.
- **Realtime.** `realtime_report()` over the last 30 (or 60) minutes.
- **Discovery.** `dimensions()`, `metrics()`, `check_compatibility()` and
  `property_quota()`.
- **Configuration.** Tables `account_summaries`, `accounts`, `properties`;
  functions `data_streams()`, `custom_dimensions()`, `custom_metrics()` and
  `key_events()`, each accepting `''` to walk every readable property.
- **Credentials.** A `google_analytics` DuckDB secret type taking a
  service-account key, an OAuth refresh-token grant or an access token, with
  Application Default Credentials as a stdio-only fallback.
- **ATTACH options.** `credentials` (`auto` / `secret`), `report_page_size`,
  `data_api_url` and `admin_api_url`, validated at ATTACH. The worker reads no
  environment variables of its own; the HTTP server's ADC fallback is an
  operator-only `--allow-adc` flag.
- **Filter pushdown.** String `=` / `IN` predicates on report dimensions are
  sent to GA4; every pushed predicate is also applied by the worker.
- **Read-only guarantees.** One HTTP chokepoint with a GET + three-read-RPC
  allow-list, the `analytics.readonly` scope only, and numeric-only ids in URL
  paths, all held by `tests/test_readonly_guard.py`.
- **Tests and CI.** Unit tests, an offline SQL end-to-end tier against a fake
  GA4 server, a live tier, ruff, mypy `--strict` and vgi-lint (100/100).
