# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

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
