from __future__ import annotations

import asyncio
import hmac
import logging
import math
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select

from .config import settings
from .crossings import TARGET_FRA_ID, TARGET_MILEPOST, TARGET_NAME, load_static_configuration
from .db import SessionLocal, init_db, session_scope, utc_now
from .models import Crossing, CrossingEvent, SystemState, TrainHypothesis, TrafficObservation
from .scheduler import PollScheduler
from .sheets import GoogleSheetsArchive
from .tomtom import TomTomClient
from .usage import UsageService, usage_month

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="Charlotte Freight-Train Early Warning", version="0.1.0")
poll_scheduler: PollScheduler | None = None
poll_task: asyncio.Task | None = None
tomtom_client: TomTomClient | None = None
sheets_archive: GoogleSheetsArchive | None = None
poll_request_lock = asyncio.Lock()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value else None


def _minutes_until(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, math.ceil((_aware(value) - now).total_seconds() / 60))


def _valid_poll_trigger(request: Request) -> bool:
    expected_token = settings.poll_trigger_token
    if expected_token:
        supplied = request.headers.get("x-train-tracker-token", "")
        return hmac.compare_digest(supplied, expected_token)

    audience = settings.poll_trigger_audience
    expected_email = settings.poll_trigger_service_account_email
    authorization = request.headers.get("authorization", "")
    if not audience or not expected_email or not authorization.lower().startswith("bearer "):
        return False
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(
            authorization.split(" ", 1)[1],
            google_requests.Request(),
            audience=audience,
        )
        return claims.get("email") == expected_email
    except Exception:
        LOGGER.warning("Cloud Scheduler OIDC validation failed", exc_info=True)
        return False


@app.on_event("startup")
async def startup() -> None:
    init_db()
    with session_scope() as session:
        load_static_configuration(session)
    global poll_scheduler, poll_task, tomtom_client, sheets_archive
    sheets_archive = GoogleSheetsArchive()
    sheets_archive.connect()
    usage = UsageService(SessionLocal)
    restored_usage = sheets_archive.load_usage(usage_month()) if sheets_archive.connected else None
    if restored_usage:
        usage.restore(usage_month(), restored_usage)
        LOGGER.info("restored TomTom usage checkpoint for %s", usage_month())
    if settings.enable_poller or settings.serverless_polling:
        tomtom_client = TomTomClient(
            usage_callback=usage.record,
            request_guard=usage.allowed,
        )
        poll_scheduler = PollScheduler(
            tomtom_client,
            usage_service=usage,
            archive=sheets_archive,
            initial_poll_all=settings.serverless_polling,
        )
        if sheets_archive.connected:
            poll_scheduler.restore_runtime_state(sheets_archive.load_runtime_state())
        if settings.serverless_polling:
            LOGGER.info("poller configured for one-shot Cloud Run invocations")
        else:
            poll_task = asyncio.create_task(poll_scheduler.run_forever())
            LOGGER.info("poller started with one in-process scheduler")
    else:
        LOGGER.info("poller disabled by ENABLE_POLLER")


@app.on_event("shutdown")
async def shutdown() -> None:
    global poll_task, tomtom_client, sheets_archive
    if poll_scheduler:
        await poll_scheduler.stop()
    if poll_task:
        await poll_task
    if poll_scheduler and sheets_archive:
        await asyncio.to_thread(poll_scheduler.flush_archive_if_due, force=True)
    if tomtom_client:
        tomtom_client.close()


def _latest_observation(session, crossing_id: int) -> TrafficObservation | None:
    return session.scalar(select(TrafficObservation).where(TrafficObservation.crossing_id == crossing_id).order_by(desc(TrafficObservation.observed_at)).limit(1))


def _latest_valid_by_group(session) -> dict[str, TrafficObservation]:
    result: dict[str, TrafficObservation] = {}
    crossings = {crossing.id: crossing for crossing in session.scalars(select(Crossing).where(Crossing.enabled.is_(True))).all()}
    observations = session.scalars(select(TrafficObservation).where(TrafficObservation.usable.is_(True)).order_by(desc(TrafficObservation.observed_at))).all()
    for observation in observations:
        group = crossings.get(observation.crossing_id).group_name if crossings.get(observation.crossing_id) else None
        if group and group not in result:
            result[group] = observation
    return result


