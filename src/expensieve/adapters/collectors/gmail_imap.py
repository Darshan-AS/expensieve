"""Gmail IMAP receipt and PDF invoice collector with pluggable ReceiptExtractor strategies."""

import email
import html
import imaplib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Protocol

from expensieve.adapters.collectors.pdf_text import (
    extract_pdf_lines,
    extract_pdf_text,
    parse_biff8_xls_rows,
)
from expensieve.adapters.statements.hdfc_cc_pdf import (
    DEFAULT_CC_PDF_PASSWORDS,
    parse_hdfc_cc_pdf_bytes,
)
from expensieve.domain.gazetteer import Gazetteer
from expensieve.domain.models import (
    EnrichmentEvidence,
    EvidencePriority,
    PaymentMethod,
    StatementRow,
    TransactionKind,
)


@dataclass(frozen=True, slots=True)
class EmailMessagePayload:
    """Decoded email message representation passed to ReceiptExtractor strategies."""

    uid: str
    sent_utc: datetime
    date_iso: str
    sender: str
    subject: str
    text_body: str
    attachments: tuple[tuple[str, bytes], ...]


class ReceiptExtractor(Protocol):
    """Strategy interface for extracting EnrichmentEvidence from an EmailMessagePayload."""

    name: str
    imap_search_query: str

    def extract(
        self,
        messages: Sequence[EmailMessagePayload],
        rows: Sequence[StatementRow],
    ) -> Sequence[EnrichmentEvidence]: ...


def _decode_mime_header(raw: str | None) -> str:
    if not raw:
        return ""
    out: list[str] = []
    for part, enc in decode_header(raw):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out)


def _extract_email_payload(uid: str, msg: Message) -> EmailMessagePayload:
    subj = _decode_mime_header(msg.get("Subject"))
    frm = _decode_mime_header(msg.get("From"))
    dt_raw = msg.get("Date")
    try:
        dt_utc = parsedate_to_datetime(dt_raw).astimezone(UTC) if dt_raw else datetime.now(UTC)
    except TypeError, ValueError:
        dt_utc = datetime.now(UTC)

    text_chunks: list[str] = []
    html_chunks: list[str] = []
    attachments: list[tuple[str, bytes]] = []

    for part in msg.walk():
        ctype = part.get_content_type()
        fname = _decode_mime_header(part.get_filename()) if part.get_filename() else ""
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        if fname:
            attachments.append((fname, payload))
        elif ctype == "text/plain":
            charset = part.get_content_charset() or "utf-8"
            text_chunks.append(payload.decode(charset, errors="replace"))
        elif ctype == "text/html":
            charset = part.get_content_charset() or "utf-8"
            raw_html = payload.decode(charset, errors="replace")
            stripped = html.unescape(" ".join(re.sub(r"<[^>]+>", " ", raw_html).split()))
            html_chunks.append(stripped)

    body = "\n".join(text_chunks) if text_chunks else "\n".join(html_chunks)
    return EmailMessagePayload(
        uid=uid,
        sent_utc=dt_utc,
        date_iso=dt_utc.strftime("%Y-%m-%d"),
        sender=frm,
        subject=subj,
        text_body=body,
        attachments=tuple(attachments),
    )


def _match_best_row(
    candidates: Sequence[StatementRow],
    target_date_iso: str,
    amount_minor: int,
    used_indices: set[int],
    max_day_diff: int = 1,
) -> StatementRow | None:
    """Find the closest StatementRow matching amount_minor within max_day_diff days."""
    try:
        t_date = date.fromisoformat(target_date_iso)
    except ValueError:
        return None
    best: StatementRow | None = None
    best_diff = 999
    for row in candidates:
        if row.row_index in used_indices or row.amount_minor != amount_minor:
            continue
        effective_max_diff = max(max_day_diff, 1) if not row.rrn_12 else max_day_diff
        diff = abs((row.statement_date - t_date).days)
        if diff <= effective_max_diff and diff < best_diff:
            best = row
            best_diff = diff
    return best


def _parse_minor_amount(raw_amt: str) -> int | None:
    cleaned = raw_amt.replace(",", "").replace("₹", "").replace("INR", "").replace("Rs.", "").strip()
    try:
        return int((Decimal(cleaned) * 100).quantize(Decimal(1)))
    except InvalidOperation:
        return None


class SwiggyReceiptExtractor:
    """Extracts restaurant name and ordered items from Swiggy delivery confirmation emails."""

    name = "gmail_swiggy"
    imap_search_query = "from:noreply@swiggy.in"

    def extract(
        self,
        messages: Sequence[EmailMessagePayload],
        rows: Sequence[StatementRow],
    ) -> Sequence[EnrichmentEvidence]:
        swiggy_rows = [r for r in rows if "SWIGGY" in r.narration.upper()]
        if not swiggy_rows:
            return ()

        used_rows: set[int] = set()
        evidences: list[EnrichmentEvidence] = []
        for msg in messages:
            body = html.unescape(msg.text_body)
            # Match either "Paid Via ... ₹142" or "Order Total: ₹142"
            m_tot = re.search(
                r"(?:Paid\s+Via\s+[A-Za-z0-9 ]+?|Order\s+Total[:\s]*)(?:Rs\.?|₹)\s*([\d,]+(?:\.\d{1,2})?)",
                body,
                re.IGNORECASE,
            )
            if not m_tot:
                continue
            amt_minor = _parse_minor_amount(m_tot.group(1))
            if amt_minor is None:
                continue

            # Extract restaurant name after ORDER JOURNEY or Restaurant:
            rest_name = "Swiggy"
            m_journey = re.search(
                r"ORDER\s+JOURNEY\s+([A-Za-z0-9 &'\-\.]+?)\s+(?:No\.|NO\s|\d+|[A-Z][a-z]+\s+\d{1,2},)",
                body,
            )
            if m_journey:
                rest_name = m_journey.group(1).strip()
            else:
                m_rest = re.search(
                    r"Restaurant[:\s]+([A-Za-z0-9 &'\-]+?)(?:\s+Order|\s+Delivered|\n)",
                    body,
                )
                if m_rest:
                    rest_name = m_rest.group(1).strip()

            # Extract items between BILL DETAILS and Item Total / Restaurant Packaging
            items: list[str] = []
            m_bill = re.search(
                r"BILL\s+DETAILS\s+(.*?)(?:Item\s+Total|Restaurant\s+Packaging|Platform\s+fee)",
                body,
                re.DOTALL | re.IGNORECASE,
            )
            if m_bill:
                bill_chunk = m_bill.group(1)
                for im in re.finditer(
                    r"([A-Za-z0-9 &'\-\(\)/]+?)\s+x\s*(\d+)\s*₹\s*[\d,]+",
                    bill_chunk,
                ):
                    item_title = im.group(1).strip()
                    qty = im.group(2).strip()
                    items.append(f"{qty}x {item_title}")

            matched_row = _match_best_row(swiggy_rows, msg.date_iso, amt_minor, used_rows, max_day_diff=1)
            if matched_row is not None:
                used_rows.add(matched_row.row_index)
                if items:
                    items_str = "\n".join(items)
                    note = f"{items_str} ({rest_name})" if rest_name != "Swiggy" else items_str
                else:
                    note = f"Swiggy order ({rest_name})"
                evidences.append(
                    EnrichmentEvidence(
                        source_name=self.name,
                        priority=EvidencePriority.INVOICE_PDF,
                        confidence=0.98,
                        row_index=matched_row.row_index,
                        category="Meals & Fast Food",
                        payee="Swiggy",
                        note=note,
                        needs_review=False,
                    )
                )
        return evidences


