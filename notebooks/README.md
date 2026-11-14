# Google Analytics notebooks

[01 · Daily distinct users do not add up to distinct users for a period](01_daily_users_are_not_period_users.ipynb) asks the real worker for daily and period reports over an explicitly synthetic event fixture. It reconciles event counts while showing why daily distinct-user counts cannot be added into a period distinct-user total.

## Run

Requirements: [uv](https://docs.astral.sh/uv/), Git, Python 3.13–3.14 (uv can install it), and internet for initial dependencies and worker downloads. No Google account, credentials, property, source checkout or worker build is needed.

```sh
cd notebooks
uv sync --locked
uv run --locked python execute.py --timeout 300
```

The notebook runs on Haybarn 2.0.0rc11 and visibly launches `uvx --from git+https://github.com/Query-farm/vgi-google-analytics@f0d25da5fe34e01bb8ec3fea04d7697ed0287db0 ... vgi-google-analytics`. This exact remote revision is fetched and cached by uv. No release tag or PyPI package is assumed. The command pins vgi-python 0.51.0, vgi-rpc 0.53.1, httpx 0.28.1 and pyarrow 25.0.1. Source requirements permit SDK >=0.50; its root lock selects 0.50.0, which speaks protocol 3.2 and fails the notebook client's protocol-3.3 handshake. The notebook selects the tested 0.51.0 runtime without changing worker source or its root lock. Upgrade deliberately and rerun both examples and relevant worker checks.

Optional editor kernel:

```sh
uv run --locked python -m ipykernel install --user --name vgi-google-analytics-notebooks --display-name "Python (vgi-google-analytics notebooks)"
```

Use this directory as working directory and Restart Kernel → Run All. The runner creates its own temporary kernel and replaces saved outputs only after a successful full run.

## Experiment

```sh
uv run --locked python execute.py --parameters '{"END_DATE":"2026-09-05"}' --output-dir artifacts/through-sep-5
```

The default end date is 2026-09-03. Overrides require a separate destination. Branding, docs, runner, fixture/helper and lock accompany the copy; environments, logs and validation artifacts are ignored.

## What is synthetic

`synthetic_events.json` is an authored finite event inventory with explicit `synthetic-*` user labels and `fixture-event-*` event identifiers. It contains five calendar dates in the declared `America/New_York` property time zone. No records were collected from Google or real visitors, and no acquisition timestamp is claimed. The notebook displays its SHA-256 and reconciles returned report values to this independent inventory.

`fixture_server.py` implements only metadata and the read-only `runReport` HTTP POST on an ephemeral loopback port. It supports one fixture property, the two documented date windows, dimensions `[]` or `['date']`, and metrics `totalUsers,eventCount`. It groups event records and counts exact distinct synthetic identifiers, returns integer metadata and paged rows, records offsets, and never forwards requests externally. Unsupported routes or requests fail. The real unchanged worker performs authentication-header handling, HTTP calls, metadata-driven SQL schema binding, pagination and type conversion.

The fixture token `synthetic-notebook-token` is a public non-secret label, valid only for this local companion. `credentials 'secret'` prevents ADC and browser discovery, and overriding API URLs also disables ADC. No real token is sent to the fixture or stored in outputs.

Daily `totalUsers` counts user-days when summed; it is not the count of distinct users over the period. Daily `eventCount` is additive across this fixture's disjoint date partitions. Both queries use the same inclusive date range. The fixture models exact identifier counting, not Google's identity resolution, estimation or privacy systems. Thresholding/other-row flags are explicitly false in the fixture; absent sampling metadata maps to ratio 1.0 in this worker and does not independently establish unsampled real data.

## Connecting to real Google Analytics

Real reports require an authorized Google Analytics credential and accessible property. Follow the root README's documented secret or local OAuth flow, use Google's default API endpoints, and replace the synthetic property/metric selections deliberately. Do not reuse the fixture token or save real credentials or private report outputs in this notebook. The saved Run All workflow intentionally stays on the local synthetic API; it makes no claim of live Google validation.

Official references: [metric definitions](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema#metrics), [inclusive date ranges and property time zone](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/DateRange). This walkthrough is a grouping and pagination lesson, not a traffic, retention or conversion estimate.

The notebook creates its Haybarn connection with an empty temporary secret
directory and persistent secrets disabled. Its visible SQL creates only a
TEMPORARY dummy secret for the local fixture. Assertions check the initial empty
inventory and absence of persistent secrets before cleanup. Existing saved SQL
credentials are neither loaded nor modified.
