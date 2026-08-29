from __future__ import annotations

import json
import logging
import base64
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import BASE_DIR, Settings, settings

LOGGER = logging.getLogger(__name__)

OBSERVATION_TAB = "Traffic Observations"
EVENT_TAB = "Crossing Events"
HYPOTHESIS_TAB = "Train Hypotheses"
CALIBRATION_TAB = "Calibration"
USAGE_TAB = "API Usage"
INDEX_TAB = "Archive Index"
RUNTIME_TAB = "Runtime State"

TAB_HEADERS: dict[str, list[str]] = {
    OBSERVATION_TAB: [
        "recorded_at", "crossing_fra_id", "crossing_name", "group", "milepost", "observed_at",
        "tile_fetched_at", "traffic_level_min", "traffic_level_median", "directional_values_json",
        "road_coverage", "road_closure", "feature_count", "usable", "severity", "anomaly_drop",
        "anomaly_score", "status", "error_detail", "tile_key",
    ],
    EVENT_TAB: [
        "recorded_at", "event_id", "crossing_fra_id", "crossing_name", "group", "milepost",
        "event_time_estimate", "event_time_low", "event_time_high", "severity", "evidence_json",
    ],
    HYPOTHESIS_TAB: [
        "recorded_at", "hypothesis_id", "direction", "status", "evidence_level", "source_group",
        "first_seen_at", "last_seen_at", "last_crossing_fra_id", "last_milepost", "estimated_speed_mph",
        "eta", "eta_low", "eta_high", "event_ids_json",
    ],
    CALIBRATION_TAB: [
        "recorded_at", "crossing_fra_id", "crossing_name", "group", "window_days", "observation_count",
        "valid_flow_percentage", "anomaly_frequency", "isolated_anomaly_rate", "typical_baseline",
        "direction_confirmed_sequences", "hourly_usefulness_json",
    ],
    USAGE_TAB: [
        "recorded_at", "month", "actual_request_count", "successful_requests", "http_4xx", "http_429",
        "http_5xx", "network_errors", "cache_dedupe_saves", "projected_normal_requests", "soft_budget", "hard_budget",
    ],
}
INDEX_HEADERS = ["period", "spreadsheet_id", "title", "created_at"]
RUNTIME_HEADERS = ["updated_at", "state_json"]


class SheetsError(RuntimeError):
    pass


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.isoformat()
    return str(value)


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, separators=(",", ":"), sort_keys=True, default=_iso)