class NammaYatriPdfExtractor:
    """Extracts auto ride routes, fares, and dates from Namma Yatri multi-ride PDF invoices."""

    name = "gmail_namma_yatri_pdf"
    imap_search_query = "from:nammayatri.in"

    def __init__(self, gazetteer: Gazetteer | None = None) -> None:
        self._gazetteer = gazetteer or Gazetteer()

    def extract(
        self,
        messages: Sequence[EmailMessagePayload],
        rows: Sequence[StatementRow],
    ) -> Sequence[EnrichmentEvidence]:
        if not rows:
            return ()

        # Namma Yatri rides are paid directly to auto drivers via UPI; some rows say NAMMA YATRI
        # while others only have the driver's name in narration. Prefer explicit NAMMA YATRI rows first,
        # then fall back to any debit UPI row matching date and exact fare.
        ny_explicit_rows = [
            r
            for r in rows
            if not r.is_credit
            and ("NAMMA YATRI" in r.narration.upper() or "NAMMAYATRI" in r.narration.upper())
        ]
        all_debit_rows = [r for r in rows if not r.is_credit]

        used_rows: set[int] = set()
        evidences: list[EnrichmentEvidence] = []

        for msg in messages:
            for fname, payload in msg.attachments:
                if not fname.lower().endswith(".pdf"):
                    continue
                lines = extract_pdf_lines(payload)
                rides = self._parse_rides_from_lines(lines)
                for ride_date_iso, fare_minor, pickup_raw, drop_raw in rides:
                    matched = _match_best_row(
                        ny_explicit_rows, ride_date_iso, fare_minor, used_rows, max_day_diff=1
                    )
                    if matched is None:
                        matched = _match_best_row(
                            all_debit_rows, ride_date_iso, fare_minor, used_rows, max_day_diff=0
                        )
                    if matched is not None:
                        used_rows.add(matched.row_index)
                        src = self._gazetteer.normalize_address(pickup_raw)
                        dst = self._gazetteer.normalize_address(drop_raw)
                        evidences.append(
                            EnrichmentEvidence(
                                source_name=self.name,
                                priority=EvidencePriority.INVOICE_PDF,
                                confidence=0.99,
                                row_index=matched.row_index,
                                category="Auto",
                                payee="Namma Yaatri",
                                note=f"{src} to {dst}",
                                needs_review=False,
                            )
                        )
        return evidences

    @staticmethod
    def _parse_rides_from_lines(lines: Sequence[str]) -> list[tuple[str, int, str, str]]:
        """Return list of (date_iso, fare_minor, pickup_address, drop_address)."""
        rides: list[tuple[str, int, str, str]] = []
        date_pat = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3}),\s+(\d{4})$")
        fare_pat = re.compile(r"^Ride\s+Fare:\s*₹\s*([\d,]+(?:\.\d{1,2})?)$", re.IGNORECASE)
        time_pat = re.compile(r"^\d{1,2}:\d{2}\s*(?:AM|PM)$", re.IGNORECASE)

        idx = 0
        n = len(lines)
        while idx < n:
            dm = date_pat.match(lines[idx])
            if not dm:
                idx += 1
                continue
            try:
                ride_date = datetime.strptime(
                    f"{dm.group(1)} {dm.group(2)} {dm.group(3)}", "%d %b %Y"
                ).strftime("%Y-%m-%d")
            except ValueError:
                idx += 1
                continue

            idx += 1
            fare_minor: int | None = None
            time_blocks: list[str] = []
            while idx < n and not date_pat.match(lines[idx]) and lines[idx].upper() != "DRIVER NAME":
                fm = fare_pat.match(lines[idx])
                if fm:
                    fare_minor = _parse_minor_amount(fm.group(1))
                    idx += 1
                    continue
                if time_pat.match(lines[idx]):
                    idx += 1
                    addr_parts: list[str] = []
                    while (
                        idx < n
                        and not time_pat.match(lines[idx])
                        and not date_pat.match(lines[idx])
                        and lines[idx].upper() != "DRIVER NAME"
                    ):
                        addr_parts.append(lines[idx])
                        idx += 1
                    time_blocks.append(" ".join(addr_parts))
                    continue
                idx += 1

            if fare_minor is not None and len(time_blocks) >= 2:
                rides.append((ride_date, fare_minor, time_blocks[0], time_blocks[1]))
        return rides


