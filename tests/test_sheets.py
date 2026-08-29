import sys
from dataclasses import replace
from types import SimpleNamespace

from train_tracker.config import settings
from train_tracker.sheets import OBSERVATION_TAB, USAGE_TAB, GoogleSheetsArchive


class WorksheetNotFound(Exception):
    pass


class FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.values = []
        self.appends = []

    def get_all_values(self, **_kwargs):
        return [list(row) for row in self.values]

    def update_values(self, cell, values):
        row_number = int(str(cell)[1:]) if str(cell).startswith("A") else 1
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
        worksheet = FakeWorksheet(title)
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
