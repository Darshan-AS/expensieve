"""Google Maps Timeline.json collector for Metro station snapping, Auto/Cab routing, and local QR visits."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from expensieve.domain.gazetteer import Gazetteer
from expensieve.domain.models import (
    EnrichmentEvidence,
    EvidencePriority,
    GeoPoint,
    StatementRow,
)
from expensieve.ports.protocols import ReverseGeocoder


@dataclass(frozen=True, slots=True)
class TimelineActivitySegment:
    """A movement segment (IN_SUBWAY, IN_PASSENGER_VEHICLE, MOTORCYCLING, etc.) from Timeline.json."""

    start_utc: datetime
    end_utc: datetime
    activity_type: str
    distance_meters: float
    start_point: GeoPoint
    end_point: GeoPoint


@dataclass(frozen=True, slots=True)
class TimelineVisitSegment:
    """A stationary place visit from Timeline.json."""

    start_utc: datetime
    end_utc: datetime
    semantic_type: str
    place_id: str
    point: GeoPoint


def _parse_iso_to_utc(raw_iso: str) -> datetime:
    dt = datetime.fromisoformat(raw_iso)
    return dt.astimezone(UTC)


def parse_timeline_segments(
    raw_json: dict[str, Any],
) -> tuple[list[TimelineActivitySegment], list[TimelineVisitSegment]]:
    """Extract structured activity and visit segments from Google Maps Timeline.json."""
    activities: list[TimelineActivitySegment] = []
    visits: list[TimelineVisitSegment] = []

    for seg in raw_json.get("semanticSegments", []):
        st_str = seg.get("startTime")
        et_str = seg.get("endTime")
        if not st_str or not et_str:
            continue
        try:
            st_utc = _parse_iso_to_utc(st_str)
            et_utc = _parse_iso_to_utc(et_str)
        except ValueError:
            continue

        if "activity" in seg:
            act = seg["activity"]
            tc = act.get("topCandidate", {})
            atype = str(tc.get("type") or "UNKNOWN")
            dist = float(act.get("distanceMeters") or 0.0)
            s_latlng = act.get("start", {}).get("latLng")
            e_latlng = act.get("end", {}).get("latLng")
            if s_latlng and e_latlng:
                try:
                    activities.append(
                        TimelineActivitySegment(
                            start_utc=st_utc,
                            end_utc=et_utc,
                            activity_type=atype,
                            distance_meters=dist,
                            start_point=GeoPoint.from_latlng_str(s_latlng),
                            end_point=GeoPoint.from_latlng_str(e_latlng),
                        )
                    )
                except ValueError:
                    pass

        if "visit" in seg:
            vis = seg["visit"]
            tc = vis.get("topCandidate", {})
            sem = str(tc.get("semanticType") or "UNKNOWN")
            pid = str(tc.get("placeId") or "")
            v_latlng = tc.get("placeLocation", {}).get("latLng")
            if v_latlng:
                try:
                    visits.append(
                        TimelineVisitSegment(
                            start_utc=st_utc,
                            end_utc=et_utc,
                            semantic_type=sem,
                            place_id=pid,
                            point=GeoPoint.from_latlng_str(v_latlng),
                        )
                    )
                except ValueError:
                    pass

    return activities, visits


class TimelineGeoCollector:
    """Resolves Metro station pairs, Auto/Cab routes, and local QR merchant locations from Timeline.json."""

    name = "timeline_geo"

    def __init__(
        self,
        timeline_path: Path,
        gazetteer: Gazetteer | None = None,
        geocoder: ReverseGeocoder | None = None,
        row_timestamps_utc: dict[int, str] | None = None,
    ) -> None:
        self._timeline_path = timeline_path
        self._gazetteer = gazetteer or Gazetteer()
        self._geocoder = geocoder
        self._row_timestamps_utc = row_timestamps_utc or {}

    def _resolve_point_label(self, point: GeoPoint, is_metro: bool = False) -> str | None:
        if is_metro:
            station = self._gazetteer.snap_metro_station(point)
            if station:
                return station
        landmark = self._gazetteer.snap_landmark(point)
        if landmark:
            return landmark
        if self._geocoder:
            return self._geocoder.resolve_place_name(point)
        return None

    def collect(
        self,
        rows: Sequence[StatementRow],
        start_date: date,
        end_date: date,
    ) -> Sequence[EnrichmentEvidence]:
        del start_date, end_date
        if not self._timeline_path.exists():
            return ()

        raw_json = json.loads(self._timeline_path.read_text(encoding="utf-8"))
        activities, visits = parse_timeline_segments(raw_json)
        evidences: list[EnrichmentEvidence] = []

        for row in rows:
            ts_iso = self._row_timestamps_utc.get(row.row_index)
            if not ts_iso:
                continue
            txn_utc = _parse_iso_to_utc(ts_iso)
            narr_u = row.narration.upper()

            # 1. Metro tickets -> Match nearest IN_SUBWAY segment within [-30 min, +90 min]
            is_metro_row = (
                "BANGALORE METRO" in narr_u
                or "BMRC" in narr_u
                or "RAPIDO522347" in narr_u
                or ("RAPIDO" in narr_u and "METRO" in narr_u)
            ) and row.amount_minor < 30000

            if is_metro_row:
                subway_candidates = [
                    a
                    for a in activities
                    if a.activity_type == "IN_SUBWAY"
                    and -1800 <= (a.start_utc - txn_utc).total_seconds() <= 5400
                ]
                if subway_candidates:
                    best_sub = min(
                        subway_candidates,
                        key=lambda a: abs((a.start_utc - txn_utc).total_seconds()),
                    )
                    start_st = self._resolve_point_label(best_sub.start_point, is_metro=True)
                    end_st = self._resolve_point_label(best_sub.end_point, is_metro=True)
                    if start_st and end_st and start_st != end_st:
                        evidences.append(
                            EnrichmentEvidence(
                                source_name=self.name,
                                priority=EvidencePriority.TIMELINE_GPS,
                                confidence=0.92,
                                row_index=row.row_index,
                                category="Metro",
                                payee="Rapido" if "RAPIDO" in narr_u else "Namma Metro",
                                note=f"{start_st} to {end_st}",
                                needs_review=False,
                            )
                        )
                continue

            # 2. Auto/Cab rides OR unknown BharatPe/person QR paid right at the end of a vehicle trip
            is_ride_merchant = any(k in narr_u for k in ("RAPIDO", "NAMMA YATRI", "NAMMA YAATRI", "UBER"))
            vehicle_candidates = [
                a
                for a in activities
                if a.activity_type in ("IN_PASSENGER_VEHICLE", "MOTORCYCLING", "IN_BUS")
                and a.distance_meters >= 800
                and (
                    # Paid during the ride or within 20 minutes after arrival
                    (a.start_utc.timestamp() - 600) <= txn_utc.timestamp() <= (a.end_utc.timestamp() + 1200)
                )
            ]

            if vehicle_candidates and (is_ride_merchant or "BHARATPE" in narr_u or "PAID VIA CRED" in narr_u):
                best_veh = min(
                    vehicle_candidates,
                    key=lambda a: abs((a.end_utc - txn_utc).total_seconds()),
                )
                # For non-ride merchants (like BharatPe QR), require payment within 4 minutes of trip end
                end_diff_sec = abs((best_veh.end_utc - txn_utc).total_seconds())
                if is_ride_merchant or end_diff_sec <= 240:
                    start_lbl = self._resolve_point_label(best_veh.start_point)
                    end_lbl = self._resolve_point_label(best_veh.end_point)
                    if start_lbl and end_lbl and start_lbl != end_lbl:
                        evidences.append(
                            EnrichmentEvidence(
                                source_name=self.name,
                                priority=EvidencePriority.TIMELINE_GPS,
                                confidence=0.90 if is_ride_merchant else 0.85,
                                row_index=row.row_index,
                                category="Auto",
                                payee=(
                                    "Rapido"
                                    if "RAPIDO" in narr_u
                                    else ("Namma Yaatri" if "NAMMA" in narr_u else None)
                                ),
                                note=f"{start_lbl} to {end_lbl}",
                                needs_review=False,
                            )
                        )
                        continue

            # 3. Stationary Place Visit for unknown BharatPe / QR merchants
            if "BHARATPE" in narr_u:
                active_visits = [v for v in visits if v.start_utc <= txn_utc <= v.end_utc]
                if active_visits:
                    best_vis = active_visits[-1]
                    place_lbl = self._resolve_point_label(best_vis.point)
                    if place_lbl:
                        evidences.append(
                            EnrichmentEvidence(
                                source_name=self.name,
                                priority=EvidencePriority.TIMELINE_GPS,
                                confidence=0.80,
                                row_index=row.row_index,
                                note=f"At {place_lbl}",
                                needs_review=True,
                            )
                        )

        return evidences