class RapidoReceiptExtractor:
    """Extracts Auto/Cab routes, vehicle mode, and fare from Rapido PDF receipts and HTML emails."""

    name = "gmail_rapido_receipt"
    imap_search_query = "from:rapido.bike"

    def __init__(self, gazetteer: Gazetteer | None = None) -> None:
        self._gazetteer = gazetteer or Gazetteer()

    def extract(
        self,
        messages: Sequence[EmailMessagePayload],
        rows: Sequence[StatementRow],
    ) -> Sequence[EnrichmentEvidence]:
        if not rows:
            return ()

        rapido_rows = [r for r in rows if not r.is_credit and "RAPIDO" in r.narration.upper()]
        all_debit_rows = [r for r in rows if not r.is_credit]

        used_rows: set[int] = set()
        evidences: list[EnrichmentEvidence] = []

        for msg in messages:
            # 1. Parse PDF attachments (AUTO_RECEIPT_*.pdf / CAB_RECEIPT_*.pdf)
            for fname, payload in msg.attachments:
                if not fname.lower().endswith(".pdf"):
                    continue
                lines = extract_pdf_lines(payload)
                parsed = self._parse_rapido_lines(lines, fname)
                if parsed is not None:
                    ride_date_iso, fare_minor, category, pickup_raw, drop_raw = parsed
                    matched = _match_best_row(
                        rapido_rows, ride_date_iso, fare_minor, used_rows, max_day_diff=1
                    )
                    if matched is None:
                        matched = _match_best_row(
                            all_debit_rows, ride_date_iso, fare_minor, used_rows, max_day_diff=0
                        )
                    if matched is not None:
                        used_rows.add(matched.row_index)
                        src = self._gazetteer.normalize_address(pickup_raw)
                        dst = self._gazetteer.normalize_address(drop_raw)
                        evidences.append(
                            EnrichmentEvidence(
                                source_name=self.name,
                                priority=EvidencePriority.INVOICE_PDF,
                                confidence=0.99,
                                row_index=matched.row_index,
                                category=category,
                                payee="Rapido",
                                note=f"{src} to {dst}",
                                needs_review=False,
                            )
                        )

            # 2. Parse inline HTML/text body if no PDF attachments were present
            if not msg.attachments and "Selected Price" in msg.text_body:
                parsed_body = self._parse_rapido_body(msg.text_body)
                if parsed_body is not None:
                    ride_date_iso, fare_minor, category, pickup_raw, drop_raw = parsed_body
                    matched = _match_best_row(
                        rapido_rows, ride_date_iso, fare_minor, used_rows, max_day_diff=1
                    )
                    if matched is not None:
                        used_rows.add(matched.row_index)
                        src = self._gazetteer.normalize_address(pickup_raw)
                        dst = self._gazetteer.normalize_address(drop_raw)
                        evidences.append(
                            EnrichmentEvidence(
                                source_name=self.name,
                                priority=EvidencePriority.INVOICE_PDF,
                                confidence=0.98,
                                row_index=matched.row_index,
                                category=category,
                                payee="Rapido",
                                note=f"{src} to {dst}",
                                needs_review=False,
                            )
                        )

        return evidences

    @staticmethod
    def _parse_rapido_date(raw_dt: str) -> str | None:
        # e.g. "Apr 25th 2026, 6:53 PM" or "May 16th 2026, 8:39 PM"
        cleaned = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", raw_dt.strip())
        part = cleaned.split(",")[0].strip()
        try:
            return datetime.strptime(part, "%b %d %Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    @classmethod
    def _parse_rapido_lines(
        cls, lines: Sequence[str], filename: str
    ) -> tuple[str, int, str, str, str] | None:
        """Return (date_iso, fare_minor, category, pickup_raw, drop_raw)."""
        mode = "Cab" if "CAB" in filename.upper() else "Auto"
        ride_date_iso: str | None = None
        fare_minor: int | None = None
        addr_lines: list[str] = []

        idx = 0
        n = len(lines)
        while idx < n:
            ln = lines[idx].strip()
            if ln.lower() == "mode of vehicle" and idx + 1 < n:
                val = lines[idx + 1].strip().upper()
                mode = "Cab" if ("CAR" in val or "CAB" in val) else "Auto"
                idx += 2
                continue
            if ln.lower() == "time of ride" and idx + 1 < n:
                ride_date_iso = cls._parse_rapido_date(lines[idx + 1])
                idx += 2
                continue
            if ln.lower() == "selected price":
                idx += 1
                while idx < n and lines[idx].strip() in ("₹", ""):
                    idx += 1
                if idx < n:
                    fare_minor = _parse_minor_amount(lines[idx])
                    idx += 1
                while (
                    idx < n
                    and not lines[idx].startswith("This document")
                    and not lines[idx].startswith("*Selected")
                ):
                    addr_lines.append(lines[idx].strip())
                    idx += 1
                break
            idx += 1

        if not ride_date_iso or fare_minor is None or not addr_lines:
            return None

        # Group address lines into pickup and dropoff (each ends with 'India')
        joined = " ".join(addr_lines)
        parts = [p.strip(" ,") for p in re.split(r"\bIndia\b", joined) if p.strip(" ,")]
        if len(parts) >= 2:
            return (ride_date_iso, fare_minor, mode, parts[0], parts[1])
        return None

    @classmethod
    def _parse_rapido_body(cls, body: str) -> tuple[str, int, str, str, str] | None:
        m_mode = re.search(r"Mode of Vehicle\s+([A-Za-z]+)", body)
        mode = "Cab" if (m_mode and m_mode.group(1).upper() in ("CAR", "CAB")) else "Auto"
        m_time = re.search(r"Time of Ride\s+([A-Za-z]{3}\s+\d+[a-z]{2}\s+\d{4},\s*[\d:]+\s*[AP]M)", body)
        if not m_time:
            return None
        ride_date_iso = cls._parse_rapido_date(m_time.group(1))
        m_price = re.search(r"Selected Price\s*₹\s*([\d,]+(?:\.\d{1,2})?)", body)
        if not ride_date_iso or not m_price:
            return None
        fare_minor = _parse_minor_amount(m_price.group(1))
        if fare_minor is None:
            return None
        m_locs = re.search(
            r"Selected Price\s*₹\s*[\d,]+(?:\.\d{1,2})?\s+(.*?)\s+This document is issued",
            body,
            re.DOTALL,
        )
        if not m_locs:
            return None
        parts = [p.strip(" ,") for p in re.split(r"\bIndia\b", m_locs.group(1)) if p.strip(" ,")]
        if len(parts) >= 2:
            return (ride_date_iso, fare_minor, mode, parts[0], parts[1])
        return None


class AmazonEmailExtractor:
    """Extracts ordered items, quantities, and categories from Amazon order confirmation emails."""

    name = "gmail_amazon_order"
    imap_search_query = "from:auto-confirm@amazon.in"

    def extract(
        self,
        messages: Sequence[EmailMessagePayload],
        rows: Sequence[StatementRow],
    ) -> Sequence[EnrichmentEvidence]:
        amazon_rows = [r for r in rows if not r.is_credit and "AMAZON" in r.narration.upper()]
        if not amazon_rows:
            return ()

        used_rows: set[int] = set()
        evidences: list[EnrichmentEvidence] = []

        for msg in messages:
            sub_orders = self._parse_amazon_email(msg.text_body)
            if not sub_orders:
                continue

            # Try matching individual sub-orders first, as well as the combined total across all sub-orders in the email
            combined_total = sum(amt for amt, _ in sub_orders)
            combined_items: list[str] = [it for _, items in sub_orders for it in items]

            if len(sub_orders) > 1:
                matched_combined = _match_best_row(
                    amazon_rows, msg.date_iso, combined_total, used_rows, max_day_diff=2
                )
                if matched_combined is not None:
                    used_rows.add(matched_combined.row_index)
                    note = "\n".join(combined_items)
                    evidences.append(
                        EnrichmentEvidence(
                            source_name=self.name,
                            priority=EvidencePriority.INVOICE_PDF,
                            confidence=0.98,
                            row_index=matched_combined.row_index,
                            category=self.classify_amazon_items(note),
                            payee="Amazon",
                            note=note,
                            needs_review=False,
                        )
                    )
                    continue

            for amt_minor, items in sub_orders:
                if amt_minor <= 0 or not items:
                    continue
                matched = _match_best_row(amazon_rows, msg.date_iso, amt_minor, used_rows, max_day_diff=2)
                if matched is not None:
                    used_rows.add(matched.row_index)
                    note = "\n".join(items)
                    evidences.append(
                        EnrichmentEvidence(
                            source_name=self.name,
                            priority=EvidencePriority.INVOICE_PDF,
                            confidence=0.98,
                            row_index=matched.row_index,
                            category=self.classify_amazon_items(note),
                            payee="Amazon",
                            note=note,
                            needs_review=False,
                        )
                    )

        return evidences

    @staticmethod
    def _parse_amazon_email(text: str) -> list[tuple[int, list[str]]]:
        """Parse an Amazon confirmation email into [(sub_order_total_minor, ['1x Item Name', ...]), ...]."""
        results: list[tuple[int, list[str]]] = []
        blocks = re.split(r"Order\s*#\s*[\d\-]+", text)
        for block in blocks[1:]:
            items: list[str] = []
            for im in re.finditer(
                r"\*\s+([^\r\n]+)\r?\n\s*Quantity:\s*(\d+)",
                block,
            ):
                raw_title = " ".join(im.group(1).split()).strip()
                # Shorten overly verbose SEO titles at first pipe or comma-heavy description
                short_title = raw_title.split("|")[0].split(" - ")[0].strip()
                if len(short_title) > 75:
                    short_title = short_title[:72].rstrip() + "..."
                qty = im.group(2).strip()
                items.append(f"{qty}x {short_title}")

            m_tot = re.search(r"Total\s*\r?\n\s*([\d,]+(?:\.\d{1,2})?)\s*INR", block)
            if m_tot and items:
                amt = _parse_minor_amount(m_tot.group(1))
                if amt is not None:
                    results.append((amt, items))
        return results

    @staticmethod
    def classify_amazon_items(note: str) -> str:
        upper = note.upper()
        if any(
            kw in upper
            for kw in (
                "TABLET",
                "CAPSULE",
                "MG ",
                "DOLO",
                "ECOSPRIN",
                "BRILINTA",
                "ROSUVAS",
                "EPTUS",
                "ISTAMET",
                "CONCOR",
                "BECOSULES",
                "LASIX",
            )
        ):
            return "Drug-store, chemist"
        if any(
            kw in upper
            for kw in (
                "FRESH ",
                "CURD",
                "MILK",
                "PARATHA",
                "NOODLES",
                "EGGS",
                "YOGURT",
                "CHAPATI",
                "BISLERI",
                "PEANUTS",
                "DAL ",
                "OIL",
                "DETERGENT",
            )
        ):
            return "Groceries"
        if any(
            kw in upper
            for kw in (
                "USB",
                "ETHERNET",
                "ADAPTER",
                "SENSOR",
                "RADAR",
                "ROBOT VACUUM",
                "HEADPHONES",
                "SONY",
                "AQARA",
                "DREAME",
            )
        ):
            return "Electronics, accessories"
        if any(kw in upper for kw in ("FACE MASK", "FACE WASH", "COMPACT", "CREAM", "OLAY", "LAKME", "SOAP")):
            return "Wellness, beauty"
        if any(kw in upper for kw in ("JOCKEY", "BIKINI", "SHIRT", "PANTS")):
            return "Clothes & Footwear"
        if any(kw in upper for kw in ("BOOK PAGE", "BOOK")):
            return "Books, audio, subscriptions"
        if any(kw in upper for kw in ("PULL UP BAR", "CUTLERY", "APRON")):
            return "Home, garden"
        return "Shopping"


class TravelBookingExtractor:
    """Extracts train, flight, hotel, and activity bookings from ConfirmTkt, IRCTC, MakeMyTrip, and Klook."""

    name = "gmail_travel_booking"
    imap_search_query = "(from:confirmtkt.com OR from:irctc.co.in OR from:makemytrip.com OR from:klook.com)"

    def extract(
        self,
        messages: Sequence[EmailMessagePayload],
        rows: Sequence[StatementRow],
    ) -> Sequence[EnrichmentEvidence]:
        if not rows:
            return ()

        used_rows: set[int] = set()
        evidences: list[EnrichmentEvidence] = []

        for msg in messages:
            sender_l = msg.sender.lower()
            if "confirmtkt" in sender_l:
                ev = self._extract_confirmtkt(msg, rows, used_rows)
                if ev is not None:
                    evidences.append(ev)
            elif "makemytrip" in sender_l:
                ev = self._extract_makemytrip(msg, rows, used_rows)
                if ev is not None:
                    evidences.append(ev)
            elif "klook" in sender_l:
                ev = self._extract_klook(msg, rows, used_rows)
                if ev is not None:
                    evidences.append(ev)

        return evidences

    def _extract_confirmtkt(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
    ) -> EnrichmentEvidence | None:
        body = msg.text_body
        m_route = re.search(
            r"between\s*\(([A-Z]{2,5})\)\s*and\s*\(([A-Z]{2,5})\)\s*on\s*(\d{1,2}\s+[A-Za-z]{3})\s+\d{4}",
            body,
        )
        m_pnr = re.search(r"Confirmtkt\s+Booking\s+ID\s*(\d{10})", body)
        m_train = re.search(r"Class/Quota\s*([A-Z0-9\s\-]+)/(\d{5})\s*([A-Z0-9]+)/", body)
        m_fare = re.search(r"Total\s+Fare\s*₹\s*([\d,]+(?:\.\d{1,2})?)", body)
        if not (m_route and m_pnr and m_fare):
            return None

        amt_minor = _parse_minor_amount(m_fare.group(1))
        if amt_minor is None:
            return None

        src_code, dst_code, travel_dt = m_route.group(1), m_route.group(2), m_route.group(3)
        pnr = m_pnr.group(1)
        train_name = m_train.group(1).strip().title() if m_train else "Train"
        train_no = m_train.group(2).strip() if m_train else ""
        coach_cls = m_train.group(3).strip() if m_train else ""

        station_names = {"SBC": "Bangalore (SBC)", "ASK": "Arsikere (ASK)"}
        src_label = station_names.get(src_code, src_code)
        dst_label = station_names.get(dst_code, dst_code)

        candidates = [
            r
            for r in rows
            if not r.is_credit and ("CONFIRMTKT" in r.narration.upper() or "IRCTC" in r.narration.upper())
        ]
        matched = _match_best_row(candidates, msg.date_iso, amt_minor, used_rows, max_day_diff=2)
        if matched is None:
            return None

        used_rows.add(matched.row_index)
        note = f"{src_label} to {dst_label} - {train_name} {train_no} ({coach_cls}, {travel_dt}, PNR {pnr})"
        return EnrichmentEvidence(
            source_name=f"{self.name}:confirmtkt",
            priority=EvidencePriority.INVOICE_PDF,
            confidence=0.99,
            row_index=matched.row_index,
            category="Train",
            payee="ConfirmTkt",
            note=note,
            needs_review=False,
        )

    def _extract_makemytrip(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
    ) -> EnrichmentEvidence | None:
        m_hotel = re.search(
            r"(NH\d+)\s*:\s*You have a message from\s+(.+)",
            msg.subject,
        )
        if not m_hotel:
            return None
        booking_id = m_hotel.group(1).strip()
        hotel_name = " ".join(m_hotel.group(2).split()).strip()
        mmt_rows = [
            r
            for r in rows
            if not r.is_credit and "MAKEMYTRIP" in r.narration.upper() and r.row_index not in used_rows
        ]
        for row in mmt_rows:
            if abs((row.statement_date - date.fromisoformat(msg.date_iso)).days) <= 1:
                used_rows.add(row.row_index)
                return EnrichmentEvidence(
                    source_name=f"{self.name}:makemytrip",
                    priority=EvidencePriority.INVOICE_PDF,
                    confidence=0.96,
                    row_index=row.row_index,
                    category="Holiday, trips, hotels",
                    payee="MakeMyTrip",
                    note=f"{hotel_name} (Booking {booking_id})",
                    needs_review=False,
                )
        return None

    def _extract_klook(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
    ) -> EnrichmentEvidence | None:
        m_act = re.search(
            r"(?:booking\s+for|confirmed[:\s]+)\s*([A-Za-z0-9 &'\-\(\)/]+)",
            msg.subject,
            re.IGNORECASE,
        )
        activity_name = m_act.group(1).strip() if m_act else msg.subject.strip()
        klook_rows = [
            r
            for r in rows
            if not r.is_credit and "KLOOK" in r.narration.upper() and r.row_index not in used_rows
        ]
        for row in klook_rows:
            if abs((row.statement_date - date.fromisoformat(msg.date_iso)).days) <= 1:
                used_rows.add(row.row_index)
                return EnrichmentEvidence(
                    source_name=f"{self.name}:klook",
                    priority=EvidencePriority.INVOICE_PDF,
                    confidence=0.96,
                    row_index=row.row_index,
                    category="Culture, sport events",
                    payee="Klook",
                    note=activity_name,
                    needs_review=False,
                )
        return None


DEFAULT_INVESTMENT_PDF_PASSWORDS: tuple[str, ...] = (
    "DTQPD8308L",
    "dtqpd8308l",
    *DEFAULT_CC_PDF_PASSWORDS,
)


class InvestmentEmailExtractor:
    """Extracts Gullak gold certificates, INDmoney (US Stocks, Indian Stocks, MFs), CDSL/KFintech/MUFG IPOs & REITs, and Shriram Finance FDs."""

    name = "gmail_investments"
    imap_search_query = (
        "("
        "(from:no-reply@gullak.money subject:(Investment Certificate)) "
        "OR from:shriramfinance.in "
        "OR (from:kfintech.com (Allotment OR Distribution OR Purchase)) "
        "OR (from:mpms.mufg.com Allotment) "
        "OR (from:services@cdslindia.co.in subject:(Transactions In Your Demat Account)) "
        "OR (from:transactions.indmoney.com (subject:(Trade Report) OR subject:(Contract Note) OR subject:(INDstocks account funded) OR subject:(Units allotted)))"
        ")"
    )

    def __init__(self, passwords: Sequence[str] = DEFAULT_INVESTMENT_PDF_PASSWORDS) -> None:
        self._passwords = tuple(passwords)

    def extract(
        self,
        messages: Sequence[EmailMessagePayload],
        rows: Sequence[StatementRow],
    ) -> Sequence[EnrichmentEvidence]:
        if not rows:
            return ()

        used_rows: set[int] = set()
        used_ts_rows: set[int] = set()
        used_cn_rows: set[int] = set()
        used_cdsl_rows: set[int] = set()
        used_us_rows: set[int] = set()
        evidences: list[EnrichmentEvidence] = []

        for msg in messages:
            sender_l = msg.sender.lower()
            subj_l = msg.subject.lower()
            if "gullak.money" in sender_l:
                ev = self._extract_gullak(msg, rows, used_rows)
                if ev is not None:
                    evidences.append(ev)
            elif "shriramfinance" in sender_l:
                ev = self._extract_shriram(msg, rows, used_rows)
                if ev is not None:
                    evidences.append(ev)
            elif "transactions.indmoney.com" in sender_l:
                if "trade report" in subj_l:
                    evidences.extend(self._extract_indmoney_us_stocks(msg, rows, used_us_rows))
                elif "contract note" in subj_l:
                    ev = self._extract_indmoney_contract_note(msg, rows, used_cn_rows)
                    if ev is not None:
                        evidences.append(ev)
                elif "indstocks account funded" in subj_l:
                    ev = self._extract_indstocks_wallet_deposit(msg, rows, used_ts_rows)
                    if ev is not None:
                        evidences.append(ev)
                elif "mutual fund" in subj_l:
                    ev = self._extract_indmoney_mutual_fund(msg, rows, used_rows)
                    if ev is not None:
                        evidences.append(ev)
            elif "services@cdslindia.co.in" in sender_l:
                evidences.extend(self._extract_cdsl_demat(msg, rows, used_cdsl_rows, used_cn_rows))
            elif "kfintech" in sender_l or "mufg.com" in sender_l:
                if "distribution advice" in subj_l:
                    ev = self._extract_reit_distribution(msg, rows, used_rows)
                    if ev is not None:
                        evidences.append(ev)
                elif "purchase transaction" in subj_l:
                    ev = self._extract_kfintech_mutual_fund(msg, rows, used_rows)
                    if ev is not None:
                        evidences.append(ev)
                else:
                    ev = self._extract_ipo_allotment(msg, rows, used_rows)
                    if ev is not None:
                        evidences.append(ev)

        return evidences

    def _extract_gullak(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
    ) -> EnrichmentEvidence | None:
        m = re.search(
            r"investment\s+of\s+([\d.]+)\s*grams?\s+of\s+gold\s+in\s+\*?([A-Za-z0-9+ ]+?)\*?\s+done\s+on\s+(\d{2}/\d{2}/\d{4})",
            msg.text_body,
            re.IGNORECASE,
        )
        if not m:
            return None
        grams_str = m.group(1).strip()
        product = m.group(2).strip()
        date_ddmmyyyy = m.group(3).strip()
        try:
            target_date = datetime.strptime(date_ddmmyyyy, "%d/%m/%Y").date()
        except ValueError:
            target_date = date.fromisoformat(msg.date_iso)

        candidates = [
            r
            for r in rows
            if not r.is_credit
            and r.row_index not in used_rows
            and ("INTERSCALE" in r.narration.upper() or "GULLAK" in r.narration.upper())
            and abs((r.statement_date - target_date).days) <= 1
        ]
        if not candidates:
            return None

        candidates.sort(key=lambda r: abs((r.statement_date - target_date).days))
        matched = candidates[0]
        used_rows.add(matched.row_index)
        ts_iso = msg.sent_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        return EnrichmentEvidence(
            source_name=f"{self.name}:gullak",
            priority=EvidencePriority.INVOICE_PDF,
            confidence=0.99,
            row_index=matched.row_index,
            timestamp_iso=ts_iso,
            kind=TransactionKind.TRANSFER,
            transfer_account="Investments",
            category="Transfer, withdraw",
            payee="Gullak",
            note=f"Gullak {product} purchase: {grams_str}g gold (via CCAF Interscale Technologies)",
            needs_review=False,
        )

    def _extract_indmoney_us_stocks(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_us_rows: set[int],
    ) -> list[EnrichmentEvidence]:
        remit_rows = [
            r
            for r in rows
            if not r.is_credit
            and r.row_index not in used_us_rows
            and "RFX" in r.narration.upper()
            and "BTT" in r.narration.upper()
        ]
        if not remit_rows:
            return []

        trades: list[tuple[date, str]] = []
        for fname, payload in msg.attachments:
            if not fname.lower().endswith(".xls"):
                continue
            grid = parse_biff8_xls_rows(payload)
            header_idx = -1
            for idx, r_vals in enumerate(grid):
                if len(r_vals) >= 10 and str(r_vals[0]).strip() == "Stock Name":
                    header_idx = idx
                    break
            if header_idx == -1:
                continue

            for r_vals in grid[header_idx + 1 :]:
                if len(r_vals) < 11:
                    continue
                stock_name = str(r_vals[0]).strip()
                symbol = str(r_vals[1]).strip()
                exec_time_str = str(r_vals[3]).strip()
                tx_type = str(r_vals[5]).strip().upper()
                if not stock_name or not symbol or tx_type not in ("BUY", "SELL"):
                    continue
                date_part = exec_time_str.split(",")[0].strip()
                try:
                    trade_dt = datetime.strptime(date_part, "%d %b %Y").date()
                except ValueError:
                    continue
                try:
                    qty = float(r_vals[7])
                    price = float(r_vals[8])
                    order_amt = float(r_vals[9])
                    fee = float(r_vals[10])
                except TypeError, ValueError:
                    continue

                clean_stock = re.sub(
                    r"\s+(?:Class\s+[A-Z]\s+)?Common\s+Stock$", "", stock_name, flags=re.IGNORECASE
                ).strip()
                qty_s = f"{qty:g}"
                price_s = f"{price:.4f}".rstrip("0").rstrip(".")
                if "." not in price_s:
                    price_s = f"{price:.2f}"
                line = (
                    f"- {clean_stock} ({symbol}): {qty_s} qty @ ${price_s} = "
                    f"${order_amt:,.2f} + ${fee:,.2f} fee ({date_part})"
                )
                trades.append((trade_dt, line))

        if not trades:
            return []

        remit_rows.sort(key=lambda r: r.statement_date)
        evidences: list[EnrichmentEvidence] = []
        for idx, r_row in enumerate(remit_rows):
            next_dt = (
                remit_rows[idx + 1].statement_date
                if idx + 1 < len(remit_rows)
                else r_row.statement_date + timedelta(days=45)
            )
            window_end = min(next_dt, r_row.statement_date + timedelta(days=45))
            matched_lines = [line for t_dt, line in trades if r_row.statement_date <= t_dt < window_end]
            if not matched_lines:
                continue

            m_rfx = re.search(
                r"RFX\s+([A-Z0-9]+BTT[A-Z0-9]+)\s+USD([\d.]+)@([\d.]+)",
                r_row.narration,
                re.IGNORECASE,
            )
            if m_rfx:
                rfx_ref = m_rfx.group(1).upper()
                usd_val = float(m_rfx.group(2))
                rate_str = m_rfx.group(3)
                header = f"US Stocks in IndMoney: INR to USD (${usd_val:,.2f} at {rate_str} INR/USD, RFX {rfx_ref})"
            else:
                header = "US Stocks in IndMoney"

            used_us_rows.add(r_row.row_index)
            evidences.append(
                EnrichmentEvidence(
                    source_name=f"{self.name}:indmoney_us_stocks",
                    priority=EvidencePriority.INVOICE_PDF,
                    confidence=0.99,
                    row_index=r_row.row_index,
                    kind=TransactionKind.TRANSFER,
                    transfer_account="Investments",
                    category="Transfer, withdraw",
                    payee="IndMoney",
                    note=f"{header} // " + " // ".join(matched_lines),
                    needs_review=False,
                )
            )
        return evidences

    def _extract_indstocks_wallet_deposit(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_ts_rows: set[int],
    ) -> EnrichmentEvidence | None:
        clean_body = " ".join(re.sub(r"\{[^}]*\}", " ", msg.text_body).split())
        m_amt = re.search(r"deposit\s+of\s+₹\s*([\d,]+(?:\.\d{1,2})?)", clean_body, re.IGNORECASE)
        if not m_amt:
            m_amt = re.search(r"funded\s+with\s+₹\s*([\d,]+(?:\.\d{1,2})?)", msg.subject, re.IGNORECASE)
        if not m_amt:
            return None
        amt_minor = _parse_minor_amount(m_amt.group(1))
        if amt_minor is None:
            return None

        ts_iso: str | None = None
        m_dt = re.search(
            r"Date\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)",
            clean_body,
            re.IGNORECASE,
        )
        if m_dt:
            raw_dt = f"{m_dt.group(1)} {m_dt.group(2).upper().replace(' ', '')}"
            try:
                dt_ist = datetime.strptime(raw_dt, "%b %d, %Y %I:%M%p").replace(tzinfo=_IST_TZ)
                ts_iso = dt_ist.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                ts_iso = None

        candidates = [r for r in rows if not r.is_credit and "INDSTOCKS" in r.narration.upper()]
        matched = _match_best_row(candidates, msg.date_iso, amt_minor, used_ts_rows, max_day_diff=1)
        if matched is None:
            return None

        used_ts_rows.add(matched.row_index)
        return EnrichmentEvidence(
            source_name=f"{self.name}:indstocks_deposit",
            priority=EvidencePriority.INVOICE_PDF,
            confidence=0.96,
            row_index=matched.row_index,
            timestamp_iso=ts_iso or msg.sent_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            kind=TransactionKind.TRANSFER,
            transfer_account="Investments",
            category="Transfer, withdraw",
            payee="IndMoney",
            note="Indian Stocks in IndMoney: INDstocks wallet deposit",
            needs_review=False,
        )

    def _extract_indmoney_contract_note(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_cn_rows: set[int],
    ) -> EnrichmentEvidence | None:
        m_td = re.search(r"trades\s+on\s+(\d{2}/\d{2}/\d{4})", msg.subject, re.IGNORECASE)
        if m_td:
            try:
                trade_date_iso = datetime.strptime(m_td.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                trade_date_iso = msg.date_iso
        else:
            trade_date_iso = msg.date_iso

        for fname, payload in msg.attachments:
            if not fname.lower().endswith(".pdf"):
                continue
            try:
                pdf_text = extract_pdf_text(payload, passwords=self._passwords)
            except ValueError:
                continue
            if not pdf_text:
                continue

            m_net = re.search(
                r"Net\s+Amount\s+Receivable/Payable\s+By\s+Client\s+([\d,]+(?:\.\d{2})?)\s*DR",
                pdf_text,
                re.IGNORECASE,
            )
            m_ob = re.search(
                r"Pay\s+In/Pay\s+Out\s+Obligation\s+([\d,]+(?:\.\d{2})?)\s*DR",
                pdf_text,
                re.IGNORECASE,
            )
            m_sec = re.search(
                r"(IN[EF][A-Z0-9]{9})\s+(\d+)\s+([\d.]+)\s+[\d.\s]+?\s+[\d.]+\s+([\d.]+)\s+\d+\s+-?[\d.]+\s+([A-Z0-9 &\-/]+?)\s+ISIN",
                pdf_text,
                re.DOTALL,
            )
            if not (m_net and m_sec):
                continue

            net_payable = Decimal(m_net.group(1).replace(",", ""))
            obligation = Decimal(m_ob.group(1).replace(",", "")) if m_ob else Decimal(m_sec.group(4))
            charges = net_payable - obligation
            qty_str = m_sec.group(2).strip()
            wap_str = m_sec.group(3).strip()
            sec_raw = " ".join(m_sec.group(5).split()).split("/")[0].strip().title()
            sec_clean = sec_raw.replace("Etf", "ETF").replace("Bees", "BeES")

            t_date = date.fromisoformat(trade_date_iso)
            candidates = [
                r
                for r in rows
                if not r.is_credit
                and r.row_index not in used_cn_rows
                and "INDSTOCKS" in r.narration.upper()
                and "ICCL" not in r.narration.upper()
                and abs((r.statement_date - t_date).days) <= 2
            ]
            if not candidates:
                continue

            candidates.sort(key=lambda r: abs((r.statement_date - t_date).days))
            matched = candidates[0]
            used_cn_rows.add(matched.row_index)
            dt_human = t_date.strftime("%d %b %Y")
            note = (
                f"Indian Stocks in IndMoney: INDstocks wallet deposit // "
                f"- {sec_clean}: {qty_str} qty @ ₹{wap_str} = ₹{obligation:,.2f} ({dt_human}) // "
                f"- Brokerage & charges: ₹{charges:,.2f}"
            )
            return EnrichmentEvidence(
                source_name=f"{self.name}:indmoney_contract_note",
                priority=EvidencePriority.INVOICE_PDF,
                confidence=0.99,
                row_index=matched.row_index,
                kind=TransactionKind.TRANSFER,
                transfer_account="Investments",
                category="Transfer, withdraw",
                payee="IndMoney",
                note=note,
                needs_review=False,
            )
        return None

    def _extract_indmoney_mutual_fund(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
    ) -> EnrichmentEvidence | None:
        clean_body = " ".join(re.sub(r"\{[^}]*\}", " ", msg.text_body).split())
        m_fund = re.search(
            r"Fund:\s*(.+?)\s+Folio\s+Number\s+(\S+)\s+Order\s+ID\s+(\S+).*?"
            r"Units\s+allot+ed\s+([\d.]+)\s+Allotted\s+NAV\s+([\d.]+)\s+Order\s+date\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
            clean_body,
            re.IGNORECASE,
        )
        if not m_fund:
            return None

        fund_name = m_fund.group(1).strip()
        folio = m_fund.group(2).strip()
        units_str = m_fund.group(4).strip()
        nav_str = m_fund.group(5).strip()
        order_date_str = m_fund.group(6).strip()
        try:
            order_dt = datetime.strptime(order_date_str, "%d %b %Y").date()
        except ValueError:
            order_dt = date.fromisoformat(msg.date_iso)

        try:
            net_val = Decimal(units_str) * Decimal(nav_str)
            # Mutual fund purchases carry 0.005% statutory stamp duty
            gross_minor = int(((net_val / Decimal("0.99995")).quantize(Decimal(1))) * 100)
        except InvalidOperation:
            return None

        candidates = [
            r
            for r in rows
            if not r.is_credit
            and r.row_index not in used_rows
            and abs((r.statement_date - order_dt).days) <= 2
            and abs(r.amount_minor - gross_minor) <= 20000  # within ₹200 rounding tolerance
        ]
        if not candidates:
            return None

        candidates.sort(
            key=lambda r: (abs(r.amount_minor - gross_minor), abs((r.statement_date - order_dt).days))
        )
        matched = candidates[0]
        used_rows.add(matched.row_index)
        return EnrichmentEvidence(
            source_name=f"{self.name}:indmoney_mf",
            priority=EvidencePriority.INVOICE_PDF,
            confidence=0.99,
            row_index=matched.row_index,
            kind=TransactionKind.TRANSFER,
            transfer_account="Investments",
            category="Transfer, withdraw",
            payee="IndMoney",
            note=f"Mutual Fund in IndMoney: {fund_name} ({units_str} units @ ₹{nav_str} NAV, Folio {folio})",
            needs_review=False,
        )

    def _extract_kfintech_mutual_fund(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
    ) -> EnrichmentEvidence | None:
        body = " ".join(msg.text_body.split())
        m_quant = re.search(
            r"Scheme\s+Details\s+(.+?)\s+NAV\s+Date\s+(\d{2}/\d{2}/\d{4})\s+"
            r"NAV\s*\(Rs\.\s*per\s*unit\)\s*([\d.]+)\s+Units\s*\(Nos\.\)\s*Allotted\s*([\d.]+)\s+"
            r"Amount\s*\(Rs\.\)\s*/\s*units\s*([\d,]+(?:\.\d{1,2})?)",
            body,
            re.IGNORECASE,
        )
        if m_quant:
            scheme = m_quant.group(1).strip()
            if scheme.lower().startswith("quant "):
                scheme = "Quant " + scheme[6:]
            nav_date_str = m_quant.group(2).strip()
            nav_str = m_quant.group(3).strip()
            units_str = m_quant.group(4).strip()
            net_amt_str = m_quant.group(5).replace(",", "").strip()
            try:
                t_dt = datetime.strptime(nav_date_str, "%d/%m/%Y").date()
                net_dec = Decimal(net_amt_str)
                gross_minor = int(((net_dec / Decimal("0.99995")).quantize(Decimal(1))) * 100)
            except ValueError, InvalidOperation:
                return None

            candidates = [
                r
                for r in rows
                if not r.is_credit
                and r.row_index not in used_rows
                and abs((r.statement_date - t_dt).days) <= 2
                and (r.amount_minor == int(net_dec * 100) or abs(r.amount_minor - gross_minor) <= 2000)
            ]
            if candidates:
                matched = candidates[0]
                used_rows.add(matched.row_index)
                return EnrichmentEvidence(
                    source_name=f"{self.name}:kfintech_mf",
                    priority=EvidencePriority.INVOICE_PDF,
                    confidence=0.98,
                    row_index=matched.row_index,
                    kind=TransactionKind.TRANSFER,
                    transfer_account="Investments",
                    category="Transfer, withdraw",
                    payee="IndMoney",
                    note=f"Mutual Fund in IndMoney: {scheme} ({units_str} units @ ₹{nav_str} NAV)",
                    needs_review=False,
                )

        m_generic = re.search(
            r"purchase\s+in\s+(.+?)\s+We\s+confirm\s+having\s+processed.*?transaction\s+date\s+(\d{2}/\d{2}/\d{4}).*?"
            r"Folio\s+Number\s*:\s*(\S+)\s+Amount\s*\(Rs\.\)\s*:\s*([\d,]+(?:\.\d{1,2})?)",
            body,
            re.IGNORECASE,
        )
        if m_generic:
            scheme = m_generic.group(1).strip()
            tx_date_str = m_generic.group(2).strip()
            folio = m_generic.group(3).strip()
            net_amt_str = m_generic.group(4).replace(",", "").strip()
            try:
                t_dt = datetime.strptime(tx_date_str, "%d/%m/%Y").date()
                net_dec = Decimal(net_amt_str)
                gross_minor = int(((net_dec / Decimal("0.99995")).quantize(Decimal(1))) * 100)
            except ValueError, InvalidOperation:
                return None
            candidates = [
                r
                for r in rows
                if not r.is_credit
                and r.row_index not in used_rows
                and abs((r.statement_date - t_dt).days) <= 2
                and (r.amount_minor == int(net_dec * 100) or abs(r.amount_minor - gross_minor) <= 2000)
            ]
            if candidates:
                matched = candidates[0]
                used_rows.add(matched.row_index)
                return EnrichmentEvidence(
                    source_name=f"{self.name}:kfintech_mf",
                    priority=EvidencePriority.INVOICE_PDF,
                    confidence=0.98,
                    row_index=matched.row_index,
                    kind=TransactionKind.TRANSFER,
                    transfer_account="Investments",
                    category="Transfer, withdraw",
                    payee="IndMoney",
                    note=f"Mutual Fund in IndMoney: {scheme} (Folio {folio})",
                    needs_review=False,
                )
        return None

    def _extract_cdsl_demat(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
        used_cn_rows: set[int],
    ) -> list[EnrichmentEvidence]:
        body = " ".join(msg.text_body.split())
        evidences: list[EnrichmentEvidence] = []
        for m in re.finditer(
            r"\d+\s+([A-Z0-9 #&\-\./\(\)]+?)\s+(IN[EF][A-Z0-9]{9})\s+([\d.]+)\s+(Credit|Debit)\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})",
            body,
            re.IGNORECASE,
        ):
            raw_company = re.sub(
                r"^.*?Date\s+and\s+Time\s+\d+\s+",
                "",
                m.group(1),
                flags=re.IGNORECASE,
            ).strip()
            qty_dec = Decimal(m.group(3))
            direction = m.group(4).upper()
            dt_str = m.group(5)
            if direction != "CREDIT" or qty_dec <= 0:
                continue
            try:
                credit_dt = datetime.strptime(dt_str, "%d/%m/%Y").date()
            except ValueError:
                continue

            qty_int = int(qty_dec)
            company_part = raw_company.split("#")[0].strip().title()
            sub_part = raw_company.split("#")[1].strip().title() if "#" in raw_company else ""

            # 1. Check if this CDSL credit corresponds to an ASBA IPO debit row on the same date (±1 day)
            ipo_candidates = [
                r
                for r in rows
                if not r.is_credit
                and r.row_index not in used_rows
                and (r.narration.endswith("-50200021339924") or "IPO" in r.narration.upper())
                and abs((r.statement_date - credit_dt).days) <= 1
                and (r.amount_minor % (qty_int * 100) == 0)
            ]
            if ipo_candidates:
                matched = ipo_candidates[0]
                used_rows.add(matched.row_index)
                price_inr = (matched.amount_minor // 100) // qty_int
                m_appl = re.search(r"UPI-\d+-[A-Z]*(\d{7,12})-50200021339924", matched.narration)
                appl_suffix = f", Appl {m_appl.group(1)}" if m_appl else ""
                unit_label = "units" if "REIT" in raw_company.upper() else "shares"
                prefix = "REIT IPO in IndMoney" if "REIT" in raw_company.upper() else "IPO in IndMoney"
                evidences.append(
                    EnrichmentEvidence(
                        source_name=f"{self.name}:cdsl_demat",
                        priority=EvidencePriority.INVOICE_PDF,
                        confidence=0.98,
                        row_index=matched.row_index,
                        kind=TransactionKind.TRANSFER,
                        transfer_account="Investments",
                        category="Transfer, withdraw",
                        payee="IndMoney",
                        note=f"{prefix}: {company_part} ({qty_int} {unit_label} @ ₹{price_inr:,}{appl_suffix})",
                        needs_review=False,
                    )
                )
                continue

            # 2. Check if this CDSL credit corresponds to an INDstocks wallet deposit within 0..2 days
            stock_candidates = [
                r
                for r in rows
                if not r.is_credit
                and r.row_index not in used_cn_rows
                and "INDSTOCKS" in r.narration.upper()
                and "ICCL" not in r.narration.upper()
                and 0 <= (credit_dt - r.statement_date).days <= 2
            ]
            if stock_candidates:
                matched = stock_candidates[0]
                sec_display = sub_part.split("-")[-1].strip() if sub_part else company_part
                sec_display = sec_display.replace("Etf", "ETF").replace("Bees", "BeES")
                evidences.append(
                    EnrichmentEvidence(
                        source_name=f"{self.name}:cdsl_demat",
                        priority=EvidencePriority.INVOICE_PDF,
                        confidence=0.97,
                        row_index=matched.row_index,
                        kind=TransactionKind.TRANSFER,
                        transfer_account="Investments",
                        category="Transfer, withdraw",
                        payee="IndMoney",
                        note=f"Indian Stocks in IndMoney: INDstocks wallet deposit ({qty_int}x {sec_display})",
                        needs_review=False,
                    )
                )
        return evidences

    def _extract_reit_distribution(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
    ) -> EnrichmentEvidence | None:
        body = " ".join(msg.text_body.split())
        m_qtr = re.search(r"Cashflow\s+Distribution\s+pertaining\s+to\s+(Q\d\s*FY\d{2})", body, re.IGNORECASE)
        m_units = re.search(r"Units\s+held\s+as\s+on\s+the\s+Record\s+Date\s+(\d+)", body, re.IGNORECASE)
        m_dt = re.search(r"Date\s+of\s+Credit\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", body, re.IGNORECASE)
        m_net = re.search(
            r"Net\s+Distribution\s*\(B-D\)\s*[?₹Rs.\s]*([\d,]+(?:\.\d{2})?)\s+[?₹Rs.\s]*([\d,]+(?:\.\d{2})?)\s+[?₹Rs.\s]*([\d,]+(?:\.\d{2})?)",
            body,
            re.IGNORECASE,
        )
        m_tds = re.search(r"TDS\s+Amount\s*[?₹Rs.\s]*([\d,]+(?:\.\d{2})?)", body, re.IGNORECASE)
        if not m_net:
            return None

        net_interest = m_net.group(1)
        net_dividend = m_net.group(2)
        total_minor = _parse_minor_amount(m_net.group(3))
        if total_minor is None:
            return None

        target_date_iso = msg.date_iso
        if m_dt:
            try:
                target_date_iso = datetime.strptime(m_dt.group(1).strip(), "%B %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                target_date_iso = msg.date_iso

        credit_rows = [r for r in rows if r.is_credit]
        matched = _match_best_row(credit_rows, target_date_iso, total_minor, used_rows, max_day_diff=2)
        if matched is None:
            return None

        used_rows.add(matched.row_index)
        reit_name = msg.subject.split("||")[0].strip().title().replace("Reit", "REIT")
        qtr_str = f" {m_qtr.group(1).strip()}" if m_qtr else ""
        units_str = f"{m_units.group(1)} units: " if m_units else ""
        tds_str = f" after ₹{m_tds.group(1)} TDS" if m_tds else ""
        note = (
            f"REIT Distribution in IndMoney: {reit_name}{qtr_str} "
            f"({units_str}₹{net_dividend} dividend + ₹{net_interest} net interest{tds_str})"
        )
        return EnrichmentEvidence(
            source_name=f"{self.name}:reit_distribution",
            priority=EvidencePriority.INVOICE_PDF,
            confidence=0.99,
            row_index=matched.row_index,
            kind=TransactionKind.INCOME,
            category="Interests, dividends",
            payee="IndMoney",
            note=note,
            needs_review=False,
        )

    def _extract_shriram(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
    ) -> EnrichmentEvidence | None:
        # 1. Yearly interest credit email: "Fixed Deposit Interest Credit against Cert No: SFL-61344474"
        m_int = re.search(
            r"Interest\s+amount\s+of\s+Rs\.?\s*([\d,]+(?:\.\d{1,2})?).*?certificate\s+bearing\s+no\.?\s*(SFL-\d+)",
            msg.text_body,
            re.IGNORECASE | re.DOTALL,
        )
        if m_int:
            amt_minor = _parse_minor_amount(m_int.group(1))
            cert_no = m_int.group(2).strip()
            if amt_minor is not None:
                credit_rows = [r for r in rows if r.is_credit and "SHRIRAM" in r.narration.upper()]
                matched = _match_best_row(credit_rows, msg.date_iso, amt_minor, used_rows, max_day_diff=2)
                if matched is not None:
                    used_rows.add(matched.row_index)
                    return EnrichmentEvidence(
                        source_name=f"{self.name}:shriram_fd",
                        priority=EvidencePriority.INVOICE_PDF,
                        confidence=0.99,
                        row_index=matched.row_index,
                        kind=TransactionKind.INCOME,
                        category="Interests, dividends",
                        payee="Shriram Finance",
                        note=f"Shriram Finance FD Yearly Interest payout in IndMoney (Cert No. {cert_no})",
                        needs_review=False,
                    )

        # 2. Maturity principal refund: "Maturity Refund – Certificate No. SFL-61344474"
        m_mat = re.search(r"Maturity\s+Refund.*?(SFL-\d+)", msg.subject, re.IGNORECASE)
        if m_mat:
            cert_no = m_mat.group(1).strip()
            credit_rows = [
                r
                for r in rows
                if r.is_credit
                and "SHRIRAM" in r.narration.upper()
                and r.row_index not in used_rows
                and abs((r.statement_date - date.fromisoformat(msg.date_iso)).days) <= 2
            ]
            if len(credit_rows) == 1:
                matched = credit_rows[0]
                used_rows.add(matched.row_index)
                return EnrichmentEvidence(
                    source_name=f"{self.name}:shriram_fd",
                    priority=EvidencePriority.INVOICE_PDF,
                    confidence=0.99,
                    row_index=matched.row_index,
                    kind=TransactionKind.TRANSFER,
                    transfer_account="Investments",
                    category="Transfer, withdraw",
                    payee="Shriram Finance",
                    note=f"Fixed Deposit in IndMoney: Shriram Finance FD Maturity Principal Auto-Refund (Cert No. {cert_no})",
                    needs_review=False,
                )
        return None

    def _extract_ipo_allotment(
        self,
        msg: EmailMessagePayload,
        rows: Sequence[StatementRow],
        used_rows: set[int],
    ) -> EnrichmentEvidence | None:
        body = msg.text_body
        m_app = re.search(r"Bid\s+cum\s+Application\s+Form\s+No\.\s*(\d+)", body, re.IGNORECASE)
        m_price = re.search(
            r"(?:Issue|Offer)\s+Price\s+per\s+(?:Security|Equity\s+Share|Unit)\s*\(Rs\.\)\s*([\d,]+)",
            body,
            re.IGNORECASE,
        )
        m_allotted = re.search(
            r"(?:Shares|Units|Number\s+of\s+(?:Equity\s+)?(?:Shares|Units))\s+Allotted\s*(\d+)",
            body,
            re.IGNORECASE,
        )
        m_adj = re.search(
            r"Amount\s+adjusted\s+towards\s+allotment\s*\(Rs\.\)\s*([\d,]+(?:\.\d{1,2})?)",
            body,
            re.IGNORECASE,
        )
        if not (m_app and m_price and m_allotted):
            return None

        allotted = int(m_allotted.group(1))
        if allotted <= 0:
            return None

        price_str = m_price.group(1).strip()
        price_minor = _parse_minor_amount(price_str)
        if m_adj:
            amt_minor = _parse_minor_amount(m_adj.group(1))
        elif price_minor is not None:
            amt_minor = allotted * price_minor
        else:
            amt_minor = None

        if amt_minor is None or amt_minor <= 0:
            return None

        app_no = m_app.group(1).strip()
        clean_subj = " ".join(msg.subject.split())
        is_reit = "REIT" in clean_subj.upper()
        company = re.sub(r"\s*-\s*(?:IPO|REIT).*$", "", clean_subj, flags=re.IGNORECASE).strip().title()
        if is_reit and "REIT" not in company.upper():
            company = f"{company} REIT"

        debit_rows = [r for r in rows if not r.is_credit]
        matched = _match_best_row(debit_rows, msg.date_iso, amt_minor, used_rows, max_day_diff=3)
        if matched is None:
            return None

        used_rows.add(matched.row_index)
        prefix = "REIT IPO in IndMoney" if is_reit else "IPO in IndMoney"
        unit_word = "units" if is_reit else "shares"
        return EnrichmentEvidence(
            source_name=f"{self.name}:ipo",
            priority=EvidencePriority.INVOICE_PDF,
            confidence=0.99,
            row_index=matched.row_index,
            kind=TransactionKind.TRANSFER,
            transfer_account="Investments",
            category="Transfer, withdraw",
            payee="IndMoney",
            note=f"{prefix}: {company} ({allotted} {unit_word} @ ₹{price_str}, Appl {app_no})",
            needs_review=False,
        )


_IST_TZ = timezone(timedelta(hours=5, minutes=30))
_CC_ALERT_PAT = re.compile(
    r"using\s+your\s+HDFC\s+Bank\s+Credit\s+Card\s+ending\s+\d{4}\s+for\s+"
    r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)\s+at\s+(.+?)\s+on\s+"
    r"(\d{2}-\d{2}-\d{4})\s+(\d{2}:\d{2}:\d{2})",
    re.IGNORECASE,
)


class HdfcCreditCardExtractor:
    """Extracts exact timestamps from HDFC CC InstaAlerts and bill-payment transfers from encrypted CC PDFs."""

    name = "gmail_hdfc_cc"
    imap_search_query = "(from:alerts@hdfcbank.net OR from:alerts@hdfcbank.bank.in OR from:Emailstatements.cards@hdfcbank.net)"

    def __init__(self, passwords: Sequence[str] = DEFAULT_CC_PDF_PASSWORDS) -> None:
        self._passwords = tuple(passwords)

    def extract(
        self,
        messages: Sequence[EmailMessagePayload],
        rows: Sequence[StatementRow],
    ) -> Sequence[EnrichmentEvidence]:
        if not rows:
            return ()

        debit_rows = [r for r in rows if not r.is_credit]
        used_alert_rows: set[int] = set()
        used_bill_rows: set[int] = set()
        evidences: list[EnrichmentEvidence] = []

        for msg in messages:
            # 1. Check HDFC Credit Card InstaAlert emails for exact second-level IST timestamps
            m_alert = _CC_ALERT_PAT.search(msg.text_body)
            if m_alert:
                amt_minor = _parse_minor_amount(m_alert.group(1))
                merchant_raw = " ".join(m_alert.group(2).split()).strip()
                date_ddmmyyyy = m_alert.group(3)
                time_hhmmss = m_alert.group(4)
                if amt_minor is not None:
                    try:
                        dt_ist = datetime.strptime(
                            f"{date_ddmmyyyy} {time_hhmmss}", "%d-%m-%Y %H:%M:%S"
                        ).replace(tzinfo=_IST_TZ)
                        dt_utc_iso = dt_ist.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                        date_iso = dt_ist.strftime("%Y-%m-%d")
                    except ValueError:
                        dt_utc_iso = None
                        date_iso = msg.date_iso

                    matched = _match_best_row(
                        debit_rows, date_iso, amt_minor, used_alert_rows, max_day_diff=1
                    )
                    if matched is not None and dt_utc_iso is not None:
                        used_alert_rows.add(matched.row_index)
                        payee_clean = self._normalize_cc_merchant(merchant_raw)
                        evidences.append(
                            EnrichmentEvidence(
                                source_name=f"{self.name}:insta_alert",
                                priority=EvidencePriority.INVOICE_PDF,
                                confidence=0.98,
                                row_index=matched.row_index,
                                timestamp_iso=dt_utc_iso,
                                payee=payee_clean,
                                payment_method=PaymentMethod.CREDIT_CARD,
                                needs_review=False,
                            )
                        )

            # 2. Check encrypted HDFC Credit Card monthly PDF statements for bill-payment transfers
            for fname, payload in msg.attachments:
                if not fname.lower().endswith(".pdf"):
                    continue
                try:
                    stmt = parse_hdfc_cc_pdf_bytes(payload, filename=fname, passwords=self._passwords)
                except ValueError:
                    continue
                if stmt is None:
                    continue
                for txn_date_iso, narration, _ref_no, _debit_minor, credit_minor in stmt.transactions:
                    if credit_minor <= 0:
                        continue
                    narr_u = narration.upper()
                    if "TELE TRANSFER CREDIT" not in narr_u and "IMPS PMT" not in narr_u:
                        continue
                    # Match corresponding debit on the HDFC savings bank statement (e.g. CRED bill payment)
                    bank_candidates = [
                        r
                        for r in debit_rows
                        if "TELE TRANSFER CREDIT" not in r.narration.upper()
                        and "IMPS PMT" not in r.narration.upper()
                    ]
                    matched_bank = _match_best_row(
                        bank_candidates, txn_date_iso, credit_minor, used_bill_rows, max_day_diff=1
                    )
                    if matched_bank is not None:
                        used_bill_rows.add(matched_bank.row_index)
                        evidences.append(
                            EnrichmentEvidence(
                                source_name=f"{self.name}:statement_pdf",
                                priority=EvidencePriority.INVOICE_PDF,
                                confidence=0.99,
                                row_index=matched_bank.row_index,
                                kind=TransactionKind.TRANSFER,
                                transfer_account="HDFC Credit Card",
                                category="Transfer, withdraw",
                                payee="CRED",
                                note=f"Credit card bill payment (Statement {stmt.statement_date_iso})",
                                needs_review=False,
                            )
                        )

        return evidences

    @staticmethod
    def _normalize_cc_merchant(merchant_raw: str) -> str:
        u = merchant_raw.upper()
        if "REL RETAIL LTD DIGITAL" in u:
            return "Reliance Digital"
        if "KAKE DI HATTI" in u:
            return "Kake Di Hatti"
        if "REGENTA PLACE" in u:
            return "Regenta Place"
        if "BLOSSOM BOOK HOUSE" in u:
            return "Blossom Book House"
        if "MANIPAL HEALTH" in u:
            return "Manipal Hospital"
        if "STORIES BAR" in u:
            return "Stories Bar and Kitchen"
        if "NKP EMPIRE" in u:
            return "Empire Restaurant"
        return merchant_raw.title()


DEFAULT_RECEIPT_EXTRACTORS: tuple[ReceiptExtractor, ...] = (
    SwiggyReceiptExtractor(),
    NammaYatriPdfExtractor(),
    RapidoReceiptExtractor(),
    AmazonEmailExtractor(),
    TravelBookingExtractor(),
    InvestmentEmailExtractor(),
    HdfcCreditCardExtractor(),
)


class GmailImapCollector:
    """Connects to Gmail over IMAP SSL and runs registered ReceiptExtractor strategies."""

    name = "gmail_imap"

    def __init__(
        self,
        email_address: str,
        app_password: str,
        extractors: Sequence[ReceiptExtractor] | None = None,
    ) -> None:
        self._email = email_address
        self._password = app_password.replace(" ", "")
        self._extractors: tuple[ReceiptExtractor, ...] = (
            tuple(extractors) if extractors is not None else DEFAULT_RECEIPT_EXTRACTORS
        )

    def collect(
        self,
        rows: Sequence[StatementRow],
        start_date: date,
        end_date: date,
    ) -> Sequence[EnrichmentEvidence]:
        del end_date
        if not self._email or not self._password or not self._extractors:
            return ()

        after_str = start_date.strftime("%Y/%m/%d")
        evidences: list[EnrichmentEvidence] = []
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=20)
            mail.login(self._email, self._password)
            mail.select('"[Gmail]/All Mail"', readonly=True)

            for extractor in self._extractors:
                raw_q = f'"{extractor.imap_search_query} after:{after_str}"'
                typ, data = mail.uid("SEARCH", "X-GM-RAW", raw_q)
                if typ != "OK" or not data or not data[0]:
                    continue
                uids = data[0].split()[-120:]
                msgs: list[EmailMessagePayload] = []
                for uid_bytes in uids:
                    _, msg_data = mail.uid("FETCH", uid_bytes, "(RFC822)")
                    if msg_data and msg_data[0] and isinstance(msg_data[0][1], bytes):
                        parsed_msg = email.message_from_bytes(msg_data[0][1])
                        msgs.append(_extract_email_payload(uid_bytes.decode(), parsed_msg))
                evidences.extend(extractor.extract(msgs, rows))

            mail.logout()
        except OSError, imaplib.IMAP4.error:
            return ()

        return evidences
