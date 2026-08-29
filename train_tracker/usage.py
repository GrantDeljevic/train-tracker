from __future__ import annotations

import calendar
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import utc_now
from .models import ApiUsage, Crossing


def usage_month(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y-%m")


class UsageService:
    def __init__(self, session_factory, hard_budget: int | None = None, soft_budget: int | None = None):
        self.session_factory = session_factory
        self.hard_budget = hard_budget if hard_budget is not None else settings.monthly_request_budget
        self.soft_budget = soft_budget if soft_budget is not None else settings.soft_request_budget

    def _row(self, session: Session, month: str | None = None) -> ApiUsage:
        month = month or usage_month()
        row = session.get(ApiUsage, month)
        if row is None:
            row = ApiUsage(month=month, updated_at=utc_now())
            session.add(row)
            session.flush()
        return row

    def record(self, status_code: int | None, kind: str) -> None:
        with self.session_factory() as session:
            row = self._row(session)
            if kind == "cache":
                row.cache_dedupe_saves += 1
            else:
                row.actual_request_count += 1
                if kind == "network":
                    row.network_errors += 1
                elif status_code is not None and 200 <= status_code < 300:
                    row.successful_requests += 1
                elif status_code == 429:
                    row.http_429 += 1
                elif status_code is not None and 400 <= status_code < 500:
                    row.http_4xx += 1
                elif status_code is not None and status_code >= 500:
                    row.http_5xx += 1
            row.updated_at = utc_now()
            session.commit()

    def allowed(self) -> bool:
        with self.session_factory() as session:
            return self._row(session).actual_request_count < self.hard_budget

    def projected_monthly_requests(self, session: Session | None = None) -> int:
        if session is None:
            with self.session_factory() as owned_session:
                return self.projected_monthly_requests(owned_session)
        else:
            crossings = list(session.scalars(select(Crossing).where(Crossing.enabled.is_(True))).all())
            days = calendar.monthrange(datetime.now(timezone.utc).year, datetime.now(timezone.utc).month)[1]
            total = 0.0
            for crossing in crossings:
                interval = max(60, crossing.poll_interval_sec or 240)
                tiles = len((crossing.tile_mapping_json or {}).get("tiles", [])) or 1
                total += (days * 24 * 3600 / interval) * tiles
            return round(total)

    def snapshot(self) -> dict:
        with self.session_factory() as session:
            row = self._row(session)
            result = {
                "month": row.month,
                "actual_request_count": row.actual_request_count,
                "successful_requests": row.successful_requests,
                "http_4xx": row.http_4xx,
                "http_429": row.http_429,
                "http_5xx": row.http_5xx,
                "network_errors": row.network_errors,
                "cache_dedupe_saves": row.cache_dedupe_saves,
                "soft_budget": self.soft_budget,
                "hard_budget": self.hard_budget,
                "projected_normal_requests": self.projected_monthly_requests(session),
            }
            session.commit()
            return result
