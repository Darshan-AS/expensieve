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
    TransactionKind,
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

        epoch_ms = int(
            datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC).timestamp() * 1000
        )
        cmd = [
            self._adb_binary,
            "shell",
            f"content query --uri content://sms/inbox --projection date:address:body --where 'date >= {epoch_ms}'",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=20)
        except OSError, subprocess.SubprocessError:
            return ""
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
        bse_appl_by_symbol: dict[str, str] = {}
        cdsl_ipo_credits: list[tuple[str, int, str, str]] = []  # (date_iso, qty, company, timestamp_iso)

        for a in alerts:
            if a.rrn_12 and a.rrn_12 not in by_rrn:
                by_rrn[a.rrn_12] = a
            m_bse = re.search(
                r"APPLICATION\s+NO\.?\s*(\d+)\s+OF\s+([A-Za-z0-9.\-]+?)\.?\s+HAS\s+BEEN\s+SUBMITTED\s+ON\s+BSE",
                a.body,
                re.IGNORECASE,
            )
            if m_bse:
                bse_appl_by_symbol[m_bse.group(2).upper().strip(".")] = m_bse.group(1).strip()

            m_cdsl = re.search(
                r"CDSL:\s*Credit\s+in\s+a/c\s+\*\d+\s+through\s+IPO/FPO\s+for\s+(\d+(?:\.\d+)?)-([A-Za-z0-9 \-\.]+?)(?:-EQ)?\s+on\s+\d{2}[A-Za-z]{3}",
                a.body,
                re.IGNORECASE,
            )
            if m_cdsl:
                qty_val = int(float(m_cdsl.group(1)))
                comp_raw = m_cdsl.group(2).strip().title()
                if qty_val > 0:
                    cdsl_ipo_credits.append((a.date_iso, qty_val, comp_raw, a.timestamp_iso))

        evidences: list[EnrichmentEvidence] = []
        for row in rows:
            rrn = row.rrn_12
            matched = by_rrn.get(rrn) if rrn else None
            if matched is None:
                # Fallback match by (date_iso, amount_minor)
                candidates = [
                    a for a in alerts if a.amount_minor == row.amount_minor and a.date_iso == row.date_iso
                ]
                if len(candidates) == 1:
                    cand = candidates[0]
                    # Only apply fallback timestamp if sender matches account or no POS timestamp exists
                    is_pos_narr = "POS VS/" in row.narration.upper()
                    is_dcb_sms = "DCBANK" in cand.address.upper() or "DCB" in cand.body.upper()
                    if not is_pos_narr or is_dcb_sms:
                        matched = cand

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

            # Check if an ASBA IPO debit row matches a CDSL IPO/FPO credit SMS
            if not row.is_credit and (
                row.narration.endswith("-50200021339924") or "IPO" in row.narration.upper()
            ):
                for c_date_iso, qty_val, comp_name, c_ts_iso in cdsl_ipo_credits:
                    if c_date_iso == row.date_iso and (row.amount_minor % (qty_val * 100) == 0):
                        price_inr = (row.amount_minor // 100) // qty_val
                        m_nar_appl = re.search(
                            r"UPI-\d+-([A-Z]*)(\d{7,12})-50200021339924",
                            row.narration.upper(),
                        )
                        appl_no = ""
                        if m_nar_appl:
                            appl_no = m_nar_appl.group(2)
                        else:
                            for sym, b_appl in bse_appl_by_symbol.items():
                                if sym in comp_name.upper():
                                    appl_no = b_appl
                                    break
                        appl_part = f", Appl {appl_no}" if appl_no else ""
                        evidences.append(
                            EnrichmentEvidence(
                                source_name=f"{self.name}:cdsl_ipo",
                                priority=EvidencePriority.UPI_REMARK,
                                confidence=0.96,
                                row_index=row.row_index,
                                timestamp_iso=c_ts_iso if matched is None else None,
                                kind=TransactionKind.TRANSFER,
                                transfer_account="Investments",
                                category="Transfer, withdraw",
                                payee="IndMoney",
                                note=f"IPO in IndMoney: {comp_name} ({qty_val} shares @ ₹{price_inr:,}{appl_part})",
                                needs_review=False,
                            )
                        )
                        break
        return evidences