def _number(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class GoogleSheetsArchive:
    """Append-only historical archive backed by Google Sheets.

    Authentication follows Curious Bot's existing pygsheets pattern when a
    service-account file is available, while production can safely use a
    Heroku config var containing the JSON credential.  No credential belongs
    in this repository.
    """

    def __init__(self, settings_obj: Settings = settings, client: Any | None = None):
        self.settings = settings_obj
        self.client = client
        self.connected = False
        self.required = settings_obj.sheets_required
        self.last_error: str | None = None
        self.last_flush_at: datetime | None = None
        self.last_rotation: str | None = None
        self._base_spreadsheet: Any | None = None
        self._active_spreadsheet: Any | None = None
        self._worksheets: dict[str, Any] = {}
        self._queues: dict[str, list[list[Any]]] = {tab: [] for tab in TAB_HEADERS}
        self._last_usage_signature: tuple[Any, ...] | None = None
        self._hypothesis_signatures: dict[str, tuple[Any, ...]] = {}
        self._consecutive_flush_failures = 0
        self._next_retry_at: datetime | None = None
        self._dropped_rows = 0
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return bool(self.settings.sheets_spreadsheet_id)

    def _authorize(self) -> Any:
        try:
            import pygsheets
        except ImportError as error:
            raise SheetsError("pygsheets is required for Google Sheets persistence") from error

        service_json = self.settings.sheets_service_account_json
        if not service_json and self.settings.sheets_service_account_json_b64:
            try:
                service_json = base64.b64decode(self.settings.sheets_service_account_json_b64).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as error:
                raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 is not valid base64 JSON") from error
        if service_json:
            try:
                json.loads(service_json)
                return pygsheets.authorize(service_account_json=service_json)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid service-account JSON") from error

        service_file = self.settings.sheets_service_account_file
        path = Path(service_file) if service_file else BASE_DIR / "client_secret.json"
        if not path.exists():
            raise SheetsError(
                "Google Sheets is configured but no service-account credential was found; "
                "set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE"
            )
        return pygsheets.authorize(service_account_file=str(path))

    @staticmethod
    def _period(now: datetime, rotation: str) -> str:
        if rotation == "quarterly":
            return f"{now.year}-Q{((now.month - 1) // 3) + 1}"
        return f"{now.year}-{now.month:02d}"

    @staticmethod
    def _column_label(column: int) -> str:
        if column < 1:
            raise ValueError("column must be positive")
        labels: list[str] = []
        while column:
            column, remainder = divmod(column - 1, 26)
            labels.append(chr(ord("A") + remainder))
        return "".join(reversed(labels))

    @classmethod
    def _write_rows(
        cls,
        worksheet: Any,
        start_row: int,
        rows: list[list[Any]],
        expected_cols: int,
    ) -> None:
        """Write a rectangular range without pygsheets' single-cell off-by-one.

        pygsheets infers the end cell incorrectly when ``update_values`` is
        given a single-cell start address.  Supplying the complete range also
        makes the write safe for existing tabs whose grid has not been grown
        beyond its original 100 rows.
        """
        if not rows:
            return
        if expected_cols < 1:
            raise ValueError("expected_cols must be positive")
        matrix: list[list[Any]] = []
        for row in rows:
            if len(row) > expected_cols:
                raise SheetsError(
                    f"row has {len(row)} columns but the worksheet expects {expected_cols}"
                )
            matrix.append(list(row) + [""] * (expected_cols - len(row)))

        end_row = start_row + len(matrix) - 1
        current_rows = int(getattr(worksheet, "rows", 0) or 0)
        current_cols = int(getattr(worksheet, "cols", 0) or 0)
        required_rows = max(current_rows, end_row)
        required_cols = max(current_cols, expected_cols)
        if required_rows > current_rows or required_cols > current_cols:
            resize = getattr(worksheet, "resize", None)
            if resize is not None:
                resize(rows=required_rows, cols=required_cols)

        end_col = cls._column_label(expected_cols)
        worksheet.update_values(f"A{start_row}:{end_col}{end_row}", matrix)

    @classmethod
    def _worksheet(cls, spreadsheet: Any, title: str, headers: list[str], pygsheets_module: Any) -> Any:
        try:
            worksheet = spreadsheet.worksheet_by_title(title)
        except pygsheets_module.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=title, rows=100, cols=len(headers))
        values = worksheet.get_all_values(
            include_tailing_empty_rows=False,
            include_tailing_empty=False,
            returnas="matrix",
        )
        if not values or not any(str(value).strip() for value in values[0]):
            cls._write_rows(worksheet, 1, [headers], len(headers))
        elif list(values[0][: len(headers)]) != headers:
            raise SheetsError(f"Google Sheet tab {title!r} has unexpected headers; append stopped")
        return worksheet

    def _ensure_tabs(self, spreadsheet: Any) -> None:
        try:
            import pygsheets
        except ImportError as error:
            raise SheetsError("pygsheets is required for Google Sheets persistence") from error
        self._worksheets = {
            title: self._worksheet(spreadsheet, title, headers, pygsheets)
            for title, headers in TAB_HEADERS.items()
        }
        self._worksheets[RUNTIME_TAB] = self._worksheet(spreadsheet, RUNTIME_TAB, RUNTIME_HEADERS, pygsheets)

    def _index_worksheet(self) -> Any:
        try:
            import pygsheets
        except ImportError as error:
            raise SheetsError("pygsheets is required for Google Sheets persistence") from error
        return self._worksheet(self._base_spreadsheet, INDEX_TAB, INDEX_HEADERS, pygsheets)

    @classmethod
    def _append_rows(cls, worksheet: Any, rows: list[list[Any]], expected_cols: int) -> None:
        """Append using an explicit next row.

        pygsheets' append-table response parser can mis-handle tab names with
        spaces even after Google has accepted the write.  An explicit range is
        both append-only for this single writer and avoids false retry/duplicate
        decisions caused by that parser behavior.
        """
        if not rows:
            return
        existing = worksheet.get_all_values(
            include_tailing_empty_rows=False,
            include_tailing_empty=False,
            returnas="matrix",
        )
        start_row = len(existing) + 1
        cls._write_rows(worksheet, start_row, rows, expected_cols)

    def _open_period(self, now: datetime) -> None:
        period = self._period(now, self.settings.sheets_rotation)
        index = self._index_worksheet()
        values = index.get_all_values(include_tailing_empty_rows=False, include_tailing_empty=False, returnas="matrix")
        matching = next((row for row in values[1:] if len(row) >= 2 and row[0] == period and row[1]), None)
        if matching:
            self._active_spreadsheet = self.client.open_by_key(matching[1])
        else:
            base_id = self.settings.sheets_spreadsheet_id
            title = f"Charlotte Freight Train Warning {period}"
            if not values or len(values) == 1:
                self._active_spreadsheet = self._base_spreadsheet
                self._append_rows(index, [[period, base_id, title, _iso(now)]], len(INDEX_HEADERS))
            else:
                self._active_spreadsheet = self.client.create(title)
                spreadsheet_id = getattr(self._active_spreadsheet, "id", "")
                self._append_rows(index, [[period, spreadsheet_id, title, _iso(now)]], len(INDEX_HEADERS))
        self._ensure_tabs(self._active_spreadsheet)
        self.last_rotation = period

    def connect(self) -> bool:
        with self._lock:
            self.last_error = None
            if not self.configured:
                self.connected = False
                self.last_error = "TRAIN_TRACKER_SHEET_ID is not configured"
                return False
            try:
                if self.client is None:
                    self.client = self._authorize()
                self._base_spreadsheet = self.client.open_by_key(self.settings.sheets_spreadsheet_id)
                self._open_period(datetime.now(timezone.utc))
                self.connected = True
                return True
            except Exception as error:
                self.connected = False
                self.last_error = str(error)[:500]
                LOGGER.exception("Google Sheets persistence unavailable: %s", error)
                return False

    def health(self) -> dict[str, Any]:
        with self._lock:
            queued = sum(len(rows) for rows in self._queues.values())
            return {
                "configured": self.configured,
                "required": self.required,
                "connected": self.connected,
                "healthy": self.connected and self.last_error is None and self._dropped_rows == 0,
                "queued_rows": queued,
                "dropped_rows": self._dropped_rows,
                "last_flush_at": _iso(self.last_flush_at) if self.last_flush_at else None,
                "last_rotation": self.last_rotation,
                "last_error": self.last_error,
                "next_retry_at": _iso(self._next_retry_at) if self._next_retry_at else None,
            }

    def _enqueue(self, tab: str, row: list[Any]) -> None:
        if not self.connected:
            return
        with self._lock:
            max_pending = max(1, int(self.settings.sheets_max_pending_rows))
            queued = sum(len(rows) for rows in self._queues.values())
            if queued >= max_pending:
                # Preserve the newest observation/event while bounding memory
                # during a prolonged Sheets outage.  The loss is explicit in
                # health/logs; normal operation never reaches this path.
                oldest_tab = next((name for name, rows in self._queues.items() if rows), None)
                if oldest_tab is not None:
                    self._queues[oldest_tab].pop(0)
                    self._dropped_rows += 1
                self.last_error = (
                    f"Google Sheets pending queue reached {max_pending} rows; "
                    "oldest pending row was dropped"
                )
                LOGGER.error("Google Sheets pending queue full; dropping oldest pending row")
            self._queues[tab].append(row)

    def enqueue_observation(self, payload: Mapping[str, Any]) -> None:
        self._enqueue(OBSERVATION_TAB, [
            _iso(payload.get("recorded_at")), payload.get("crossing_fra_id", ""), payload.get("crossing_name", ""),
            payload.get("group", ""), payload.get("milepost", ""), _iso(payload.get("observed_at")),
            _iso(payload.get("tile_fetched_at")), payload.get("traffic_level_min", ""), payload.get("traffic_level_median", ""),
            _json(payload.get("directional_values")), payload.get("road_coverage", ""), payload.get("road_closure", ""),
            payload.get("feature_count", 0), payload.get("usable", False), payload.get("severity", "UNKNOWN"),
            payload.get("anomaly_drop", ""), payload.get("anomaly_score", ""), payload.get("status", ""),
            payload.get("error_detail", ""), payload.get("tile_key", ""),
        ])

    def enqueue_event(self, payload: Mapping[str, Any]) -> None:
        self._enqueue(EVENT_TAB, [
            _iso(payload.get("recorded_at")), payload.get("event_id", ""), payload.get("crossing_fra_id", ""),
            payload.get("crossing_name", ""), payload.get("group", ""), payload.get("milepost", ""),
            _iso(payload.get("event_time_estimate")), _iso(payload.get("event_time_low")), _iso(payload.get("event_time_high")),
            payload.get("severity", ""), _json(payload.get("evidence_json")),
        ])

    def enqueue_hypothesis(self, payload: Mapping[str, Any]) -> None:
        identity = str(payload.get("hypothesis_id", ""))
        signature = (
            payload.get("status"), payload.get("direction"), payload.get("last_seen_at"),
            payload.get("last_milepost"), payload.get("eta_low"), payload.get("eta_high"),
        )
        with self._lock:
            if identity and self._hypothesis_signatures.get(identity) == signature:
                return
            if identity:
                self._hypothesis_signatures[identity] = signature
        self._enqueue(HYPOTHESIS_TAB, [
            _iso(payload.get("recorded_at")), payload.get("hypothesis_id", ""), payload.get("direction", ""),
            payload.get("status", ""), payload.get("evidence_level", ""), payload.get("source_group", ""),
            _iso(payload.get("first_seen_at")), _iso(payload.get("last_seen_at")), payload.get("last_crossing_fra_id", ""),
            payload.get("last_milepost", ""), payload.get("estimated_speed_mph", ""), _iso(payload.get("eta")),
            _iso(payload.get("eta_low")), _iso(payload.get("eta_high")), _json(payload.get("event_ids")),
        ])

    def enqueue_calibration(self, payload: Mapping[str, Any]) -> None:
        self._enqueue(CALIBRATION_TAB, [
            _iso(payload.get("recorded_at")), payload.get("crossing_fra_id", ""), payload.get("crossing_name", ""),
            payload.get("group", ""), payload.get("window_days", ""), payload.get("observation_count", ""),
            payload.get("valid_flow_percentage", ""), payload.get("anomaly_frequency", ""),
            payload.get("isolated_anomaly_rate", ""), payload.get("typical_baseline", ""),
            payload.get("direction_confirmed_sequences", ""), _json(payload.get("hourly_usefulness")),
        ])

    def enqueue_usage(self, payload: Mapping[str, Any]) -> None:
        signature = tuple(payload.get(key) for key in (
            "month", "actual_request_count", "successful_requests", "http_4xx", "http_429", "http_5xx",
            "network_errors", "cache_dedupe_saves", "projected_normal_requests",
        ))
        with self._lock:
            if self._last_usage_signature == signature:
                return
            self._last_usage_signature = signature
        self._enqueue(USAGE_TAB, [
            _iso(payload.get("recorded_at")), payload.get("month", ""), payload.get("actual_request_count", 0),
            payload.get("successful_requests", 0), payload.get("http_4xx", 0), payload.get("http_429", 0),
            payload.get("http_5xx", 0), payload.get("network_errors", 0), payload.get("cache_dedupe_saves", 0),
            payload.get("projected_normal_requests", 0), payload.get("soft_budget", 0), payload.get("hard_budget", 0),
        ])

    def load_usage(self, month: str) -> dict[str, int] | None:
        with self._lock:
            if not self.connected:
                return None
            try:
                worksheet = self._worksheets[USAGE_TAB]
                values = worksheet.get_all_values(include_tailing_empty_rows=False, include_tailing_empty=False, returnas="matrix")
                if not values:
                    return None
                headers = values[0]
                index = {name: position for position, name in enumerate(headers)}
                for row in reversed(values[1:]):
                    if len(row) > index.get("month", 1) and row[index["month"]] == month:
                        return {
                            key: _number(row[index[key]])
                            for key in ("actual_request_count", "successful_requests", "http_4xx", "http_429", "http_5xx", "network_errors", "cache_dedupe_saves")
                            if key in index and len(row) > index[key]
                        }
            except Exception as error:
                self.last_error = str(error)[:500]
                LOGGER.exception("Unable to restore TomTom usage from Sheets: %s", error)
        return None

    def load_runtime_state(self) -> dict[str, Any] | None:
        """Read the latest small state snapshot used by scheduled runtimes."""
        with self._lock:
            if not self.connected:
                return None
            try:
                worksheet = self._worksheets[RUNTIME_TAB]
                values = worksheet.get_all_values(include_tailing_empty_rows=False, include_tailing_empty=False, returnas="matrix")
                if len(values) < 2 or len(values[-1]) < 2 or not values[-1][1]:
                    return None
                state = json.loads(values[-1][1])
                return state if isinstance(state, dict) else None
            except Exception as error:
                self.last_error = str(error)[:500]
                LOGGER.exception("Unable to restore runtime state from Sheets: %s", error)
                return None

    def save_runtime_state(self, state: Mapping[str, Any], recorded_at: datetime | None = None) -> bool:
        """Overwrite the latest runtime snapshot; this tab is intentionally not append-only."""
        with self._lock:
            if not self.connected:
                return False
            try:
                worksheet = self._worksheets[RUNTIME_TAB]
                self._write_rows(
                    worksheet,
                    2,
                    [[_iso(recorded_at or datetime.now(timezone.utc)), _json(state)]],
                    len(RUNTIME_HEADERS),
                )
                return True
            except Exception as error:
                self.last_error = str(error)[:500]
                LOGGER.exception("Unable to save runtime state to Sheets: %s", error)
                return False

    def should_flush(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            queued = sum(len(rows) for rows in self._queues.values())
            if not queued:
                return False
            if queued >= self.settings.sheets_batch_rows:
                return True
            return self.last_flush_at is None or (now - self.last_flush_at).total_seconds() >= self.settings.sheets_batch_seconds

    def flush(self, force: bool = False) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            if not self.connected:
                return False
            if self._next_retry_at and now < self._next_retry_at:
                return False
            if not force and not self.should_flush(now):
                return False
            try:
                self._open_period(now)
                for tab, worksheet in self._worksheets.items():
                    if tab == RUNTIME_TAB:
                        continue
                    queue = self._queues[tab]
                    while queue:
                        chunk = queue[:100]
                        self._append_rows(worksheet, chunk, len(TAB_HEADERS[tab]))
                        # Remove only the chunk whose write returned
                        # successfully.  A later tab failure cannot replay
                        # already-written rows, and a mid-tab failure retains
                        # only the unsent suffix.
                        del queue[: len(chunk)]
                self.last_flush_at = now
                self.last_error = None
                self._consecutive_flush_failures = 0
                self._next_retry_at = None
                return True
            except Exception as error:
                self.last_error = str(error)[:500]
                self._consecutive_flush_failures += 1
                backoff_seconds = min(300, 10 * (2 ** (self._consecutive_flush_failures - 1)))
                self._next_retry_at = now + timedelta(seconds=backoff_seconds)
                LOGGER.exception("Google Sheets batch flush failed: %s", error)
                return False
