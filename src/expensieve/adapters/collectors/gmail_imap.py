"""Gmail IMAP receipt and PDF invoice collector with pluggable ReceiptExtractor strategies."""

import email
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

from expensieve.domain.models import (
    EnrichmentEvidence,
    EvidencePriority,
    StatementRow,
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
    ) -> Sequence[EnrichmentEvidence]:
        ...


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
    except (TypeError, ValueError):
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
            stripped = " ".join(re.sub(r"<[^>]+>", " ", raw_html).split())
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

        evidences: list[EnrichmentEvidence] = []
        for msg in messages:
            # Parse total paid from email text
            m_tot = re.search(r"Order Total[:\s]*(?:Rs\.?|₹)\s*([\d,]+(?:\.\d{1,2})?)", msg.text_body, re.IGNORECASE)
            if not m_tot:
                continue
            try:
                amt_minor = int((Decimal(m_tot.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
            except InvalidOperation:
                continue

            m_rest = re.search(r"Restaurant[:\s]+([A-Za-z0-9 &'\-]+?)(?:\s+Order|\s+Delivered|\n)", msg.text_body)
            rest_name = m_rest.group(1).strip() if m_rest else "Swiggy"

            candidates = [r for r in swiggy_rows if r.amount_minor == amt_minor and r.date_iso == msg.date_iso]
            if len(candidates) == 1:
                evidences.append(
                    EnrichmentEvidence(
                        source_name=self.name,
                        priority=EvidencePriority.INVOICE_PDF,
                        confidence=0.98,
                        row_index=candidates[0].row_index,
                        category="Meals & Fast Food",
                        payee="Swiggy",
                        note=f"Swiggy order ({rest_name})",
                        needs_review=False,
                    )
                )
        return evidences


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
            tuple(extractors) if extractors is not None else (SwiggyReceiptExtractor(),)
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
        except (OSError, imaplib.IMAP4.error):
            return ()

        return evidences
