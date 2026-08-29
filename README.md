# Charlotte Freight-Train Early Warning System

An always-on FastAPI service that uses FRA grade-crossing metadata and TomTom
traffic-flow vector tiles to watch eight railroad sentinels around Charlotte,
Michigan. Validated crossing setup is checked into the repository. Current
runtime state is process-local and historical observations, events, hypotheses,
calibration, and usage checkpoints are appended to Google Sheets.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Set TOMTOM_API_KEY in .env before live initialization.
python -m train_tracker.cli init --live
uvicorn train_tracker.main:app --reload
```

Open `http://127.0.0.1:8000/`. The poller runs inside the web process when
`ENABLE_POLLER=1`; production must use exactly one web worker, or a separate
worker process.

## Configuration

See `.env.example`. The default TomTom endpoint is Traffic API v4 relative flow. Set
`TOMTOM_FLOW_ENDPOINT=orbis` only when the account is enabled for the Orbis
flow-tile API. `TOMTOM_FLOW_URL_TEMPLATE` can be used for a provider-compatible
endpoint without changing detector code.

`python -m train_tracker.cli init --live` is a local setup command. It resolves
all configured FRA IDs, automatically selects the higher-quality Battle Creek
candidate, tests tile coverage at zoom 16 through 18, and writes the validated
result to `config/validated_crossings.json`. Production startup loads that file
directly; it does not repeat broad FRA discovery. If a configured crossing later
loses live flow, it becomes UNKNOWN/DATA DEGRADED rather than being silently
replaced at runtime.

Google Sheets persistence follows the existing Curious Bot `pygsheets` service
account pattern. Set `TRAIN_TRACKER_SHEET_ID` and either
`GOOGLE_SERVICE_ACCOUNT_JSON` or `GOOGLE_SERVICE_ACCOUNT_FILE`. The service
creates/uses append-only tabs for traffic observations, crossing events, train
hypotheses, calibration, API usage, and an archive index. Monthly rotation uses
the archive index to create a new period spreadsheet without putting a
credential in the repository.

## Tests

```powershell
pytest -q
```

Tests use deterministic synthetic vector-tile-like features and mocked provider
responses; they do not need a TomTom key or Google credentials.

## Deployment

The `Procfile` uses one web process for FastAPI plus the background poller. No
Heroku Postgres add-on or release migration is required. Use the existing Google
service-account infrastructure for durable Sheets history. The dyno must still
remain available for the poller; deployment is intentionally a final step after
local and live validation.
