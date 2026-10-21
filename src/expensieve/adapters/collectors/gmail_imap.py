"""Gmail IMAP receipt and PDF invoice collector with pluggable ReceiptExtractor strategies."""

import email
import html
import imaplib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Protocol

from expensieve.adapters.collectors.pdf_text import extract_pdf_lines
from expensieve.domain.gazetteer import Gazetteer
from expensieve.domain.models import (
    EnrichmentEvidence,
    EvidencePriority,
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
        diff = abs((row.statement_date - t_date).days)
        if diff <= max_day_diff and diff < best_diff:
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


class InvestmentEmailExtractor:
    """Extracts IPO allotment details (KFintech, Link Intime/MUFG, Bigshare) and Shriram Finance FD payouts/refunds."""

    name = "gmail_investments"
    imap_search_query = "(from:shriramfinance.in OR from:kfintech.com OR from:mpms.mufg.com)"

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
            if "shriramfinance" in sender_l:
                ev = self._extract_shriram(msg, rows, used_rows)
                if ev is not None:
                    evidences.append(ev)
            elif "kfintech" in sender_l or "mufg.com" in sender_l:
                ev = self._extract_ipo_allotment(msg, rows, used_rows)
                if ev is not None:
                    evidences.append(ev)

        return evidences

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
            r"(?:Issue|Offer)\s+Price\s+per\s+(?:Security|Equity\s+Share)\s*\(Rs\.\)\s*([\d,]+)",
            body,
            re.IGNORECASE,
        )
        m_allotted = re.search(
            r"(?:Shares|Number\s+of\s+Equity\s+Shares)\s+Allotted\s*(\d+)",
            body,
            re.IGNORECASE,
        )
        m_adj = re.search(
            r"Amount\s+adjusted\s+towards\s+allotment\s*\(Rs\.\)\s*([\d,]+(?:\.\d{1,2})?)",
            body,
            re.IGNORECASE,
        )
        if not (m_app and m_price and m_allotted and m_adj):
            return None

        allotted = int(m_allotted.group(1))
        if allotted <= 0:
            return None

        amt_minor = _parse_minor_amount(m_adj.group(1))
        if amt_minor is None or amt_minor <= 0:
            return None

        app_no = m_app.group(1).strip()
        price_str = m_price.group(1).strip()
        company = re.sub(r"\s*-\s*IPO.*$", "", msg.subject, flags=re.IGNORECASE).strip().title()

        debit_rows = [r for r in rows if not r.is_credit]
        matched = _match_best_row(debit_rows, msg.date_iso, amt_minor, used_rows, max_day_diff=3)
        if matched is None:
            return None

        used_rows.add(matched.row_index)
        return EnrichmentEvidence(
            source_name=f"{self.name}:ipo",
            priority=EvidencePriority.INVOICE_PDF,
            confidence=0.99,
            row_index=matched.row_index,
            kind=TransactionKind.TRANSFER,
            transfer_account="Investments",
            category="Transfer, withdraw",
            payee="IndMoney",
            note=f"IPO in IndMoney: {company} ({allotted} shares @ ₹{price_str}, Appl {app_no})",
            needs_review=False,
        )


DEFAULT_RECEIPT_EXTRACTORS: tuple[ReceiptExtractor, ...] = (
    SwiggyReceiptExtractor(),
    NammaYatriPdfExtractor(),
    RapidoReceiptExtractor(),
    AmazonEmailExtractor(),
    TravelBookingExtractor(),
    InvestmentEmailExtractor(),
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
                uids = data[0].split()[-50:]
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
