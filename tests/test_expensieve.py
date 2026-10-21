"""Unit tests for ExpenSieve domain models, Namma Metro Gazetteer, EvidenceMerger, and TimelineGeoCollector."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from expensieve.adapters.collectors.adb_sms import parse_raw_sms_dump
from expensieve.adapters.collectors.timeline_geo import TimelineGeoCollector
from expensieve.adapters.statements.hdfc_delimited import (
    HdfcDelimitedStatementSource,
    parse_upi_narration,
)
from expensieve.domain.gazetteer import Gazetteer, haversine_meters
from expensieve.domain.models import (
    EnrichmentEvidence,
    EvidencePriority,
    GeoPoint,
    StatementRow,
)
from expensieve.domain.rules import DeclarativeRuleCollector
from expensieve.services.evidence_merger import EvidenceMerger
from expensieve.services.pipeline import SyncPipeline


class TestGazetteerAndGeo(unittest.TestCase):
    def test_metro_station_snapping(self) -> None:
        gaz = Gazetteer()
        # Benniganahalli Metro coordinates from Timeline.json
        benn = GeoPoint.from_latlng_str("12.9961954°, 77.6685395°")
        self.assertEqual(gaz.snap_metro_station(benn), "Benniganahalli")

        # Indiranagar Metro coordinates from Timeline.json
        ind = GeoPoint.from_latlng_str("12.9782619°, 77.6385257°")
        self.assertEqual(gaz.snap_metro_station(ind), "Indiranagar")

        # Sandal Soap Factory Metro
        ssf = GeoPoint.from_latlng_str("13.0147286°, 77.5540309°")
        self.assertEqual(gaz.snap_metro_station(ssf), "Sandal Soap Factory")

    def test_landmark_snapping(self) -> None:
        gaz = Gazetteer()
        home = GeoPoint.from_latlng_str("13.0250041°, 77.673822°")
        self.assertEqual(gaz.snap_landmark(home), "Bangalore Home")

        bic = GeoPoint.from_latlng_str("12.966694°, 77.6354237°")
        self.assertEqual(gaz.snap_landmark(bic), "Bangalore International Centre (Domlur)")

        dist = haversine_meters(home, bic)
        self.assertGreater(dist, 7000.0)
        self.assertLess(dist, 8500.0)


class TestHdfcParserAndSms(unittest.TestCase):
    def test_upi_narration_decomposition(self) -> None:
        parts = parse_upi_narration(
            "UPI-SHIVANANDA A B-9844073291@YBL-SBIN0040046-645935318030-BANGALORE HOME EXP",
            "0000645935318030",
        )
        self.assertTrue(parts.is_upi)
        self.assertEqual(parts.payee, "SHIVANANDA A B")
        self.assertEqual(parts.vpa, "9844073291@YBL")
        self.assertEqual(parts.rrn, "645935318030")
        self.assertEqual(parts.note, "BANGALORE HOME EXP")

    def test_sms_dump_parsing(self) -> None:
        sample_dump = (
            "Row: 1 date=1770646936000, address=VM-HDFCBK-T, "
            "body=Sent Rs.20.00\nFrom HDFC Bank A/C *0122\nTo Bangalore Metro Rail Corp\n"
            "On 09/02/26\nRef 604006394456\n"
        )
        alerts = parse_raw_sms_dump(sample_dump)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].rrn_12, "604006394456")
        self.assertEqual(alerts[0].amount_minor, 2000)


class TestEvidenceMergerAndTimeline(unittest.TestCase):
    def test_priority_precedence_and_timeline_matching(self) -> None:
        row = StatementRow(
            row_index=1,
            date_iso="2026-02-11",
            value_date_iso="2026-02-11",
            narration="UPI-SUNDAR RAJ V-SUNDARRAJU.V3@IBL-UBIN0907014-640821698556-RAPIDO",
            reference_no="0000640821698556",
            debit_minor=20000,
            credit_minor=0,
            closing_balance_minor=50000000,
        )

        # Simulate Timeline.json with Home -> BIC Domlur ride ending at 2026-02-11T19:47:00+05:30 (14:17:00Z)
        fake_timeline = {
            "semanticSegments": [
                {
                    "startTime": "2026-02-11T18:47:00.000+05:30",
                    "endTime": "2026-02-11T19:47:00.000+05:30",
                    "activity": {
                        "distanceMeters": 11967.0,
                        "topCandidate": {"type": "IN_PASSENGER_VEHICLE"},
                        "start": {"latLng": "13.0249479°, 77.6738096°"},
                        "end": {"latLng": "12.9662506°, 77.6364679°"},
                    },
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tl_path = Path(tmpdir) / "Timeline.json"
            tl_path.write_text(json.dumps(fake_timeline), encoding="utf-8")

            rule_collector = DeclarativeRuleCollector()
            tl_collector = TimelineGeoCollector(
                timeline_path=tl_path,
                row_timestamps_utc={1: "2026-02-11T14:17:54Z"},
            )

            evs = [
                *rule_collector.collect([row], date(2026, 2, 11), date(2026, 2, 11)),
                EnrichmentEvidence(
                    source_name="adb_sms",
                    priority=EvidencePriority.DECLARATIVE_RULE,
                    confidence=0.95,
                    row_index=1,
                    timestamp_iso="2026-02-11T14:17:54Z",
                ),
                *tl_collector.collect([row], date(2026, 2, 11), date(2026, 2, 11)),
            ]

            merger = EvidenceMerger(account_name="HDFC")
            merged = merger.merge([row], evs)
            self.assertEqual(len(merged), 1)
            tx = merged[0]
            self.assertEqual(tx.category, "Auto")
            self.assertEqual(tx.payee, "Rapido")
            self.assertEqual(tx.note, "Bangalore Home to Bangalore International Centre (Domlur)")
            self.assertFalse(tx.needs_review)
            self.assertNotIn("Needs Review", tx.labels)

    def test_end_to_end_pipeline_balance_check(self) -> None:
        sample_stmt = (
            "Date,Narration,Value Dat,Debit Amount,Credit Amount,Chq/Ref Number,Closing Balance\n"
            "24/01/26,UPI-BLINKIT-639024130200-BLINKIT,24/01/26,280.00,0.00,0000639024130200,1000.00\n"
            "25/01/26,UPI- TEA SHOP -602593847551-MASALA DOSA,25/01/26,100.00,0.00,0000602593847551,900.00\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            stmt_path = Path(tmpdir) / "stmt.txt"
            stmt_path.write_text(sample_stmt, encoding="utf-8")

            src = HdfcDelimitedStatementSource(stmt_path, cutoff_ref="639024130200")
            pipeline = SyncPipeline(
                statement_source=src,
                collectors=[DeclarativeRuleCollector()],
            )
            summary = pipeline.run()
            self.assertEqual(summary.row_count, 1)
            self.assertEqual(summary.opening_balance_minor, 100000)
            self.assertEqual(summary.closing_balance_minor, 90000)
            self.assertEqual(summary.net_delta_minor, -10000)
            self.assertEqual(summary.transactions[0].category, "Meals & Fast Food")
            self.assertEqual(summary.transactions[0].note, "Masala dosa")
            self.assertFalse(summary.transactions[0].needs_review)


if __name__ == "__main__":
    unittest.main()