def _hypothesis_payload(session, row: TrainHypothesis, now: datetime) -> dict:
    crossing = session.get(Crossing, row.last_crossing_id) if row.last_crossing_id else None
    return {
        "id": row.id,
        "direction": row.direction,
        "status": row.status,
        "evidence": row.evidence_level,
        "source_group": row.source_group,
        "first_seen_at": _iso(row.first_seen_at),
        "last_seen_at": _iso(row.last_seen_at),
        "last_crossing": crossing.name if crossing else None,
        "last_crossing_fra_id": crossing.fra_id if crossing else None,
        "last_milepost": row.last_milepost,
        "estimated_speed_mph": row.estimated_speed,
        "eta": _iso(row.eta),
        "eta_low": _iso(row.eta_low),
        "eta_high": _iso(row.eta_high),
        "eta_minutes": _minutes_until(row.eta, now),
        "eta_low_minutes": _minutes_until(row.eta_low, now),
        "eta_high_minutes": _minutes_until(row.eta_high, now),
        "event_ids": row.event_ids,
    }


@app.post("/internal/poll")
async def internal_poll(request: Request) -> dict:
    """Run one scheduled polling cycle for Cloud Scheduler."""
    if not settings.serverless_polling:
        raise HTTPException(status_code=404, detail="scheduled polling is disabled")
    if not settings.poll_trigger_token and not (settings.poll_trigger_audience and settings.poll_trigger_service_account_email):
        raise HTTPException(status_code=503, detail="poll trigger authentication is not configured")
    if not _valid_poll_trigger(request):
        raise HTTPException(status_code=401, detail="invalid poll trigger token")
    if poll_scheduler is None:
        raise HTTPException(status_code=503, detail="poller is not initialized")
    async with poll_request_lock:
        count = await asyncio.to_thread(poll_scheduler.poll_due)
        flushed = await asyncio.to_thread(poll_scheduler.flush_archive_if_due, force=True)
        return {
            "ok": True,
            "polled_crossings": count,
            "archive_flushed": flushed,
            "last_run": _iso(poll_scheduler.last_run),
            "last_error": poll_scheduler.last_error,
        }


@app.get("/api/status")
def api_status() -> dict:
    now = utc_now()
    try:
        with SessionLocal() as session:
            crossings = list(session.scalars(select(Crossing).where(Crossing.enabled.is_(True))).all())
            latest_valid = _latest_valid_by_group(session)
            active = list(session.scalars(select(TrainHypothesis).where(TrainHypothesis.status.in_(["POSSIBLE", "APPROACHING", "HIGH_CONFIDENCE"])).order_by(TrainHypothesis.eta_low)).all())
            valid_groups = {}
            for group in ("Battle Creek", "Lansing", "Durand"):
                observation = latest_valid.get(group)
                age = (now - _aware(observation.observed_at)).total_seconds() if observation else None
                valid_groups[group] = {"latest_valid_at": _iso(observation.observed_at) if observation else None, "age_seconds": age, "fresh": age is not None and age <= 10 * 60}
            historical_health = sheets_archive.health() if sheets_archive else {"configured": False, "required": False, "connected": False}
            data_degraded = any(not value["fresh"] for value in valid_groups.values()) if crossings else True
            if historical_health.get("required") and not historical_health.get("healthy", historical_health.get("connected")):
                data_degraded = True
            provider_error = ((settings.enable_poller or settings.serverless_polling) and not settings.tomtom_api_key) or any((latest := _latest_observation(session, crossing.id)) is not None and latest.status == "ERROR" for crossing in crossings)
            if provider_error:
                state = "DATA DEGRADED"
            elif active:
                best = active[0]
                if best.source_group == "Durand" and best.direction == "from_durand":
                    state = "EARLY WARNING"
                elif best.status in {"APPROACHING", "HIGH_CONFIDENCE"}:
                    state = "TRAIN APPROACHING"
                else:
                    state = "POSSIBLE TRAIN"
            elif data_degraded:
                state = "DATA DEGRADED"
            else:
                state = "CLEAR"
            hypotheses = [_hypothesis_payload(session, row, now) for row in active]
            primary = hypotheses[0] if hypotheses else None
            health = session.get(SystemState, "poller")
            usage = UsageService(SessionLocal).snapshot()
            target_state = session.get(SystemState, "target_metadata")
            target_json = target_state.value_json if target_state else {"name": TARGET_NAME, "fra_id": TARGET_FRA_ID, "milepost": TARGET_MILEPOST}
            return {
                "state": state,
                "target": target_json,
                "eta": {"minutes": primary["eta_minutes"], "low_minutes": primary["eta_low_minutes"], "high_minutes": primary["eta_high_minutes"]} if primary else None,
                "direction": primary["direction"] if primary else None,
                "last_detected": {"crossing": primary["last_crossing"], "at": primary["last_seen_at"]} if primary else None,
                "evidence": primary["evidence"] if primary else "NONE",
                "updated_at": _iso(now),
                "groups": valid_groups,
                "hypotheses": hypotheses,
                "system": {
                    "poller_enabled": settings.enable_poller,
                    "poller_mode": "scheduled" if settings.serverless_polling else "continuous",
                    "poller_last_run": (health.value_json or {}).get("last_run") if health else None,
                    "poller_error": (health.value_json or {}).get("last_error") if health else None,
                    "api_key_configured": bool(settings.tomtom_api_key),
                    "runtime_state_backend": "process-memory",
                    "historical_persistence": historical_health,
                    "usage": usage,
                },
            }
    except Exception as exc:
        LOGGER.exception("status query failed")
        raise HTTPException(status_code=503, detail=f"runtime status unavailable: {exc}") from exc


