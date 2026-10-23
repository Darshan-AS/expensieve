"""HDFC Bank Credit Card PDF statement adapter implementing StatementSource."""

import email
import imaplib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from expensieve.adapters.collectors.pdf_text import extract_pdf_rows_by_y
from expensieve.adapters.statements.hdfc_delimited import (
    MAX_STATEMENT_BYTES,
    parse_ddmmyy_to_iso,
    parse_inr_to_paise,
)
from expensieve.domain.models import StatementRow

DEFAULT_CC_PDF_PASSWORDS: tuple[str, ...] = ("DARS3835", "DARS5631", "128789591")

_DATE_PAT = re.compile(r"^(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2}(?::\d{2})?)?$")
_AMOUNT_CR_PAT = re.compile(
    r"^(?:([A-Z]{3}\s+[\d,]+\.\d{2})\s+)?([\d,]+\.\d{2})(?:\s*(Cr|CR|Dr|DR))?$"
)
_REF_PAT = re.compile(r"\(Ref#\s*([A-Za-z0-9]+)\)", re.IGNORECASE)
_STMT_DATE_PAT = re.compile(r"Statement\s+Date\s*:\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
_FEE_KEYWORDS: tuple[str, ...] = (
    "1% ON ALL DCC",
    "CONSOLIDATED FCY MARKUP FEE",
    "FIRST YEAR MEMBERSHIP FEE",
    "RENEWAL MEMBERSHIP FEE",
)


def _strip_ref_tag(narration: str) -> str:
    return re.sub(r"\s*\(Ref#\s*[A-Za-z0-9]+\)", "", narration, flags=re.IGNORECASE).strip()


@dataclass(frozen=True, slots=True)
class ParsedCcStatement:
    """Single parsed monthly HDFC Credit Card PDF statement."""

    filename: str
    statement_date_iso: str
    opening_balance_minor: int
    transactions: tuple[tuple[str, str, str, int, int], ...]  # (date_iso, narration, ref, debit, credit)


def parse_hdfc_cc_pdf_bytes(
    data: bytes,
    filename: str = "statement.pdf",
    passwords: Sequence[str] = DEFAULT_CC_PDF_PASSWORDS,
) -> ParsedCcStatement | None:
    """Parse a single HDFC Millennia / Credit Card monthly PDF statement into structured rows."""
    rows_by_y = extract_pdf_rows_by_y(data, passwords=passwords)
    statement_date_iso: str | None = None
    opening_balance_minor = 0
    found_summary_header = False
    found_opening = False
    in_transactions_section = False
    txns: list[tuple[str, str, str, int, int]] = []

    for _page_idx, _y_coord, cols in rows_by_y:
        joined = " ".join(cols)
        if statement_date_iso is None:
            m_sd = _STMT_DATE_PAT.search(joined)
            if m_sd:
                statement_date_iso = parse_ddmmyy_to_iso(m_sd.group(1))

        if "Account Summary" in joined or ("Previous Balance" in joined and "Total Dues" in joined):
            found_summary_header = True

        if (
            not found_opening
            and len(cols) >= 4
            and (found_summary_header or len(cols) == 5)
            and all(re.match(r"^-?[\d,]+\.\d{2}$", c.strip()) for c in cols[:4])
        ):
            # Account summary values row:
            # Previous Balance | Payments/Credits | Purchases/Debits | Finance Charges | Total Dues
            opening_balance_minor = -parse_inr_to_paise(cols[0].strip())
            found_opening = True
            continue

        if "Domestic Transactions" in joined or "International Transactions" in joined:
            in_transactions_section = True
            continue

        if "Cash points waiting for you" in joined or "Reward Points Summary" in joined:
            in_transactions_section = False
            continue

        if in_transactions_section and len(cols) >= 3:
            m_dt = _DATE_PAT.match(cols[0].strip())
            m_amt = _AMOUNT_CR_PAT.match(cols[-1].strip())
            if m_dt and m_amt:
                date_iso = parse_ddmmyy_to_iso(m_dt.group(1))
                fcy_part = re.sub(r"\s+", " ", (m_amt.group(1) or "").strip())
                amt_minor = parse_inr_to_paise(m_amt.group(2))
                cr_flag = (m_amt.group(3) or "").upper() == "CR"
                mid_cols = [c.strip() for c in cols[1:-1] if c.strip()]
                narration = re.sub(r"\s+", " ", " ".join(mid_cols)).strip()
                if not narration or not re.search(r"[A-Za-z]", narration):
                    continue
                if fcy_part:
                    narration = f"{narration} ({fcy_part})"
                m_ref = _REF_PAT.search(narration)
                ref_no = m_ref.group(1) if m_ref else ""
                debit_minor = 0 if cr_flag else amt_minor
                credit_minor = amt_minor if cr_flag else 0
                txns.append((date_iso, narration, ref_no, debit_minor, credit_minor))

    if statement_date_iso is None:
        return None

    return ParsedCcStatement(
        filename=filename,
        statement_date_iso=statement_date_iso,
        opening_balance_minor=opening_balance_minor,
        transactions=tuple(txns),
    )


class HdfcCreditCardPdfStatementSource:
    """Parses HDFC Credit Card PDF statements (from disk or Gmail IMAP) into StatementRows."""

    def __init__(
        self,
        path: Path | None = None,
        pdf_payloads: Sequence[tuple[str, bytes]] = (),
        account_name: str = "HDFC Credit Card",
        cutoff_ref: str | None = None,
        passwords: Sequence[str] = DEFAULT_CC_PDF_PASSWORDS,
    ) -> None:
        if path is None and not pdf_payloads:
            raise ValueError(
                "Either path or pdf_payloads must be provided to HdfcCreditCardPdfStatementSource."
            )
        self._path = path.expanduser().resolve() if path is not None else None
        self._pdf_payloads = tuple(pdf_payloads)
        self.account_name = account_name
        self._cutoff_ref = cutoff_ref
        self._passwords = tuple(passwords)
        self._opening_minor: int = 0
        self._all_rows: list[StatementRow] | None = None
        self._active_rows: list[StatementRow] | None = None

    @classmethod
    def from_gmail_imap(
        cls,
        email_address: str,
        app_password: str,
        account_name: str = "HDFC Credit Card",
        cutoff_ref: str | None = None,
        passwords: Sequence[str] = DEFAULT_CC_PDF_PASSWORDS,
        since_date_yyyy_mm_dd: str = "2024/01/01",
    ) -> HdfcCreditCardPdfStatementSource:
        """Fetch monthly HDFC Credit Card statement PDFs directly from Gmail IMAP."""
        clean_pwd = app_password.replace(" ", "")
        payloads: list[tuple[str, bytes]] = []
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=25)
        try:
            mail.login(email_address, clean_pwd)
            mail.select('"[Gmail]/All Mail"', readonly=True)
            raw_q = f'"from:Emailstatements.cards@hdfcbank.net has:attachment after:{since_date_yyyy_mm_dd}"'
            typ, data = mail.uid("SEARCH", "X-GM-RAW", raw_q)
            if typ == "OK" and data and data[0]:
                for uid_bytes in data[0].split():
                    _, msg_data = mail.uid("FETCH", uid_bytes, "(RFC822)")
                    if not (msg_data and msg_data[0] and isinstance(msg_data[0][1], bytes)):
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])
                    for part in msg.walk():
                        fname = part.get_filename() or ""
                        if fname.lower().endswith(".pdf"):
                            raw_pdf = part.get_payload(decode=True)
                            if isinstance(raw_pdf, bytes) and 0 < len(raw_pdf) <= MAX_STATEMENT_BYTES:
                                payloads.append((f"{uid_bytes.decode()}_{fname}", raw_pdf))
        finally:
            try:
                mail.logout()
            except imaplib.IMAP4.error, OSError:
                pass

        if not payloads:
            raise ValueError("No HDFC Credit Card PDF statements found in Gmail IMAP.")
        return cls(
            pdf_payloads=payloads,
            account_name=account_name,
            cutoff_ref=cutoff_ref,
            passwords=passwords,
        )

    def _collect_pdf_blobs(self) -> list[tuple[str, bytes]]:
        blobs: list[tuple[str, bytes]] = list(self._pdf_payloads)
        if self._path is not None:
            if not self._path.exists():
                raise FileNotFoundError(f"Statement path not found: {self._path}")
            if self._path.is_dir():
                for child in sorted(self._path.iterdir()):
                    if child.is_file() and child.suffix.lower() == ".pdf":
                        size = child.stat().st_size
                        if 0 < size <= MAX_STATEMENT_BYTES:
                            blobs.append((child.name, child.read_bytes()))
            elif self._path.is_file():
                size = self._path.stat().st_size
                if size <= 0 or size > MAX_STATEMENT_BYTES:
                    raise ValueError(f"Invalid PDF file size: {size} bytes")
                blobs.append((self._path.name, self._path.read_bytes()))
        return blobs

    def _load_all_rows(self) -> list[StatementRow]:
        if self._all_rows is not None:
            return self._all_rows

        by_stmt_date: dict[str, ParsedCcStatement] = {}
        for fname, raw_bytes in self._collect_pdf_blobs():
            try:
                parsed = parse_hdfc_cc_pdf_bytes(raw_bytes, filename=fname, passwords=self._passwords)
            except ValueError:
                continue
            if parsed is not None:
                by_stmt_date[parsed.statement_date_iso] = parsed

        if not by_stmt_date:
            raise ValueError("No valid HDFC Credit Card monthly statements could be parsed.")

        ordered_stmts = [by_stmt_date[k] for k in sorted(by_stmt_date)]
        self._opening_minor = ordered_stmts[0].opening_balance_minor
        running_minor = self._opening_minor
        rows: list[StatementRow] = []
        row_idx = 0
        merchant_by_ref: dict[str, str] = {}
        fee_by_ref: dict[str, str] = {}

        for stmt in ordered_stmts:
            for _dt, narration, ref_no, _deb, _cred in stmt.transactions:
                if not ref_no:
                    continue
                narr_u = narration.upper()
                clean_desc = _strip_ref_tag(narration)
                if "IGST" in narr_u:
                    continue
                if any(k in narr_u for k in _FEE_KEYWORDS):
                    fee_by_ref[ref_no] = clean_desc
                elif "TELE TRANSFER CREDIT" not in narr_u and "IMPS PMT" not in narr_u:
                    merchant_by_ref[ref_no] = clean_desc

            for date_iso, narration, ref_no, debit_minor, credit_minor in stmt.transactions:
                row_idx += 1
                running_minor += credit_minor - debit_minor
                synth_ref = (
                    ref_no or f"CC-{date_iso.replace('-', '')}-{debit_minor or credit_minor}-{row_idx:03d}"
                )
                enriched_narr = narration
                if ref_no and "[Parent:" not in narration:
                    narr_u = narration.upper()
                    if "IGST" in narr_u:
                        parent_m = merchant_by_ref.get(ref_no)
                        parent_f = fee_by_ref.get(ref_no)
                        if parent_f and parent_m:
                            enriched_narr = f"{narration} [Parent: {parent_f} - {parent_m}]"
                        elif parent_f or parent_m:
                            enriched_narr = f"{narration} [Parent: {parent_f or parent_m}]"
                    elif any(k in narr_u for k in ("1% ON ALL DCC", "CONSOLIDATED FCY MARKUP FEE")):
                        parent_m = merchant_by_ref.get(ref_no)
                        if parent_m:
                            enriched_narr = f"{narration} [Parent: {parent_m}]"

                rows.append(
                    StatementRow(
                        row_index=row_idx,
                        date_iso=date_iso,
                        value_date_iso=date_iso,
                        narration=enriched_narr,
                        reference_no=synth_ref,
                        debit_minor=debit_minor,
                        credit_minor=credit_minor,
                        closing_balance_minor=running_minor,
                    )
                )

        self._all_rows = rows
        return rows

    def parse(self) -> Sequence[StatementRow]:
        """Return all StatementRows after cutoff_ref (or all rows if no cutoff_ref is specified)."""
        if self._active_rows is not None:
            return self._active_rows

        all_rows = self._load_all_rows()
        if not self._cutoff_ref:
            self._active_rows = list(all_rows)
            return self._active_rows

        for idx, row in enumerate(all_rows):
            if (
                self._cutoff_ref in row.reference_no
                or self._cutoff_ref == row.rrn_12
                or self._cutoff_ref == row.date_iso
            ):
                self._active_rows = all_rows[idx + 1 :]
                return self._active_rows

        raise ValueError(f"Cutoff reference {self._cutoff_ref!r} not found in credit card statements.")

    def opening_balance_minor(self) -> int:
        """Return the card balance immediately preceding the first active row."""
        active = self.parse()
        if not active:
            all_rows = self._load_all_rows()
            return all_rows[-1].closing_balance_minor if all_rows else self._opening_minor
        first = active[0]
        return first.closing_balance_minor - first.signed_delta_minor

    def closing_balance_minor(self) -> int:
        """Return the card balance after the last active row."""
        active = self.parse()
        if not active:
            all_rows = self._load_all_rows()
            return all_rows[-1].closing_balance_minor if all_rows else self._opening_minor
        return active[-1].closing_balance_minor
