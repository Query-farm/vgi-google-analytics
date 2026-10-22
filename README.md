<p align="center">
  <a href="https://query.farm/vgi/">
    <img src="https://raw.githubusercontent.com/Query-farm/vgi-google-analytics/main/docs/vgi-logo.png" alt="Vector Gateway Interface logo" width="320">
  </a>
</p>

<h1 align="center">vgi-google-analytics</h1>

<p align="center">
  <a href="https://developers.google.com/analytics">Google Analytics 4</a> reports, realtime data and property
  configuration as ordinary DuckDB tables.<br>
  A <strong>read-only</strong> <a href="https://query.farm/vgi/">VGI</a> worker, built by <a href="https://query.farm">🚜 Query.Farm</a>
</p>

<p align="center">
  <a href="https://github.com/Query-farm/vgi-google-analytics/actions/workflows/ci.yml"><img src="https://github.com/Query-farm/vgi-google-analytics/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.13%2B-blue.svg" alt="Python 3.13+">
  <a href="https://query.farm/vgi/"><img src="https://img.shields.io/badge/VGI-Vector%20Gateway%20Interface-2f7d32.svg" alt="VGI"></a>
</p>

---

```sql
ATTACH 'ga4' (TYPE vgi, LOCATION 'uv run ga4_worker.py');
CREATE SECRET ga (TYPE google_analytics, service_account_json '<key file contents>');

SELECT date, sessionDefaultChannelGroup, sessions, keyEvents
FROM ga4.report('123456789',
    dimensions => ['date', 'sessionDefaultChannelGroup'],
    metrics    => ['sessions', 'keyEvents'],
    start_date => '28daysAgo', end_date => 'yesterday')
ORDER BY date;
```

> **Read-only, twice over.** The worker only ever requests the
> `analytics.readonly` OAuth scope, and every HTTP call goes through one
> chokepoint (`ga_api._call`) that allows `GET` plus exactly three read RPCs
> (`runReport`, `runRealtimeReport`, `checkCompatibility`). Anything else is
> refused before a socket opens. `tests/test_readonly_guard.py` fails the build
> if either guarantee weakens.

## Contents

- [Run](#run)
- [Credentials](#credentials)
- [Surface](#surface)
- [Reports](#reports)
- [Realtime](#realtime)
- [Configuration](#configuration)
- [Recipes](#recipes)
- [Design notes](#design-notes)
- [Not included, and why](#not-included-and-why)
- [ATTACH options](#attach-options)
- [Development](#development)
- [CI](#ci)
- [License](#license)

## Run

```bash
uv run ga4_worker.py            # stdio — what DuckDB launches
uv run serve.py --port 8000     # HTTP
```

Both scripts carry PEP-723 headers pinning their dependencies, so they run from a
fresh clone with nothing installed. A relative `LOCATION` resolves against the
working directory, so it only works from inside the clone. Anywhere else, point
`LOCATION` at the repository and let `uvx` fetch and cache it:

```sql
ATTACH 'ga4' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-google-analytics vgi-google-analytics');
```

Pin a tag for a deployment, so the worker cannot change under you:

```sql
ATTACH 'ga4' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-google-analytics@v0.1.0 vgi-google-analytics');
```

[Haybarn](https://query.farm), Query Farm's DuckDB distribution, ships the `vgi`
extension. With stock DuckDB, `INSTALL vgi FROM community; LOAD vgi;` first.

## Credentials

Every Google Analytics call needs an OAuth token; there is no anonymous access.
For local use, ATTACH opens a browser to sign in to Query Farm's Google OAuth
app. Each attachment carries its own authorization in the client's opaque
attach data. For HTTP deployments or supplied credentials, use a DuckDB
**secret** of type `google_analytics` — never an ATTACH option, because ATTACH
strings show up in `duckdb_databases()` and in statement logs. Every
credential-bearing field is marked redacted, so `duckdb_secrets()` masks it.
The secret's *name* does not matter; the worker finds it by type.

### Sign in to Query Farm's Google app (local)

Query Farm registers one OAuth application and distributes its Desktop app
client registration with the worker. Each user signs in with their own Google
account. Users do not create a Google Cloud project or configure client credentials.

From the project directory:

```sql
ATTACH 'ga4' (TYPE vgi, LOCATION 'uv run ga4_worker.py', credentials 'oauth');
SELECT * FROM ga4.account_summaries;
```

ATTACH opens Google's account chooser. Choose an account with access to your
GA4 properties and approve read-only access. ATTACH completes when sign-in
succeeds. A login attempt times out after three minutes; rerun ATTACH to retry.

Each attachment has an independent login. ATTACH returns its refreshable OAuth
grant in VGI's `attach_opaque_data`, which the client retains and supplies to
subsequent worker calls. Each worker can obtain and refresh its own access token,
so queries continue after the worker that performed sign-in exits, including
with `POOL false`. The worker uses no Keychain or credential files for browser
sign-in and no background token server.

**The opaque attach data contains credentials, including the refresh token.**
It is not encrypted by this worker. Clients must treat it as a secret: keep it
out of logs, diagnostics and ordinary SQL options, and protect any storage if
they choose to persist it. The local SQL flow retains it for the attachment;
this does not automatically remember the login after the SQL application exits.
A new ATTACH starts a new sign-in. Google's browser session may make that quicker.

Workers keep a bounded in-memory credential cache to reuse access tokens. A
detach RPC evicts this worker's cache entry; the current native extension does
not send that RPC on SQL DETACH. DETACH removes the SQL catalog, but cannot
revoke credentials already copied into other workers or clients. Worker exit
clears its cache without invalidating the client's grant. Google's token
validity and revocation determine whether a retained grant can still refresh.

For two Google accounts, attach twice and choose the appropriate account in
each browser flow:

```sql
ATTACH 'ga4' AS work_ga4 (
    TYPE vgi, LOCATION 'uv run ga4_worker.py', credentials 'oauth'
);
ATTACH 'ga4' AS personal_ga4 (
    TYPE vgi, LOCATION 'uv run ga4_worker.py', credentials 'oauth'
);

-- Remove only the work catalog from this SQL connection.
DETACH work_ga4;
```

Only the browser authorization callback uses a temporary loopback listener;
it closes when sign-in finishes. To revoke the app's authorization, use Google's
connected-app settings. If a refresh token expires or is revoked, detach and
attach again to sign in.

Browser sign-in currently supports **local stdio** workers, with the browser on
the same computer. HTTP, TCP and Unix-socket workers require credentials supplied
by their clients unless the operator explicitly enables ADC. OAuth mode also
requires the standard Google API endpoints, so `data_api_url` and
`admin_api_url` cannot be overridden with it.

**Query Farm setup (publisher only):** this checkout includes Query Farm's
Desktop OAuth client registration in `vgi_google_analytics/oauth_app.py`, in
the `query-farm-analytics-console` Google Cloud project. That project must
have the *Google Analytics Data API* and *Google Analytics Admin API* enabled,
and its consent screen must list the `analytics.readonly` scope for the
intended audience. During testing, add participating Google accounts as test
users. If replacing the registration, create a client of type **Desktop app**
and update `CLIENT_ID` and `CLIENT_SECRET` in that module. Complete Google's
applicable verification before a public rollout. Desktop client identifiers are
public app configuration; never put user tokens in that module. See Google's
[desktop OAuth documentation](https://developers.google.com/identity/protocols/oauth2/native-app).

### Supplying credentials with a DuckDB secret

Set **exactly one** of the following shapes. A secret that sets two is
rejected rather than resolved by precedence, so it is never ambiguous which
identity is reading your data.

### Option 1 — a service account (recommended for servers)

Best for servers, schedules and CI: no user involved, nothing expires.

1. In the [Google Cloud console](https://console.cloud.google.com/), pick or
   create a project and **enable** both the *Google Analytics Data API* and the
   *Google Analytics Admin API*.
2. *IAM & Admin → Service accounts → Create*. No Cloud roles are needed.
3. On the new account, *Keys → Add key → JSON*. Keep the downloaded file safe.
4. In **Google Analytics**, *Admin → Property access management → +*, add the
   service account's email (the `client_email` in the key) with the **Viewer**
   role. Repeat per property, or grant it at the account level.

```sql
CREATE SECRET ga (TYPE google_analytics,
    service_account_json '{"type": "service_account", "project_id": "...", ...}');
```

From a file, without pasting it into SQL history:

```sql
SET VARIABLE ga_key = (SELECT content FROM read_text('/secure/path/ga-reader.json'));
CREATE SECRET ga (TYPE google_analytics, service_account_json getvariable('ga_key'));
```

### Option 2 — an OAuth user grant

For people who cannot add a service account to the property (an agency client,
the [GA4 demo account](https://support.google.com/analytics/answer/6367342)).
Create an *OAuth client ID* (type *Desktop app*) in the Cloud console, run any
OAuth flow once with scope `https://www.googleapis.com/auth/analytics.readonly`
to obtain a refresh token, then:

```sql
CREATE SECRET ga (TYPE google_analytics,
    client_id '1234-abc.apps.googleusercontent.com',
    client_secret 'GOCSPX-...',
    refresh_token '1//0g...');
```

### Option 3 — a short-lived access token

Handy for a quick test. Expires in about an hour.

```sql
CREATE SECRET ga (TYPE google_analytics, access_token 'ya29....');
```

### Fallback — Application Default Credentials

With no secret, and unless the catalog was attached with
`credentials 'secret'`, a **stdio** worker falls back to Google's
[Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials):
`GOOGLE_APPLICATION_CREDENTIALS`, or your own login via

```bash
gcloud auth application-default login \
  --client-id-file=/path/to/desktop-oauth-client.json \
  --scopes=https://www.googleapis.com/auth/analytics.readonly
```

The **HTTP** server does *not* fall back: a shared server's ADC is the server's
identity, and lending it to every client that forgot a secret would be a
privilege leak. An operator who wants that starts the server with
`uv run serve.py --allow-adc`. This is deliberately a server flag rather than an
ATTACH option, because ATTACH options are chosen by clients.

The fallback is also disabled whenever `data_api_url` or `admin_api_url` is
overridden, so no ATTACH can aim the worker at another host and receive its
token. A secret that is present but broken never falls back to ADC either.

### Quota project

Add `quota_project_id 'my-project'` to any shape to bill API usage to a specific
Cloud project (sent as `x-goog-user-project`).

## Surface

Everything lives in one schema. Three tables need no arguments; everything else
is keyed by a **numeric property id** — not the `G-XXXXXXX` measurement id,
which names a data stream.

### Tables

| Table | One row per | Use it to |
|---|---|---|
| `account_summaries` | readable property (with its account) | **start here** — find property ids |
| `accounts` | readable account | see accounts, regions, creation dates |
| `properties` | readable property | time zone, currency, service level (standard / 360) |

Each is backed by a scan function of the same name with an `all_` prefix
(`all_account_summaries()`, …), because a function and a table cannot share a
name in one schema.

### Functions

| Function | Returns |
|---|---|
| `report(property, dimensions =>, metrics =>, …)` | a GA4 report — see [Reports](#reports) |
| `realtime_report(property, dimensions =>, metrics =>, …)` | the last 30 minutes — see [Realtime](#realtime) |
| `dimensions(property)` | every dimension `report()` accepts, with the SQL type it becomes |
| `metrics(property)` | every metric, with GA4's type, formula and any access restriction |
| `check_compatibility(property, dimensions =>, metrics =>)` | which fields can be combined in one report |
| `property_quota(property)` | Data API tokens left this hour and today |
| `data_streams(property)` | web/app streams, including measurement ids |
| `custom_dimensions(property)` | custom dimensions, with the `api_name` a report takes |
| `custom_metrics(property)` | custom metrics, with units and `api_name` |
| `key_events(property)` | events counted as key events (formerly conversions) |

`dimensions('0')` / `metrics('0')` list the universal set every property shares.
The four configuration functions accept `''` to walk **every** readable property;
their `property_id` column says where each row came from.

Every object carries catalog documentation — descriptions, column comments,
categorised examples — readable from `duckdb_functions()`, `duckdb_tables()` and
`DESCRIBE`, so an agent can discover the surface without this README.

## Reports

```sql
SELECT * FROM ga4.report('123456789',
    dimensions       => ['country', 'deviceCategory'],   -- group by
    metrics          => ['activeUsers', 'engagementRate'], -- aggregate
    start_date       => '28daysAgo',                     -- default
    end_date         => 'yesterday',                     -- default
    compare_start_date => '', compare_end_date => '',     -- optional 2nd range
    dimension_filter => '',   -- GA4 FilterExpression JSON (pre-aggregation)
    metric_filter    => '',   -- GA4 FilterExpression JSON (post-aggregation)
    keep_empty_rows  => false,
    currency_code    => '',   -- ISO 4217; default is the property's
    max_rows         => 0,    -- 0 = all
    quality_columns  => false,
    cache_ttl        => 0);   -- seconds DuckDB may reuse the result
```

### Typed columns

The worker reads the property's metadata when the query is **bound**, so the
result is typed and a misspelling fails immediately, with suggestions:

| Requested | Column type |
|---|---|
| `date`, `firstSessionDate` | `DATE` |
| `dateHour`, `dateHourMinute` | `TIMESTAMP` (property-local wall-clock time) |
| any other dimension | `VARCHAR` |
| metric of type `TYPE_INTEGER` | `BIGINT` |
| any other metric (rates, durations, currency, distances) | `DOUBLE` |

Columns are named exactly as requested. Custom definitions contain a colon and
need double quotes: `SELECT "customEvent:plan_tier" FROM ga4.report(...)`.
Values GA4 cannot express in a typed column — `(other)` in a `date` column, for
instance — become `NULL` rather than failing the query.

### Dates

`start_date` / `end_date` accept `YYYY-MM-DD`, `today`, `yesterday` or
`NdaysAgo`, inclusive, in the **property's** reporting time zone (see
`properties.time_zone`). The default is the last 28 complete days. Recent data
can be incomplete for a day or two while GA4 processes it.

Set `compare_start_date` and `compare_end_date` to add a second range; the
result gains a `dateRange` column reading `current` or `comparison`, so
period-over-period is a `PIVOT` away.

### Filtering

A SQL `WHERE` on a string dimension column using `=` or `IN` is **pushed down**
to GA4 as a case-sensitive `dimensionFilter`, so fewer rows cross the wire.
Every other predicate is applied by the worker after the fact. Either way the
result is exactly what the SQL says.

To filter on a dimension you do **not** want as a column — purchases by
country, without an `eventName` column — pass a GA4
[`FilterExpression`](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/FilterExpression)
as JSON. It is combined with any pushed-down predicate:

```sql
SELECT country, eventCount
FROM ga4.report('123456789', dimensions => ['country'], metrics => ['eventCount'],
    dimension_filter => '{"filter": {"fieldName": "eventName",
                                     "stringFilter": {"value": "purchase"}}}')
ORDER BY eventCount DESC;
```

`metric_filter` is the post-aggregation counterpart (like `HAVING`).

### Totals are GA4's, not SQL's

GA4 aggregates before SQL sees anything. Unique counts — `totalUsers`,
`activeUsers`, `newUsers` — are deduplicated **within** each row. Summing them
across dates, pages or channels counts a returning user once per row they
appear in. Ask GA4 for the breakdown you want to read:

```sql
-- Right: distinct users over the whole range
SELECT totalUsers FROM ga4.report('123456789', metrics => ['totalUsers']);

-- Wrong: double-counts anyone active on more than one day
SELECT sum(totalUsers) FROM ga4.report('123456789', dimensions => ['date'], metrics => ['totalUsers']);
```

### Data quality

GA4 may withhold rows to protect privacy (thresholding), fold rare values into
an `(other)` row, or sample. `quality_columns => true` appends three columns to
every row: `subject_to_thresholding`, `data_loss_from_other_row` and
`sampling_ratio` (1.0 when unsampled).

### Paging, limits and quota

Rows stream one API page at a time (100,000 rows per request, offset-paged), so
a `LIMIT` stops the walk early and a large report is never held in memory whole.
`max_rows` caps what is requested from Google in the first place.

Each request spends tokens from the property's Data API quota — 200,000 a day
and 40,000 an hour on a standard property, ten times that on GA4 360 — and long
ranges and high-cardinality dimensions cost more. `property_quota(id)` shows
what is left. `cache_ttl => 3600` lets DuckDB reuse an identical query's result
for an hour instead of spending tokens again. Transient failures (HTTP 429 from
the 10-concurrent-requests limit, 5xx) are retried with bounded backoff; an
exhausted token budget surfaces as an error naming the quota.

## Realtime

```sql
SELECT country, activeUsers
FROM ga4.realtime_report('123456789', dimensions => ['country'], metrics => ['activeUsers'])
ORDER BY activeUsers DESC;
```

Activity in the last 30 minutes (60 on GA4 360), seconds old. Realtime reports
use their own, smaller [schema](https://developers.google.com/analytics/devguides/reporting/data/v1/realtime-api-schema):
dimensions such as `country`, `city`, `deviceCategory`, `platform`,
`eventName`, `unifiedScreenName`, `streamName`, `audienceName` and
`minutesAgo` (a `BIGINT`, 0 = this minute); metrics `activeUsers`,
`eventCount`, `keyEvents` and `screenPageViews`. `minutes_ago_start` /
`minutes_ago_end` narrow the window. Results are never cached.

## Configuration

```sql
-- Which property does each website tag belong to?
SELECT measurement_id, default_uri, property_id
FROM ga4.data_streams('') WHERE type = 'WEB_DATA_STREAM';

-- Custom dimensions, as names a report accepts
SELECT api_name, display_name, scope FROM ga4.custom_dimensions('123456789');

-- Properties missing a purchase key event
SELECT property_id, property_name FROM ga4.account_summaries
WHERE property_id IS NOT NULL AND property_id NOT IN (
    SELECT property_id FROM ga4.key_events('') WHERE event_name = 'purchase');
```

## Recipes

```sql
-- Find a dimension by what it means
SELECT api_name, ui_name, description FROM ga4.dimensions('0')
WHERE description ILIKE '%landing page%';

-- Top landing pages by engagement, last 7 days vs the 7 before
PIVOT (
  SELECT landingPage, dateRange, sessions
  FROM ga4.report('123456789', dimensions => ['landingPage'], metrics => ['sessions'],
      start_date => '7daysAgo', compare_start_date => '14daysAgo', compare_end_date => '8daysAgo')
) ON dateRange USING sum(sessions)
ORDER BY current DESC LIMIT 20;

-- Hourly traffic profile, in the property's time zone
SELECT hour(dateHour) AS hour, avg(sessions) AS avg_sessions
FROM ga4.report('123456789', dimensions => ['dateHour'], metrics => ['sessions'])
GROUP BY ALL ORDER BY hour;

-- Revenue by channel, in euros
SELECT sessionDefaultChannelGroup, totalRevenue
FROM ga4.report('123456789', dimensions => ['sessionDefaultChannelGroup'],
    metrics => ['totalRevenue'], currency_code => 'EUR')
ORDER BY totalRevenue DESC;

-- Will this combination work before spending quota on it?
SELECT kind, api_name, compatibility
FROM ga4.check_compatibility('123456789', dimensions => ['itemName'], metrics => ['sessions'])
WHERE requested;

-- Land a year of daily data in a local table
CREATE TABLE daily AS
SELECT * FROM ga4.report('123456789', dimensions => ['date', 'country'],
    metrics => ['activeUsers', 'sessions', 'keyEvents', 'totalRevenue'],
    start_date => '365daysAgo');
```

## Design notes

**REST over `httpx`, not Google's gRPC clients.** The generated
`google-analytics-data` / `-admin` libraries bring grpcio and protobuf and
return protobuf messages. Both APIs speak plain JSON over REST, which maps
directly onto Arrow, needs one small dependency, and gives the tests an
`httpx.MockTransport` seam that exercises the real request code.
`google-auth` is still used — only to mint and refresh tokens — and its own
HTTP calls are routed through the same `httpx` pool by a small transport
adapter (`ga_api.AuthTransport`), so `requests` is never imported.

**Types come from metadata, at bind.** The Data API returns every value as a
string. Reading `getMetadata` during bind (cached per property for an hour)
means a report's columns are typed before DuckDB plans the query, and an
unknown name fails with a "did you mean" instead of a 400 halfway through.

**Pushdown is always re-applied.** Declaring `filter_pushdown` makes DuckDB drop
its own filter above the scan, so a predicate the worker receives and ignores
would silently stop applying. The worker therefore evaluates *every* pushed
predicate against the rows it emits (`pushdown.build_filtered`), and only
*additionally* translates the subset GA4 can evaluate identically (string
`=` / `IN` on a requested dimension). `tests/test_end_to_end.py` holds it to
this with impossible predicates that must return nothing.

**Scans stream and can be cancelled.** Every paged endpoint is walked one page
per `process()` tick, with the page token or offset carried in serialisable scan
state. A report's request is frozen on its first page, because an offset is
only meaningful against the query that produced it.

**Ids are validated, not escaped.** Property and account ids from SQL must be
ASCII digits (optionally `properties/`-prefixed) before they reach a URL path,
so no value can traverse to another endpoint. Measurement ids (`G-…`) and
Universal Analytics ids (`UA-…`) get a specific, helpful error.

**Errors say what to do.** A 403 names the service account to grant Viewer to;
a disabled API says which two to enable; a 429 points at `property_quota()`.

## Not included, and why

| GA4 feature | Status |
|---|---|
| Pivot reports (`runPivotReport`) | Use DuckDB's `PIVOT` over `report()` — same result, more flexible. |
| Batch reports | One `report()` per query composes better; DuckDB runs them in one statement. |
| Cohort reports (`cohortSpec`) | Not yet exposed; the cohort request shape needs its own function. |
| Funnel reports | `v1alpha` only — not stable enough to build a contract on. |
| Audience exports | Creating an export is a **write**; out of scope for a read-only worker. |
| Change history (`searchChangeHistoryEvents`) | Requires the `analytics.edit` scope, which this worker never requests. |
| Data access reports (`runAccessReport`) | Not yet exposed. |
| Universal Analytics | Shut down by Google in 2024. |

## ATTACH options

Everything that changes how one attached catalog behaves is an ATTACH option,
visible in the statement that made the choice and scoped to that catalog. Each
is validated when you ATTACH, so a typo fails there rather than on a later query.

```sql
ATTACH 'ga4' (TYPE vgi, LOCATION 'uv run ga4_worker.py',
              credentials 'secret', report_page_size 50000);
```

| Option | Default | Purpose |
|---|---|---|
| `credentials` | `'auto'` | `'auto'`: the secret, else Application Default Credentials where the worker permits it. `'secret'`: require the secret, never fall back. `'oauth'`: browser sign-in to Query Farm's app (local stdio only), with refreshable credentials in this attachment's opaque data. |
| `report_page_size` | `100000` | Rows per `runReport` request, 1–250,000. Smaller pages reach DuckDB sooner. |
| `data_api_url` | `https://analyticsdata.googleapis.com/v1beta` | Data API base URL, for a proxy or a test double. Overriding it disables the ADC fallback and is rejected with `credentials 'oauth'`. |
| `admin_api_url` | `https://analyticsadmin.googleapis.com/v1beta` | Admin API base URL, likewise. |

The worker reads no environment variables of its own. The only ones that matter
are Google's: `GOOGLE_APPLICATION_CREDENTIALS`, used by the ADC fallback.

## Development

### Commands

```bash
uv sync                                   # dev environment
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict
uv run pytest                             # offline: unit + SQL end-to-end against a fake GA4
uv run pytest -m live                     # against real Google Analytics (see below)
uv run --no-sources --with vgi-lint-check==0.82.0 vgi-lint lint "uv run --no-sources ga4_worker.py" \
    --no-execute --no-check-links --audit-waivers --fail-on warning
```

### Layout


| Module | Role |
|---|---|
| `ga_api.py` | The only module that touches the network: allow-list, auth headers, retries, errors |
| `auth.py` | The `google_analytics` secret type; credential parsing, caching, ADC fallback |
| `oauth.py` | Query Farm browser sign-in, portable OAuth grants and per-worker credential caching |
| `oauth_app.py` | Query Farm's shared Desktop OAuth app registration |
| `settings.py` | ATTACH options: validation and the ADC safety rules |
| `admin.py` | Accounts, properties, streams, custom definitions, key events |
| `discovery.py` | `dimensions`, `metrics`, `check_compatibility`, `property_quota` |
| `reports.py` | `report` and `realtime_report` |
| `types.py` | Requested dimensions/metrics → typed Arrow schema |
| `pushdown.py` | Applying pushed filters; translating the safe subset to GA4 |
| `schemas.py` | Fixed schemas (one documented comment per column) and total coercers |
| `meta.py` | Builders for the `vgi.*` documentation tags |
| `worker.py` | Catalog assembly, tables, catalog docs, entry points |

### Tests

Three tiers, all under `tests/`:

- **Unit** (`test_auth`, `test_oauth`, `test_ga_api`, `test_shapes`,
  `test_catalog`, `test_packaging`, `test_readonly_guard`) — pure functions,
  browser sign-in and portable grants (including a real ATTACH through
  replacement workers), the HTTP chokepoint via `httpx.MockTransport`, metadata
  completeness, PEP-723 header drift, and the read-only guarantees.
- **End-to-end SQL, offline** (`test_end_to_end`) — the real worker subprocess
  attached from DuckDB, talking over a socket to `tests/fake_ga.py`, a
  deterministic stand-in for both APIs. Covers binding, typing, secrets, filter
  pushdown correctness, paging, `LIMIT` early-exit and error messages. Runs on
  every push; needs no credentials.
- **Live** (`test_live`, marker `live`) — the same through a real ATTACH against
  real GA4. Configure with `VGI_GA4_TEST_SERVICE_ACCOUNT_JSON` (key JSON text)
  or `VGI_GA4_TEST_ACCESS_TOKEN`, or neither to use your ADC; and optionally
  `VGI_GA4_TEST_PROPERTY`. Every assertion holds for any property, quiet or busy.

## CI

`.github/workflows/ci.yml` runs on every push and pull request:

- **check** — ruff lint + format, mypy `--strict`, the full offline suite
  (including the end-to-end SQL tier), and both entry points starting from their
  PEP-723 headers alone.
- **catalog** — `vgi-lint` structural tier at a pinned version, with
  `--audit-waivers` (a waiver that stops suppressing anything fails the build)
  and `--fail-on warning`.

`.github/workflows/live.yml` runs nightly and on demand against real Google
Analytics: the live test tier, then `vgi-lint` executing every catalog example.
It needs the repository secret `GA4_SERVICE_ACCOUNT_JSON` (and optionally the
variable `GA4_TEST_PROPERTY`); without them it skips cleanly.

`vgi-lint.toml` carries three waivers, each with its reason;
`vgi-agent-tests.yaml` holds the private graders for the agent-suitability
suite, whose public prompts are in the catalog.

## License

MIT — see [LICENSE](LICENSE). Copyright © 2026 Query Farm LLC.

Analytics data belongs to the property owner and is accessed under the
[Google Analytics Terms of Service](https://marketingplatform.google.com/about/analytics/terms/us/).
Google Analytics is a trademark of Google LLC; this project is not affiliated
with or endorsed by Google. See [NOTICE](NOTICE).