@app.get("/api/crossings")
def api_crossings() -> list[dict]:
    with SessionLocal() as session:
        rows = []
        for crossing in session.scalars(select(Crossing).where(Crossing.enabled.is_(True)).order_by(Crossing.group_name, Crossing.milepost)).all():
            latest = _latest_observation(session, crossing.id)
            rows.append({
                "id": crossing.id, "fra_id": crossing.fra_id, "name": crossing.name, "group": crossing.group_name,
                "milepost": crossing.milepost, "role": crossing.role, "aadt": crossing.aadt, "aadt_year": crossing.aadt_year,
                "coverage_score": crossing.coverage_score, "poll_interval_sec": crossing.poll_interval_sec,
                "latest": {"observed_at": _iso(latest.observed_at), "traffic_level": latest.traffic_level_median, "severity": latest.severity, "usable": latest.usable, "status": latest.status} if latest else None,
            })
        return rows


@app.get("/api/events")
def api_events(hours: int = Query(default=24, ge=1, le=168)) -> list[dict]:
    cutoff = utc_now() - timedelta(hours=hours)
    with SessionLocal() as session:
        crossings = {crossing.id: crossing for crossing in session.scalars(select(Crossing)).all()}
        rows = session.scalars(select(CrossingEvent).where(CrossingEvent.event_time_estimate >= cutoff).order_by(desc(CrossingEvent.event_time_estimate))).all()
        return [{"id": row.id, "crossing": crossings.get(row.crossing_id).name if row.crossing_id in crossings else None, "fra_id": crossings.get(row.crossing_id).fra_id if row.crossing_id in crossings else None, "event_time": _iso(row.event_time_estimate), "low": _iso(row.event_time_low), "high": _iso(row.event_time_high), "severity": row.severity, "evidence": row.evidence_json} for row in rows]


@app.get("/api/usage")
def api_usage() -> dict:
    return UsageService(SessionLocal).snapshot()


