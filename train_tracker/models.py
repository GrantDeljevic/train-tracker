from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Crossing(Base):
    __tablename__ = "crossings"
    __table_args__ = (UniqueConstraint("fra_id", name="uq_crossings_fra_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fra_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    group_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    milepost: Mapped[float] = mapped_column(Float, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    railroad: Mapped[str | None] = mapped_column(String(16))
    subdivision: Mapped[str | None] = mapped_column(String(80))
    aadt: Mapped[int | None] = mapped_column(Integer)
    aadt_year: Mapped[int | None] = mapped_column(Integer)
    fra_revision_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role: Mapped[str] = mapped_column(String(16), default="backup", nullable=False)
    poll_interval_sec: Mapped[int] = mapped_column(Integer, default=240, nullable=False)
    phase_sec: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tile_zoom: Mapped[int | None] = mapped_column(Integer)
    tile_mapping_json: Mapped[dict | None] = mapped_column(JSON)
    coverage_score: Mapped[float | None] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_fra_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TrafficObservation(Base):
    __tablename__ = "traffic_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crossing_id: Mapped[int] = mapped_column(ForeignKey("crossings.id"), nullable=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    tile_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    traffic_level_min: Mapped[float | None] = mapped_column(Float)
    traffic_level_median: Mapped[float | None] = mapped_column(Float)
    directional_values: Mapped[dict | None] = mapped_column(JSON)
    road_coverage: Mapped[str | None] = mapped_column(String(32))
    road_closure: Mapped[bool | None] = mapped_column(Boolean)
    feature_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="UNKNOWN", nullable=False)
    anomaly_drop: Mapped[float | None] = mapped_column(Float)
    anomaly_score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(32))
    error_detail: Mapped[str | None] = mapped_column(Text)
    tile_key: Mapped[str | None] = mapped_column(String(80))


class CrossingEvent(Base):
    __tablename__ = "crossing_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crossing_id: Mapped[int] = mapped_column(ForeignKey("crossings.id"), nullable=False, index=True)
    event_time_estimate: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    event_time_low: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_time_high: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TrainHypothesis(Base):
    __tablename__ = "train_hypotheses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_crossing_id: Mapped[int | None] = mapped_column(ForeignKey("crossings.id"))
    last_milepost: Mapped[float | None] = mapped_column(Float)
    estimated_speed: Mapped[float | None] = mapped_column(Float)
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eta_low: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eta_high: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_level: Mapped[str] = mapped_column(String(24), nullable=False)
    source_group: Mapped[str] = mapped_column(String(64), nullable=False)
    event_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApiUsage(Base):
    __tablename__ = "api_usage"

    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    actual_request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    http_4xx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    http_429: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    http_5xx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    network_errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_dedupe_saves: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

