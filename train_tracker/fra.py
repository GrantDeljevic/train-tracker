from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)

FRA_LAYER_URL = (
    "https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services/"
    "NTAD_Railroad_Grade_Crossings/FeatureServer/0"
)


@dataclass(frozen=True)
class FRARecord:
    fra_id: str
    street: str
    railroad: str
    subdivision: str
    milepost: float
    latitude: float
    longitude: float
    aadt: int | None
    aadt_year: int | None
    revision_date: datetime | None
    raw: dict[str, Any]


class FRAValidationError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _date_from_ms(value: Any) -> datetime | None:
    try:
        if value is None:
            return None
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_milepost(value: Any) -> float | None:
    return _number(value)


def validate_feature(feature: dict[str, Any], revision_date: datetime | None = None) -> FRARecord:
    attrs = feature.get("attributes", feature)
    fra_id = _text(attrs.get("CrossingID") or attrs.get("crossing_id"))
    railroad = _upper(attrs.get("RailroadCode") or attrs.get("ParentRailroadCode"))
    parent = _upper(attrs.get("ParentRailroadCode"))
    subdivision = _text(attrs.get("RailroadSubdivision"))
    crossing_type = _upper(attrs.get("CrossingType"))
    purpose = _upper(attrs.get("CrossingPurpose"))
    position = _upper(attrs.get("CrossingPosition"))
    reason = _upper(attrs.get("ReasonCode"))
    end_date = _text(attrs.get("RRCoEndDate"))
    main_tracks = _int(attrs.get("NumberOfMainTracks"))
    if not fra_id:
        raise FRAValidationError("FRA record has no CrossingID")
    if railroad not in {"GTW", "CN"} and parent not in {"GTW", "CN"}:
        raise FRAValidationError(f"{fra_id} is not a GTW/CN crossing (railroad={railroad!r})")
    if "FLINT" not in subdivision.upper():
        raise FRAValidationError(f"{fra_id} is not on the Flint Subdivision")
    if "GRADE" not in position or "AT" not in position:
        raise FRAValidationError(f"{fra_id} is not an at-grade crossing (position={position!r})")
    if "PRIVATE" in crossing_type or "PRIVATE" in purpose or "PRIVATE" in position:
        raise FRAValidationError(f"{fra_id} is not public")
    if reason in {"C", "CLOSED", "INACTIVE", "RETIRED"} or end_date:
        raise FRAValidationError(f"{fra_id} is closed/inactive")
    if main_tracks is not None and main_tracks < 1:
        raise FRAValidationError(f"{fra_id} has no main-line track")

    geometry = feature.get("geometry") or {}
    longitude = _number(attrs.get("Longitude"))
    latitude = _number(attrs.get("LATITUDE"))
    if longitude is None:
        longitude = _number(geometry.get("x"))
    if latitude is None:
        latitude = _number(geometry.get("y"))
    milepost = _parse_milepost(attrs.get("RailroadMilepostNumber"))
    if latitude is None or longitude is None or milepost is None:
        raise FRAValidationError(f"{fra_id} is missing authoritative coordinates or milepost")
    aadt = _int(attrs.get("AnnualAverageDailyTrafficCount"))
    aadt_year = _int(attrs.get("AnnualAverageDailyTrafficYear"))
    return FRARecord(
        fra_id=fra_id,
        street=_text(attrs.get("STREET") or attrs.get("HighwayName")) or fra_id,
        railroad=railroad if railroad in {"GTW", "CN"} else parent,
        subdivision=subdivision,
        milepost=milepost,
        latitude=latitude,
        longitude=longitude,
        aadt=aadt,
        aadt_year=aadt_year,
        revision_date=revision_date,
        raw=attrs,
    )


class FRAClient:
    def __init__(self, client: httpx.Client | None = None, timeout: float = 15.0):
        self.client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._revision_date: datetime | None = None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def layer_revision_date(self) -> datetime | None:
        if self._revision_date is not None:
            return self._revision_date
        response = self.client.get(FRA_LAYER_URL, params={"f": "json"})
        response.raise_for_status()
        data = response.json()
        self._revision_date = _date_from_ms((data.get("editingInfo") or {}).get("lastEditDate"))
        return self._revision_date

    def resolve_crossing(self, fra_id: str) -> FRARecord:
        revision = self.layer_revision_date()
        response = self.client.get(
            f"{FRA_LAYER_URL}/query",
            params={
                "where": f"CrossingID = '{fra_id}'",
                "outFields": "*",
                "returnGeometry": "true",
                "f": "json",
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise FRAValidationError(f"FRA query failed for {fra_id}: {data['error']}")
        features = data.get("features") or []
        if len(features) != 1:
            raise FRAValidationError(f"FRA ID {fra_id} resolved to {len(features)} records")
        record = validate_feature(features[0], revision)
        if record.fra_id.upper() != fra_id.upper():
            raise FRAValidationError(f"FRA returned {record.fra_id} for requested {fra_id}")
        return record

    def find_valid_crossings_near(
        self,
        latitude: float,
        longitude: float,
        milepost_low: float,
        milepost_high: float,
        radius_degrees: float = 0.30,
    ) -> list[FRARecord]:
        """Discover substitute sentinels from the same FRA-maintained corridor."""
        response = self.client.get(
            f"{FRA_LAYER_URL}/query",
            params={
                "where": "RailroadSubdivision LIKE '%FLINT%'",
                "geometry": json.dumps({"xmin": longitude - radius_degrees, "ymin": latitude - radius_degrees, "xmax": longitude + radius_degrees, "ymax": latitude + radius_degrees, "spatialReference": {"wkid": 4326}}),
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true",
                "resultRecordCount": "2000",
                "f": "json",
            },
        )
        response.raise_for_status()
        data = response.json()
        result = []
        for feature in data.get("features", []):
            try:
                candidate = validate_feature(feature, self.layer_revision_date())
            except FRAValidationError:
                continue
            if milepost_low <= candidate.milepost <= milepost_high:
                result.append(candidate)
        return result