@app.get("/healthz")
def healthz() -> dict:
    try:
        with SessionLocal() as session:
            session.execute(select(1)).scalar_one()
        historical = sheets_archive.health() if sheets_archive else {"configured": False, "required": False, "connected": False, "healthy": False}
        ok = not historical.get("required") or historical.get("healthy", historical.get("connected"))
        response = {
            "ok": ok,
            "runtime_state": "sheets-snapshot" if settings.serverless_polling else "memory",
            "historical_persistence": historical,
            "poller": "scheduled" if settings.serverless_polling else ("running" if settings.enable_poller else "disabled"),
            "time": _iso(utc_now()),
        }
        if not ok:
            raise HTTPException(status_code=503, detail=response)
        return response
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail=f"runtime state unavailable: {exc}") from exc


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Charlotte Freight Warning</title>
<style>
:root{color-scheme:dark;--bg:#10151b;--panel:#19222c;--muted:#9aaabd;--green:#43d17c;--amber:#f4bd4f;--red:#ff6f6f;--blue:#74b9ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#f6f8fb;font:16px system-ui,-apple-system,Segoe UI,sans-serif}main{max-width:920px;margin:auto;padding:18px}.card{background:var(--panel);border:1px solid #2b3947;border-radius:14px;padding:18px;margin:12px 0}.hero{display:flex;align-items:center;justify-content:space-between;gap:16px}.state{font-size:clamp(2rem,8vw,4.3rem);font-weight:800;letter-spacing:.02em}.clear{color:var(--green)}.approach{color:var(--red)}.early,.possible{color:var(--amber)}.degraded{color:var(--blue)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.label{color:var(--muted);font-size:.82rem;text-transform:uppercase;letter-spacing:.08em}.value{font-size:1.22rem;margin-top:4px}.small{color:var(--muted);font-size:.9rem}.pill{display:inline-block;border-radius:999px;padding:4px 9px;background:#273646}.group{border-left:4px solid var(--green);padding-left:10px}.group.stale{border-color:var(--amber)}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px 5px;border-bottom:1px solid #2b3947}th{color:var(--muted);font-size:.8rem;text-transform:uppercase}.error{color:var(--amber)}
</style></head><body><main><section class="card"><div class="hero"><div><div id="state" class="state">Loading…</div><div class="small">Lawrence St / M-79 · Charlotte, Michigan · FRA 283602W</div></div><div id="updated" class="small">—</div></div><div id="details" class="grid" style="margin-top:18px"></div></section>
<section class="card"><h2>Sentinel health</h2><div id="groups" class="grid"></div><p id="health" class="small">—</p></section>
<section class="card"><h2>Recent detected events</h2><div id="events" class="small">Loading…</div></section>
<section class="card"><h2>Usage</h2><div id="usage" class="small">Loading…</div></section>
<p class="small">Auto-refreshes every 12 seconds. Times are shown in America/Detroit.</p></main>
<script>
const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const title=s=>s.replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
function stateClass(s){return s.includes('DEGRADED')?'degraded':s.includes('APPROACHING')?'approach':s.includes('EARLY')||s.includes('POSSIBLE')?'early':'clear'}
function minutes(h){return h==null?'—':`${h} min`}
async function refresh(){try{const responses=await Promise.all([fetch('/api/status'),fetch('/api/events?hours=24'),fetch('/api/usage'),fetch('/api/crossings')]);if(responses.some(r=>!r.ok))throw new Error('one or more dashboard API requests failed');const [s,e,u,c]=responses;const status=await s.json(),events=await e.json(),usage=await u.json(),crossings=await c.json();
const st=document.querySelector('#state');st.textContent=esc(status.state);st.className='state '+stateClass(status.state);document.querySelector('#updated').textContent='Updated '+new Date(status.updated_at).toLocaleTimeString();const p=status.hypotheses?.[0];document.querySelector('#details').innerHTML=`<div><div class="label">ETA</div><div class="value">${p?`${minutes(p.eta_low_minutes)}–${minutes(p.eta_high_minutes)}`:'—'}</div></div><div><div class="label">Direction</div><div class="value">${p?esc(title(p.direction)):'—'}</div></div><div><div class="label">Last detected</div><div class="value">${p?esc(p.last_crossing):'—'}</div></div><div><div class="label">Evidence</div><div class="value"><span class="pill">${esc(p?.evidence||status.evidence)}</span></div></div>`;
document.querySelector('#groups').innerHTML=Object.entries(status.groups).map(([g,v])=>`<div class="group ${v.fresh?'':'stale'}"><div class="label">${esc(g)}</div><div class="value">${v.latest_valid_at?new Date(v.latest_valid_at).toLocaleTimeString():'No valid data'}</div><div class="small">${v.fresh?'Fresh':'Stale / unknown'}</div></div>`).join('');
const archive=status.system.historical_persistence;document.querySelector('#health').textContent=`Poller: ${status.system.poller_enabled?'enabled':'disabled'} · API key: ${status.system.api_key_configured?'configured':'missing'} · ${status.system.poller_error||'no recent poller error'}${archive&&!archive.healthy?' · History: degraded':''}`;
document.querySelector('#events').innerHTML=events.length?'<table><tr><th>Time</th><th>Crossing</th><th>Severity</th></tr>'+events.slice(0,12).map(x=>`<tr><td>${new Date(x.event_time).toLocaleString()}</td><td>${esc(x.crossing)}</td><td>${esc(x.severity)}</td></tr>`).join('')+'</table>':'No crossing events in the last 24 hours.';
document.querySelector('#usage').innerHTML=`${usage.actual_request_count.toLocaleString()} actual requests this month · ${usage.cache_dedupe_saves.toLocaleString()} cache/dedupe saves · projected normal ${usage.projected_normal_requests.toLocaleString()} / hard ${usage.hard_budget.toLocaleString()}`;
}catch(err){document.querySelector('#state').textContent='DATA DEGRADED';document.querySelector('#state').className='state degraded';document.querySelector('#health').textContent='Dashboard request failed: '+err}}
refresh();setInterval(refresh,12000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML
