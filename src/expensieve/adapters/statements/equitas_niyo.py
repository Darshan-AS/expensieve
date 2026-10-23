"""Equitas Small Finance Bank (NiyoX Savings) PDF statement and email alert adapter implementing StatementSource."""

import hashlib
import json
import re
import struct
import subprocess
import zlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from expensieve.adapters.collectors.pdf_text import (
    PdfEncryptInfo,
    _parse_tounicode_cmap,
    _unescape_pdf_literal_bytes,
    derive_pdf_encryption_key,
    parse_pdf_encrypt_dict,
)
from expensieve.adapters.statements.hdfc_delimited import (
    parse_ddmmyy_to_iso,
    parse_inr_to_paise,
)
from expensieve.domain.models import StatementRow

DEFAULT_EQUITAS_PDF_PASSWORDS: tuple[str, ...] = (
    "1704DAR",
    "109995157",
    "DTQPD8308L",
)

_IST_TZ = timezone(timedelta(hours=5, minutes=30))
_DATE_SLASH_PAT = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_REF_IMP_PAT = re.compile(r"\bIMP\s+(\d{12})\b", re.IGNORECASE)
_REF_UPI_PAT = re.compile(r"\bUPI\s+REF\s+NO\s+(\d{12})", re.IGNORECASE)
_REF_AMC_PAT = re.compile(r":(\d{6}/\d+)\b")


def _aes128_cbc_decrypt(key: bytes, data: bytes) -> bytes:
    """Decrypt an AES-128-CBC PDF stream (16-byte IV prefix + ciphertext) using system openssl."""
    if len(data) < 32 or len(data) % 16 != 0:
        return b""
    iv = data[:16]
    ct = data[16:]
    res = subprocess.run(
        ["/usr/bin/openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()],
        input=ct,
        capture_output=True,
        check=False,
    )
    return res.stdout


def extract_equitas_pdf_rows(
    pdf_path: Path,
    passwords: Sequence[str] = DEFAULT_EQUITAS_PDF_PASSWORDS,
) -> list[tuple[int, float, tuple[str, ...]]]:
    """Decrypt an Equitas Small Finance Bank PDF statement (AES-128-CBC + /ObjStm) and group text by (page, y)."""
    data = pdf_path.read_bytes()
    enc = parse_pdf_encrypt_dict(data)
    if enc is None:
        return []

    fixed_enc = PdfEncryptInfo(
        encrypt_obj_id=enc.encrypt_obj_id,
        owner_hash=enc.owner_hash,
        user_hash=enc.user_hash,
        permissions=enc.permissions,
        version=enc.version,
        revision=enc.revision,
        key_length_bits=128,
        file_id_0=enc.file_id_0,
    )
    file_key: bytes | None = None
    for pw in passwords:
        file_key = derive_pdf_encryption_key(pw, fixed_enc)
        if file_key is not None:
            break
    if file_key is None:
        return []

    obj_streams: dict[int, str] = {}
    obj_dicts: dict[int, str] = {}
    for match in re.finditer(rb"(?:^|[\r\n])(\d+)\s+(\d+)\s+obj\b", data):
        oid = int(match.group(1))
        gen = int(match.group(2))
        if oid == fixed_enc.encrypt_obj_id:
            continue
        start = match.end()
        end = data.find(b"endobj", start)
        if end == -1:
            continue
        body = data[start:end]
        s_idx = body.find(b"stream")
        if s_idx == -1:
            obj_dicts[oid] = body.decode("latin1", errors="replace")
            continue
        dict_part = body[:s_idx].decode("latin1", errors="replace")
        obj_dicts[oid] = dict_part
        e_idx = body.rfind(b"endstream")
        if e_idx == -1:
            continue
        s_start = s_idx + 6
        if body[s_start : s_start + 2] == b"\r\n":
            s_start += 2
        elif body[s_start : s_start + 1] in (b"\n", b"\r"):
            s_start += 1
        raw_stream = body[s_start:e_idx]
        while raw_stream and raw_stream[-1:] in (b"\r", b"\n"):
            raw_stream = raw_stream[:-1]
        obj_key = hashlib.md5(
            file_key + struct.pack("<I", oid)[:3] + struct.pack("<I", gen)[:2] + b"sAlT"
        ).digest()[:16]
        dec_bytes = _aes128_cbc_decrypt(obj_key, raw_stream)
        if dec_bytes:
            try:
                decomp = zlib.decompress(dec_bytes).decode("latin1", errors="replace")
                obj_streams[oid] = decomp
                if "/ObjStm" in dict_part:
                    m_n = re.search(r"/N\s+(\d+)", dict_part)
                    m_first = re.search(r"/First\s+(\d+)", dict_part)
                    if m_n and m_first:
                        n_objs = int(m_n.group(1))
                        first_off = int(m_first.group(1))
                        hdr_nums = [int(x) for x in decomp[:first_off].split()]
                        pairs = [
                            (hdr_nums[2 * i], hdr_nums[2 * i + 1] + first_off)
                            for i in range(min(n_objs, len(hdr_nums) // 2))
                        ]
                        for idx_p, (sub_oid, off) in enumerate(pairs):
                            next_off = (
                                pairs[idx_p + 1][1] if idx_p + 1 < len(pairs) else len(decomp)
                            )
                            obj_dicts[sub_oid] = decomp[off:next_off]
            except zlib.error:
                pass

    font_obj_cmaps: dict[int, dict[int, str]] = {}
    for oid, obj_dict in obj_dicts.items():
        tu = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj_dict)
        if tu:
            tu_id = int(tu.group(1))
            if tu_id in obj_streams:
                font_obj_cmaps[oid] = _parse_tounicode_cmap(obj_streams[tu_id])

    font_name_cmaps: dict[str, dict[int, str]] = {}
    for page_oid, page_dict in obj_dicts.items():
        for fm in re.finditer(r"/(F\d+)\s+(\d+)\s+0\s+R", page_dict):
            fname, foid = fm.group(1), int(fm.group(2))
            if foid in font_obj_cmaps:
                font_name_cmaps[f"{page_oid}:{fname}"] = font_obj_cmaps[foid]
                font_name_cmaps[fname] = font_obj_cmaps[foid]

    fallback_cmap: dict[int, str] = {}
    for cm in font_obj_cmaps.values():
        fallback_cmap.update(cm)

    blocks: list[tuple[int, float, float, str]] = []
    page_idx = 0
    for page_oid, page_dict in sorted(obj_dicts.items()):
        if not re.search(r"/Type\s*/Page\b", page_dict):
            continue
        m_cnt = re.search(r"/Contents\s+(\d+)\s+0\s+R", page_dict)
        if not m_cnt or int(m_cnt.group(1)) not in obj_streams:
            continue
        dec = obj_streams[int(m_cnt.group(1))]
        page_idx += 1
        cur_cmap: dict[int, str] | None = None
        for bt_block in re.findall(r"\bBT\b(.*?)\bET\b", dec, re.DOTALL):
            x_coord, y_coord = 0.0, 0.0
            tm = re.search(
                r"1\s+0\s+0\s+-?1\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+Tm",
                bt_block,
            )
            if tm:
                x_coord, y_coord = float(tm.group(1)), abs(float(tm.group(2)))
            chars: list[str] = []
            token_pattern = (
                r"/(F\d+)\s+[\d.]+\s+Tf|"
                r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+Td|"
                r"<([0-9A-Fa-f]+)>\s*Tj|"
                r"\(([^()\\]*(?:\\.[^()\\]*)*)\)\s*Tj"
            )
            for tok in re.finditer(token_pattern, bt_block):
                if tok.group(1):
                    cur_cmap = font_name_cmaps.get(f"{page_oid}:{tok.group(1)}") or font_name_cmaps.get(
                        tok.group(1)
                    )
                elif tok.group(4):
                    hex_str = tok.group(4)
                    cm = cur_cmap or fallback_cmap
                    for i in range(0, len(hex_str), 4):
                        cid = int(hex_str[i : i + 4], 16)
                        chars.append(cm.get(cid, ""))
                elif tok.group(5) is not None:
                    raw_lit = _unescape_pdf_literal_bytes(tok.group(5).encode("latin1"))
                    if cur_cmap and len(raw_lit) % 2 == 0:
                        for i in range(0, len(raw_lit), 2):
                            cid = (raw_lit[i] << 8) | raw_lit[i + 1]
                            chars.append(cur_cmap.get(cid, ""))
                    else:
                        chars.append(raw_lit.decode("latin1", errors="replace"))
            txt = "".join(chars).replace("\u200b", "").strip()
            if txt:
                blocks.append((page_idx, -round(y_coord, 1), round(x_coord, 1), txt))

    grouped: dict[tuple[int, float], list[tuple[float, str]]] = {}
    for p, y, x, t in blocks:
        grouped.setdefault((p, y), []).append((x, t))
    rows: list[tuple[int, float, tuple[str, ...]]] = []
    for (p, y), cols in sorted(grouped.items()):
        cols.sort(key=lambda c: c[0])
        rows.append((p, y, tuple(c[1] for c in cols)))
    return rows


def _extract_reference_no(narration: str) -> str:
    """Extract a clean reference number from an Equitas narration."""
    m_imp = _REF_IMP_PAT.search(narration)
    if m_imp:
        return m_imp.group(1)
    m_upi = _REF_UPI_PAT.search(narration)
    if m_upi:
        return m_upi.group(1)
    m_amc = _REF_AMC_PAT.search(narration)
    if m_amc:
        return m_amc.group(1)
    return ""


def _parse_alert_timestamps_and_closure(
    emails_json_path: Path | None,
) -> tuple[dict[tuple[str, int], str], int | None, str | None]:
    """Extract `(date_iso, amount_minor) -> timestamp_iso` and final closed balance from `niyo_emails.json`."""
    ts_map: dict[tuple[str, int], str] = {}
    closure_bal_minor: int | None = None
    closure_date_iso: str | None = None
    closure_ts_iso: str | None = None

    if emails_json_path is None or not emails_json_path.exists():
        return ts_map, None, None

    try:
        items = json.loads(emails_json_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return ts_map, None, None

    if not isinstance(items, list):
        return ts_map, None, None

    for item in items:
        if not isinstance(item, dict):
            continue
        subj = str(item.get("subject", ""))
        body = " ".join(str(item.get("body", "")).split())
        date_hdr = str(item.get("date", ""))
        dt_utc_str: str | None = None
        if date_hdr:
            try:
                dt = parsedate_to_datetime(date_hdr)
                dt_utc_str = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                dt_ist_date = dt.astimezone(_IST_TZ).strftime("%Y-%m-%d")
            except TypeError, ValueError:
                dt_utc_str = None
                dt_ist_date = ""
        else:
            dt_ist_date = ""

        if "Account Credit Alert" in subj or "Account Debit Alert" in subj:
            m_amt = re.search(r"INR\s*([\d,]+(?:\.\d{1,2})?)", body, re.IGNORECASE)
            m_val = re.search(r"value\s+(\d{2}-[A-Za-z]{3}-\d{4})", body, re.IGNORECASE)
            if m_amt and dt_utc_str:
                amt_minor = parse_inr_to_paise(m_amt.group(1))
                d_iso = dt_ist_date
                if m_val:
                    try:
                        d_iso = datetime.strptime(m_val.group(1).title(), "%d-%b-%Y").strftime(
                            "%Y-%m-%d"
                        )
                    except ValueError:
                        pass
                if d_iso and amt_minor > 0:
                    ts_map[(d_iso, amt_minor)] = dt_utc_str

        if "Closeout-Debit Alert" in subj or ("CLOSED today" in body and "7140" in body):
            m_close = re.search(
                r"Available\s+funds\s+of\s+INR\s*([\d,]+(?:\.\d{1,2})?)",
                body,
                re.IGNORECASE,
            )
            if m_close:
                closure_bal_minor = parse_inr_to_paise(m_close.group(1))
                closure_date_iso = dt_ist_date or "2026-06-18"
                closure_ts_iso = dt_utc_str

    if closure_date_iso and closure_ts_iso and closure_bal_minor is not None:
        # Map the closure timestamp for any pro-rata interest row on that date
        ts_map[(closure_date_iso, -1)] = closure_ts_iso

    return ts_map, closure_bal_minor, closure_date_iso


class EquitasNiyoStatementSource:
    """Adapter that parses Equitas Small Finance Bank (`NiyoX`) PDF statement chains and Insta-Alerts."""

    def __init__(
        self,
        statement_path: Path,
        account_name: str = "NiyoX",
        passwords: Sequence[str] = DEFAULT_EQUITAS_PDF_PASSWORDS,
        emails_json_path: Path | None = None,
        include_closure_debit: bool = False,
    ) -> None:
        self._path = statement_path
        self.account_name = account_name
        self._passwords = tuple(passwords)
        self._emails_json_path = emails_json_path
        self._include_closure_debit = include_closure_debit
        self._opening_balance_minor: int = 0
        self._closing_balance_minor: int = 0
        self._parsed_rows: list[StatementRow] | None = None

    def opening_balance_minor(self) -> int:
        if self._parsed_rows is None:
            self.parse()
        return self._opening_balance_minor

    def closing_balance_minor(self) -> int:
        if self._parsed_rows is None:
            self.parse()
        return self._closing_balance_minor

    def _discover_pdf_paths(self) -> list[Path]:
        if self._path.is_dir():
            candidates = list(self._path.glob("*6096.pdf"))
            if not candidates:
                candidates = list(self._path.glob("*.pdf"))
        else:
            parent_candidates = list(self._path.parent.glob("*6096.pdf"))
            candidates = parent_candidates if len(parent_candidates) > 1 else [self._path]

        def _sort_key(p: Path) -> tuple[int, str]:
            prefix = p.name.split("_", 1)[0]
            return (int(prefix) if prefix.isdigit() else 999999999, p.name)

        return sorted(candidates, key=_sort_key)

    def _discover_emails_json(self) -> Path | None:
        if self._emails_json_path is not None and self._emails_json_path.exists():
            return self._emails_json_path
        base_dir = self._path if self._path.is_dir() else self._path.parent
        for candidate in (
            base_dir / "niyo_emails.json",
            base_dir.parent / "niyo_emails.json",
        ):
            if candidate.exists():
                return candidate
        return None

    def parse(self) -> Sequence[StatementRow]:
        """Parse all Equitas PDF statements in chronological order, deduplicate, and verify balance math."""
        if self._parsed_rows is not None:
            return self._parsed_rows

        pdf_paths = self._discover_pdf_paths()
        if not pdf_paths:
            raise ValueError(f"No Equitas PDF statements found at {self._path}")

        emails_path = self._discover_emails_json()
        ts_map, closure_bal_minor, closure_date_iso = _parse_alert_timestamps_and_closure(
            emails_path
        )

        raw_entries: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, int, int]] = set()
        first_opening: int | None = None

        for pdf_path in pdf_paths:
            grouped_rows = extract_equitas_pdf_rows(pdf_path, passwords=self._passwords)
            for _page, _y, cols in grouped_rows:
                if len(cols) == 2 and "OPENING BALANCE" in cols[0].upper():
                    if first_opening is None:
                        first_opening = parse_inr_to_paise(cols[1])
                    continue
                if len(cols) >= 5 and _DATE_SLASH_PAT.match(cols[0].strip()):
                    txn_date_raw = cols[0].strip()
                    val_date_raw = cols[1].strip() if _DATE_SLASH_PAT.match(cols[1].strip()) else txn_date_raw
                    narr = re.sub(r"\s+", " ", " ".join(cols[2:-2])).strip()
                    amt_minor = parse_inr_to_paise(cols[-2])
                    bal_minor = parse_inr_to_paise(cols[-1])

                    txn_date_iso = parse_ddmmyy_to_iso(txn_date_raw)
                    val_date_iso = parse_ddmmyy_to_iso(val_date_raw)
                    # Use Value Date for quarterly interest capitalisation so dates match quarter-ends
                    effective_date_iso = (
                        val_date_iso
                        if "CREDIT INTEREST" in narr.upper()
                        else txn_date_iso
                    )
                    dedup_key = (effective_date_iso, narr.upper(), amt_minor, bal_minor)
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)
                    raw_entries.append(
                        {
                            "date_iso": effective_date_iso,
                            "value_date_iso": val_date_iso,
                            "narration": narr,
                            "amount_minor": amt_minor,
                            "closing_balance_minor": bal_minor,
                        }
                    )

        if not raw_entries:
            raise ValueError(f"No valid Equitas transaction rows extracted from {self._path}")

        self._opening_balance_minor = first_opening if first_opening is not None else 0
        running_bal = self._opening_balance_minor

        built_rows: list[StatementRow] = []
        for idx, entry in enumerate(raw_entries):
            bal_minor = int(entry["closing_balance_minor"])
            amt_minor = int(entry["amount_minor"])
            delta = bal_minor - running_bal
            if abs(delta) != amt_minor:
                raise ValueError(
                    f"Equitas balance discontinuity at row {idx} ({entry['date_iso']}): "
                    f"prev={running_bal}, amt={amt_minor}, new={bal_minor}"
                )
            is_credit = delta > 0
            running_bal = bal_minor
            d_iso = str(entry["date_iso"])
            narr = str(entry["narration"])
            ref_no = _extract_reference_no(narr)
            alert_ts = ts_map.get((d_iso, amt_minor))
            if alert_ts:
                narr = f"{narr} [TS: {alert_ts}]"

            built_rows.append(
                StatementRow(
                    row_index=len(built_rows),
                    date_iso=d_iso,
                    value_date_iso=str(entry["value_date_iso"]),
                    narration=narr,
                    reference_no=ref_no,
                    debit_minor=0 if is_credit else amt_minor,
                    credit_minor=amt_minor if is_credit else 0,
                    closing_balance_minor=bal_minor,
                )
            )

        # Backfill pro-rata closure interest on 2026-06-18 if closure alert shows higher final balance
        if (
            closure_bal_minor is not None
            and closure_date_iso is not None
            and closure_bal_minor > running_bal
        ):
            diff_minor = closure_bal_minor - running_bal
            closure_ts = ts_map.get((closure_date_iso, -1))
            cl_narr = "CREDIT INTEREST CAPITALISED (ACCOUNT CLOSURE PRO-RATA INTEREST)"
            if closure_ts:
                cl_narr = f"{cl_narr} [TS: {closure_ts}]"
            running_bal = closure_bal_minor
            built_rows.append(
                StatementRow(
                    row_index=len(built_rows),
                    date_iso=closure_date_iso,
                    value_date_iso=closure_date_iso,
                    narration=cl_narr,
                    reference_no="",
                    debit_minor=0,
                    credit_minor=diff_minor,
                    closing_balance_minor=running_bal,
                )
            )

            if self._include_closure_debit:
                dd_narr = "ACCOUNT CLOSURE PROCEEDS DISPATCHED VIA DEMAND DRAFT (AWB JJ843930190IN)"
                if closure_ts:
                    dd_narr = f"{dd_narr} [TS: {closure_ts}]"
                running_bal = 0
                built_rows.append(
                    StatementRow(
                        row_index=len(built_rows),
                        date_iso=closure_date_iso,
                        value_date_iso=closure_date_iso,
                        narration=dd_narr,
                        reference_no="JJ843930190IN",
                        debit_minor=closure_bal_minor,
                        credit_minor=0,
                        closing_balance_minor=0,
                    )
                )

        self._closing_balance_minor = running_bal
        self._parsed_rows = built_rows
        return built_rows
