import sys
from dataclasses import replace
from types import SimpleNamespace

from train_tracker.config import settings
from train_tracker.sheets import OBSERVATION_TAB, RUNTIME_TAB, USAGE_TAB, GoogleSheetsArchive


class WorksheetNotFound(Exception):
    pass


class FakeWorksheet:
    def __init__(self, title, rows=100, cols=20):
        self.title = title
        self.rows = rows
        self.cols = cols
        self.values = []
        self.appends = []
        self.updates = []

    def get_all_values(self, **_kwargs):
        return [list(row) for row in self.values]

    def resize(self, rows=None, cols=None):
        if rows is not None:
            self.rows = rows
        if cols is not None:
            self.cols = cols

    def update_values(self, cell, values, **_kwargs):
        self.updates.append((cell, [list(row) for row in values]))
        row_number = int(str(cell).split(":", 1)[0][1:]) if str(cell).startswith("A") else 1
        while len(self.values) < row_number - 1:
            self.values.append([])
        for offset, row in enumerate(values):
            target = row_number - 1 + offset
            if target < len(self.values):
                self.values[target] = list(row)
            else:
                self.values.append(list(row))

    def append_table(self, rows, **_kwargs):
        self.appends.extend([list(row) for row in rows])
        self.values.extend([list(row) for row in rows])


class FakeSpreadsheet:
    def __init__(self, identifier):
        self.id = identifier
        self.tabs = {}

    def worksheet_by_title(self, title):
        if title not in self.tabs:
            raise WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, **_kwargs):
        worksheet = FakeWorksheet(title, rows=_kwargs.get("rows", 100), cols=_kwargs.get("cols", 20))
        self.tabs[title] = worksheet
        return worksheet


class FakeClient:
    def __init__(self):
        self.spreadsheets = {"base": FakeSpreadsheet("base")}

    def open_by_key(self, identifier):
        return self.spreadsheets[identifier]

    def create(self, title):
        identifier = f"created-{len(self.spreadsheets)}"
        spreadsheet = FakeSpreadsheet(identifier)
        spreadsheet.title = title
        self.spreadsheets[identifier] = spreadsheet
        return spreadsheet


def test_sheets_archive_creates_tabs_batches_rows_and_restores_usage(monkeypatch):
    monkeypatch.setitem(sys.modules, "pygsheets", SimpleNamespace(WorksheetNotFound=WorksheetNotFound))
    archive_settings = replace(
        settings,
        sheets_spreadsheet_id="base",
        sheets_batch_rows=2,
        sheets_required=True,
    )
    client = FakeClient()
    archive = GoogleSheetsArchive(archive_settings, client=client)
    assert archive.connect() is True
    assert archive.health()["connected"] is True

    payload = {
        "recorded_at": "2026-08-29T12:00:00+00:00",
        "crossing_fra_id": "283559T",
        "crossing_name": "Pine Lake Rd",
        "group": "Battle Creek",
        "milepost": 182.48,
        "observed_at": "2026-08-29T12:00:00+00:00",
        "traffic_level_median": 1.0,
        "directional_values": {},
        "feature_count": 2,
        "usable": True,
        "severity": "NORMAL",
    }
    archive.enqueue_observation(payload)
    archive.enqueue_observation(payload)
    assert archive.flush() is True
    observation_tab = client.spreadsheets["base"].tabs[OBSERVATION_TAB]
    assert len(observation_tab.values) == 1 + 2

    usage = {
        "recorded_at": "2026-08-29T12:00:00+00:00",
        "month": "2026-08",
        "actual_request_count": 7,
        "successful_requests": 7,
        "cache_dedupe_saves": 2,
    }
    archive.enqueue_usage(usage)
    assert archive.flush(force=True) is True
    assert archive.load_usage("2026-08") == {
        "actual_request_count": 7,
        "successful_requests": 7,
        "cache_dedupe_saves": 2,
        "http_4xx": 0,
        "http_429": 0,
        "http_5xx": 0,
        "network_errors": 0,
    }

    assert USAGE_TAB in client.spreadsheets["base"].tabs

    runtime_state = {
        "version": 1,
        "last_polled": {"283559T": "2026-08-29T12:00:00+00:00"},
        "burst_until": {"Battle Creek": "2026-08-29T12:20:00+00:00"},
    }
    assert archive.save_runtime_state(runtime_state) is True
    assert archive.load_runtime_state() == runtime_state
    assert RUNTIME_TAB in client.spreadsheets["base"].tabs
    assert client.spreadsheets["base"].tabs[RUNTIME_TAB].updates[-1][0] == "A2:B2"


def test_append_rows_uses_exact_range_and_grows_existing_grid():
    worksheet = FakeWorksheet(OBSERVATION_TAB, rows=103, cols=20)
    worksheet.values = [["header"] * 20 for _ in range(103)]

    GoogleSheetsArchive._append_rows(worksheet, [["value"] * 20, ["value"] * 20], 20)

    assert worksheet.updates[-1][0] == "A104:T105"
    assert worksheet.rows == 105
    assert worksheet.cols == 20


def test_failed_flush_keeps_only_unsent_rows_and_reports_unhealthy(monkeypatch):
    class FailingWorksheet(FakeWorksheet):
        def update_values(self, cell, values, **kwargs):
            raise RuntimeError("simulated Sheets write failure")

    monkeypatch.setitem(sys.modules, "pygsheets", SimpleNamespace(WorksheetNotFound=WorksheetNotFound))
    archive_settings = replace(
        settings,
        sheets_spreadsheet_id="base",
        sheets_required=True,
        sheets_max_pending_rows=10,
    )
    archive = GoogleSheetsArchive(archive_settings, client=FakeClient())
    archive.connected = True
    archive._worksheets = {OBSERVATION_TAB: FailingWorksheet(OBSERVATION_TAB)}
    archive._open_period = lambda _now: None
    archive.enqueue_observation({"crossing_fra_id": "283559T"})

    assert archive.flush(force=True) is False
    assert archive.health()["queued_rows"] == 1
    assert archive.health()["healthy"] is False

    # A forced retry must not duplicate the pending row, even when the
    # write fails again.
    assert archive.flush(force=True) is False
    assert archive.health()["queued_rows"] == 1


def test_pending_queue_is_bounded_and_reports_dropped_rows(monkeypatch):
    monkeypatch.setitem(sys.modules, "pygsheets", SimpleNamespace(WorksheetNotFound=WorksheetNotFound))
    archive_settings = replace(
        settings,
        sheets_spreadsheet_id="base",
        sheets_required=True,
        sheets_max_pending_rows=2,
    )
    archive = GoogleSheetsArchive(archive_settings, client=FakeClient())
    archive.connected = True

    for value in range(3):
        archive.enqueue_event({"event_id": value})

    health = archive.health()
    assert health["queued_rows"] == 2
    assert health["dropped_rows"] == 1
    assert health["healthy"] is False
