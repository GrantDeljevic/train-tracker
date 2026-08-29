"""Initial Charlotte freight warning schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crossings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fra_id", sa.String(16), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("group_name", sa.String(32), nullable=False),
        sa.Column("milepost", sa.Float(), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("railroad", sa.String(16)),
        sa.Column("subdivision", sa.String(80)),
        sa.Column("aadt", sa.Integer()),
        sa.Column("aadt_year", sa.Integer()),
        sa.Column("fra_revision_date", sa.DateTime(timezone=True)),
        sa.Column("role", sa.String(16), nullable=False, server_default="backup"),
        sa.Column("poll_interval_sec", sa.Integer(), nullable=False, server_default="240"),
        sa.Column("phase_sec", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tile_zoom", sa.Integer()),
        sa.Column("tile_mapping_json", sa.JSON()),
        sa.Column("coverage_score", sa.Float()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_fra_sync_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fra_id", name="uq_crossings_fra_id"),
    )
    op.create_index("ix_crossings_fra_id", "crossings", ["fra_id"])
    op.create_index("ix_crossings_group_name", "crossings", ["group_name"])
    op.create_table(
        "traffic_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("crossing_id", sa.Integer(), sa.ForeignKey("crossings.id"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tile_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("traffic_level_min", sa.Float()),
        sa.Column("traffic_level_median", sa.Float()),
        sa.Column("directional_values", sa.JSON()),
        sa.Column("road_coverage", sa.String(32)),
        sa.Column("road_closure", sa.Boolean()),
        sa.Column("feature_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("usable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("severity", sa.String(16), nullable=False, server_default="UNKNOWN"),
        sa.Column("anomaly_drop", sa.Float()),
        sa.Column("anomaly_score", sa.Float()),
        sa.Column("status", sa.String(32)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("tile_key", sa.String(80)),
    )
    op.create_index("ix_traffic_observations_crossing_id", "traffic_observations", ["crossing_id"])
    op.create_index("ix_traffic_observations_observed_at", "traffic_observations", ["observed_at"])
    op.create_table(
        "crossing_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("crossing_id", sa.Integer(), sa.ForeignKey("crossings.id"), nullable=False),
        sa.Column("event_time_estimate", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_time_low", sa.DateTime(timezone=True)),
        sa.Column("event_time_high", sa.DateTime(timezone=True)),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_crossing_events_crossing_id", "crossing_events", ["crossing_id"])
    op.create_index("ix_crossing_events_event_time_estimate", "crossing_events", ["event_time_estimate"])
    op.create_table(
        "train_hypotheses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_crossing_id", sa.Integer(), sa.ForeignKey("crossings.id")),
        sa.Column("last_milepost", sa.Float()),
        sa.Column("estimated_speed", sa.Float()),
        sa.Column("eta", sa.DateTime(timezone=True)),
        sa.Column("eta_low", sa.DateTime(timezone=True)),
        sa.Column("eta_high", sa.DateTime(timezone=True)),
        sa.Column("evidence_level", sa.String(24), nullable=False),
        sa.Column("source_group", sa.String(64), nullable=False),
        sa.Column("event_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_train_hypotheses_last_seen_at", "train_hypotheses", ["last_seen_at"])
    op.create_table(
        "api_usage",
        sa.Column("month", sa.String(7), primary_key=True),
        sa.Column("actual_request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("successful_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_4xx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_429", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_5xx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("network_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_dedupe_saves", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "system_state",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("system_state")
    op.drop_table("api_usage")
    op.drop_index("ix_train_hypotheses_last_seen_at", table_name="train_hypotheses")
    op.drop_table("train_hypotheses")
    op.drop_index("ix_crossing_events_event_time_estimate", table_name="crossing_events")
    op.drop_index("ix_crossing_events_crossing_id", table_name="crossing_events")
    op.drop_table("crossing_events")
    op.drop_index("ix_traffic_observations_observed_at", table_name="traffic_observations")
    op.drop_index("ix_traffic_observations_crossing_id", table_name="traffic_observations")
    op.drop_table("traffic_observations")
    op.drop_index("ix_crossings_group_name", table_name="crossings")
    op.drop_index("ix_crossings_fra_id", table_name="crossings")
    op.drop_table("crossings")

