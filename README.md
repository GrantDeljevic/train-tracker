# Charlotte Freight-Train Early Warning System

An always-on FastAPI service that uses FRA grade-crossing metadata and TomTom
traffic-flow vector tiles to watch eight railroad sentinels around Charlotte,
Michigan. It stores raw traffic observations, inferred crossing events, and
train hypotheses in PostgreSQL (SQLite is convenient for local development).

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Set TOMTOM_API_KEY in .env before live initialization.
python -m train_tracker.cli init --live
alembic upgrade head
uvicorn train_tracker.main:app --reload
```

Open `http://127.0.0.1:8000/`. The poller runs inside the web process when
`ENABLE_POLLER=1`; production must use exactly one web worker, or a separate
worker process.

## Configuration

See `.env.example`. `DATABASE_URL` may be PostgreSQL or SQLite. The default
TomTom endpoint is Traffic API v4 relative flow. Set
`TOMTOM_FLOW_ENDPOINT=orbis` only when the account is enabled for the Orbis
flow-tile API. `TOMTOM_FLOW_URL_TEMPLATE` can be used for a provider-compatible
endpoint without changing detector code.

`python -m train_tracker.cli init --live` resolves all configured FRA IDs,
automatically selects the higher-quality Battle Creek candidate, tests tile
coverage at zoom 16 through 18, and persists the selected tile/road metadata.
Without `--live`, FRA metadata is still refreshed, but live coverage is left
unknown and the service reports degraded data until a live initialization is
performed.

## Tests

```powershell
pytest -q
```

Tests use deterministic synthetic vector-tile-like features and mocked provider
responses; they do not need a TomTom key or PostgreSQL.

## Deployment

The `Procfile` uses a release phase for migrations and a single web process for
FastAPI plus the background poller. Use a non-sleeping dyno and PostgreSQL.
Deployment is intentionally a final step after local and live validation.

