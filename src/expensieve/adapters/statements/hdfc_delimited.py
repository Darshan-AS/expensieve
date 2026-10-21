"""HDFC Bank delimited (.txt / .csv) statement adapter implementing StatementSource."""

import csv
import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from expensieve.domain.models import StatementRow

MAX_STATEMENT_BYTES = 25 * 1024 * 1024  # 25 MB safety limit


@dataclass(frozen=True, slots=True)
class UpiNarrationParts:
    """Decomposed components of an HDFC `UPI-<PAYEE>-<VPA>-<IFSC>-<RRN>-<NOTE>` narration."""

    is_upi: bool
    payee: str
    vpa: str
    rrn: str
    note: str


def parse_ddmmyy_to_iso(raw_date: str) -> str:
    """Convert HDFC DD/MM/YY or DD/MM/YYYY date string to ISO YYYY-MM-DD."""
    clean = raw_date.strip()
    parts = clean.split("/")
    if len(parts) != 3:
        raise ValueError(f"Invalid HDFC date format {raw_date!r}, expected DD/MM/YY")
    day, month, yr = parts[0].strip(), parts[1].strip(), parts[2].strip()
    year_full = f"20{yr}" if len(yr) == 2 else yr
    if not (day.isdigit() and month.isdigit() and year_full.isdigit()):
        raise ValueError(f"Non-numeric date component in {raw_date!r}")
    return f"{int(year_full):04d}-{int(month):02d}-{int(day):02d}"


def parse_inr_to_paise(raw_val: str) -> int:
    """Convert a comma-formatted INR string ('1,00,000.00') to integer paise."""
    clean = raw_val.strip().replace(",", "")
    if not clean:
        return 0
    try:
        dec = Decimal(clean).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid monetary amount {raw_val!r}") from exc
    return int(dec * 100)


def parse_upi_narration(narration: str, ref_number: str) -> UpiNarrationParts:
    """Decompose `UPI-<PAYEE>-<VPA>-<IFSC>-<RRN>-<NOTE>` safely."""
    clean = narration.strip()
    if not clean.startswith("UPI-"):
        return UpiNarrationParts(
            is_upi=False,
            payee="",
            vpa="",
            rrn=ref_number.lstrip("0") or ref_number,
            note="",
        )

    body = clean[4:]
    rrn_short = ref_number.lstrip("0")
    if rrn_short and len(rrn_short) >= 10 and f"-{rrn_short}-" in body:
        before_rrn, after_rrn = body.split(f"-{rrn_short}-", 1)
        note = after_rrn.strip()
        head_parts = before_rrn.split("-")
        payee = head_parts[0].strip() if head_parts else ""
        vpa = head_parts[1].strip() if len(head_parts) > 1 else ""
        return UpiNarrationParts(True, payee, vpa, rrn_short, note)

    match = re.search(r"-(\d{11,12})-(.*)$", body)
    if match:
        rrn_found = match.group(1)
        note = match.group(2).strip()
        before_rrn = body[: match.start()]
        head_parts = before_rrn.split("-")
        payee = head_parts[0].strip() if head_parts else ""
        vpa = head_parts[1].strip() if len(head_parts) > 1 else ""
        return UpiNarrationParts(True, payee, vpa, rrn_found, note)

    parts = body.split("-")
    payee = parts[0].strip() if parts else ""
    vpa = parts[1].strip() if len(parts) > 1 else ""
    note = parts[-1].strip() if len(parts) > 4 else ""
    return UpiNarrationParts(True, payee, vpa, rrn_short or ref_number, note)


class HdfcDelimitedStatementSource:
    """Parses HDFC delimited (.txt/.csv) statements and verifies row-by-row balance continuity."""

    def __init__(
        self,
        path: Path,
        account_name: str = "HDFC",
        cutoff_ref: str | None = None,
    ) -> None:
        self._path = self._validate_file(path)
        self.account_name = account_name
        self._cutoff_ref = cutoff_ref
        self._all_rows: list[StatementRow] | None = None
        self._active_rows: list[StatementRow] | None = None

    @staticmethod
    def _validate_file(path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(f"Statement file not found: {resolved}")
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_STATEMENT_BYTES:
            raise ValueError(f"Invalid statement file size: {size} bytes")
        return resolved

    def _load_all_rows(self) -> list[StatementRow]:
        if self._all_rows is not None:
            return self._all_rows

        with open(self._path, encoding="utf-8", errors="replace") as f:
            non_empty_lines = [line for line in f if line.strip()]

        if not non_empty_lines:
            raise ValueError(f"No lines found in {self._path}")

        reader = csv.DictReader(non_empty_lines)
        rows: list[StatementRow] = []
        for idx, raw_row in enumerate(reader, start=1):
            norm = {k.strip(): (v.strip() if v else "") for k, v in raw_row.items() if k}
            if not norm.get("Date") or not norm.get("Narration"):
                continue
            date_iso = parse_ddmmyy_to_iso(norm["Date"])
            val_date_iso = parse_ddmmyy_to_iso(norm.get("Value Dat") or norm["Date"])
            narration = re.sub(r"\s+", " ", norm["Narration"]).strip()
            debit_minor = parse_inr_to_paise(norm.get("Debit Amount", "0"))
            credit_minor = parse_inr_to_paise(norm.get("Credit Amount", "0"))
            ref_number = norm.get("Chq/Ref Number", "").strip()
            closing_minor = parse_inr_to_paise(norm.get("Closing Balance", "0"))

            rows.append(
                StatementRow(
                    row_index=idx,
                    date_iso=date_iso,
                    value_date_iso=val_date_iso,
                    narration=narration,
                    reference_no=ref_number,
                    debit_minor=debit_minor,
                    credit_minor=credit_minor,
                    closing_balance_minor=closing_minor,
                )
            )

        # Verify mathematical running balance continuity across all rows
        for prev, curr in itertools.pairwise(rows):
            expected = prev.closing_balance_minor + curr.signed_delta_minor
            if expected != curr.closing_balance_minor:
                raise ValueError(
                    f"Balance continuity mismatch at row {curr.row_index} ({curr.date_iso}): "
                    f"prev={prev.closing_balance_minor} + delta={curr.signed_delta_minor} "
                    f"!= closing={curr.closing_balance_minor}"
                )

        self._all_rows = rows
        return rows

    def parse(self) -> Sequence[StatementRow]:
        """Return all rows after cutoff_ref (or all rows if no cutoff_ref is specified)."""
        if self._active_rows is not None:
            return self._active_rows

        all_rows = self._load_all_rows()
        if not self._cutoff_ref:
            self._active_rows = list(all_rows)
            return self._active_rows

        for idx, row in enumerate(all_rows):
            if self._cutoff_ref in row.reference_no or self._cutoff_ref == row.rrn_12:
                self._active_rows = all_rows[idx + 1 :]
                return self._active_rows

        raise ValueError(f"Cutoff reference {self._cutoff_ref!r} not found in statement.")

    def opening_balance_minor(self) -> int:
        """Return the account balance immediately preceding the first parsed row."""
        active = self.parse()
        if not active:
            all_rows = self._load_all_rows()
            return all_rows[-1].closing_balance_minor if all_rows else 0
        first = active[0]
        return first.closing_balance_minor - first.signed_delta_minor

    def closing_balance_minor(self) -> int:
        """Return the closing balance after the last parsed row."""
        active = self.parse()
        if not active:
            all_rows = self._load_all_rows()
            return all_rows[-1].closing_balance_minor if all_rows else 0
        return active[-1].closing_balance_minor
