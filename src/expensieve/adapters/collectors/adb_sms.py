"""Android ADB SMS collector (`content://sms/inbox`) for 12-digit RRN & exact UTC timestamp matching."""

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from expensieve.domain.models import (
    EnrichmentEvidence,
    EvidencePriority,
    StatementRow,
)


@dataclass(frozen=True, slots=True)
class ParsedSmsAlert:
    """A parsed banking or merchant SMS alert."""

    timestamp_iso: str
    date_iso: str
    address: str
    body: str
    rrn_12: str | None
    amount_minor: int | None
    is_rapido_metro: bool


def parse_raw_sms_dump(raw_text: str) -> list[ParsedSmsAlert]:
    """Parse `adb shell content query --uri content://sms/inbox` output into structured alerts."""
    row_splits = re.split(r"(?m)^Row: \d+ ", raw_text)
    alerts: list[ParsedSmsAlert] = []

    for chunk in row_splits:
        chunk_s = chunk.strip()
        if not chunk_s:
            continue
        m = re.match(r"date=(\d+),\s*address=([^,]*),\s*body=(.*)", chunk_s, re.DOTALL)
        if not m:
            continue
        ts_ms = int(m.group(1))
        addr = m.group(2).strip()
        body = m.group(3).strip()

        dt_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
        iso_utc = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        date_iso = dt_utc.strftime("%Y-%m-%d")

        rrn_match = re.search(r"(?:Ref|Ref No\.?|RRN|UPI:?)\s*(\d{12})\b", body, re.IGNORECASE)
        if not rrn_match:
            rrn_match = re.search(r"\b(\d{12})\b", body)
        rrn_12 = rrn_match.group(1) if rrn_match else None

        amt_minor: int | None = None
        amt_match = re.search(r"(?:Rs\.?|INR)\s*([\d,]+(?:\.\d{1,2})?)", body, re.IGNORECASE)
        if amt_match:
            try:
                dec = Decimal(amt_match.group(1).replace(",", ""))
                amt_minor = int((dec * 100).quantize(Decimal(1)))
            except InvalidOperation:
                amt_minor = None

        is_rapido_metro = "rapido" in addr.lower() and "metro ticket" in body.lower()

        alerts.append(
            ParsedSmsAlert(
                timestamp_iso=iso_utc,
                date_iso=date_iso,
                address=addr,
                body=body,
                rrn_12=rrn_12,
                amount_minor=amt_minor,
                is_rapido_metro=is_rapido_metro,
            )
        )
    return alerts


class AdbSmsCollector:
    """Collects SMS timestamps and Rapido Metro ticket confirmations via ADB or cached dump."""

    name = "adb_sms"

    def __init__(
        self,
        cached_sms_path: Path | None = None,
        adb_binary: str = "/usr/local/bin/adb",
    ) -> None:
        self._cached_sms_path = cached_sms_path
        self._adb_binary = adb_binary

    def _fetch_sms_text(self, start_date: date) -> str:
        if self._cached_sms_path and self._cached_sms_path.exists():
            return self._cached_sms_path.read_text(encoding="utf-8")

        epoch_ms = int(datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC).timestamp() * 1000)
        cmd = [
            self._adb_binary,
            "shell",
            f"content query --uri content://sms/inbox --projection date:address:body --where 'date >= {epoch_ms}'",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=20)
        if res.returncode != 0 or not res.stdout.strip():
            return ""
        if self._cached_sms_path:
            self._cached_sms_path.parent.mkdir(parents=True, exist_ok=True)
            self._cached_sms_path.write_text(res.stdout, encoding="utf-8")
        return res.stdout

    def collect(
        self,
        rows: Sequence[StatementRow],
        start_date: date,
        end_date: date,
    ) -> Sequence[EnrichmentEvidence]:
        del end_date
        raw_text = self._fetch_sms_text(start_date)
        if not raw_text:
            return ()

        alerts = parse_raw_sms_dump(raw_text)
        by_rrn: dict[str, ParsedSmsAlert] = {}
        for a in alerts:
            if a.rrn_12 and a.rrn_12 not in by_rrn:
                by_rrn[a.rrn_12] = a

        evidences: list[EnrichmentEvidence] = []
        for row in rows:
            rrn = row.rrn_12
            matched = by_rrn.get(rrn) if rrn else None
            if matched is None:
                # Fallback match by (date_iso, amount_minor)
                candidates = [
                    a
                    for a in alerts
                    if a.amount_minor == row.amount_minor and a.date_iso == row.date_iso
                ]
                if len(candidates) == 1:
                    matched = candidates[0]

            if matched is not None:
                evidences.append(
                    EnrichmentEvidence(
                        source_name=self.name,
                        priority=EvidencePriority.DECLARATIVE_RULE,
                        confidence=0.95,
                        row_index=row.row_index,
                        rrn_12=rrn or None,
                        timestamp_iso=matched.timestamp_iso,
                    )
                )
        return evidences
