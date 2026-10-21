"""Chrome session order collector (Zomato, Zepto, Blinkit, Amazon, Uber) via macOS AppleScript / JSON cache."""

import json
import re
import subprocess
import time
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from expensieve.domain.gazetteer import Gazetteer
from expensieve.domain.models import (
    EnrichmentEvidence,
    EvidencePriority,
    StatementRow,
)


def _parse_minor_amount(raw: str | float) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return round(raw * 100)
    cleaned = str(raw).replace("₹", "").replace(",", "").replace("INR", "").strip()
    if not cleaned:
        return None
    try:
        return int((Decimal(cleaned) * 100).quantize(Decimal(1)))
    except InvalidOperation:
        return None


def _match_best_row(
    candidates: Sequence[StatementRow],
    target_date_iso: str,
    amount_minor: int,
    used_indices: set[int],
    max_day_diff: int = 1,
) -> StatementRow | None:
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


def format_zomato_dishes(dish_str: str) -> str:
    """Normalize Zomato '1 x Paper Masala Dosa, 2 x Vada' into newline-separated '1x Paper Masala Dosa'."""
    parts = [p.strip() for p in dish_str.split(",") if p.strip()]
    cleaned: list[str] = []
    for part in parts:
        norm = re.sub(r"^(\d+)\s*x\s+", r"\1x ", part, flags=re.IGNORECASE)
        cleaned.append(norm)
    return "\n".join(cleaned)


def classify_zomato_category(dish_str: str, res_name: str) -> str:
    """Distinguish 'Desserts & Sweets' from 'Meals & Fast Food' for Zomato orders."""
    upper = f"{dish_str} {res_name}".upper()
    dessert_keywords = (
        "ICE CREAM",
        "ICECREAM",
        "CAKE",
        "DESSERT",
        "WAFFLE",
        "SWEET",
        "BROWNIE",
        "KULFI",
        "PASTRY",
        "CORNETTO",
        "MAGNUM",
        "BASKIN",
        "POLAR BEAR",
        "NATURAL ICE CREAM",
    )
    savory_keywords = (
        "BIRYANI",
        "DOSA",
        "BURGER",
        "PIZZA",
        "RICE",
        "NOODLES",
        "THALI",
        "ROTI",
        "CURRY",
        "RAMEN",
    )
    if any(k in upper for k in dessert_keywords) and not any(m in upper for m in savory_keywords):
        return "Desserts & Sweets"
    return "Meals & Fast Food"


def parse_zepto_order_detail_text(raw_text: str) -> tuple[str, str]:
    """Parse Zepto SPA order page text into (note, category)."""
    m_sec = re.search(
        r"\d+\s+items?\s+in\s+order\s+(.*?)(?:Rate\s+Order|Order\s+Again|Bill\s+Summary|$)",
        raw_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not m_sec:
        return ("", "Groceries")

    lines = [ln.strip() for ln in m_sec.group(1).splitlines() if ln.strip()]
    items: list[str] = []
    idx = 0
    while idx < len(lines):
        title = lines[idx]
        if title.startswith("₹") or re.match(r"^\d+\s+units?$", title, re.IGNORECASE):
            idx += 1
            continue
        qty = "1"
        for lookahead in range(1, 4):
            if idx + lookahead < len(lines):
                m_unit = re.match(r"^(\d+)\s+units?$", lines[idx + lookahead], re.IGNORECASE)
                if m_unit:
                    qty = m_unit.group(1)
                    idx += lookahead + 1
                    break
        else:
            idx += 1
        if not title.startswith("₹"):
            items.append(f"{qty}x {title}")

    note = "\n".join(items)
    upper = note.upper()
    if any(k in upper for k in ("TABLET", "SPRAY", "CAPSULE", "NIFTAS", "OTRIVIN", "DOLO")):
        return (note, "Drug-store, chemist")
    return (note, "Groceries")


def execute_chrome_tab_js(url_substring: str, js_code: str, timeout_sec: float = 10.0) -> str | None:
    """Execute JavaScript on the first open Google Chrome tab matching url_substring via macOS osascript."""
    escaped = js_code.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    apple_script = f"""
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                if (URL of t) contains "{url_substring}" then
                    return execute t javascript "{escaped}"
                end if
            end repeat
        end repeat
        return "TAB_NOT_FOUND"
    end tell
    """
    try:
        res = subprocess.run(
            ["/usr/bin/osascript", "-e", apple_script],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_sec,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = res.stdout.strip()
    if not out or out == "TAB_NOT_FOUND":
        return None
    return out


class CachedMerchantOrderCollector:
    """Matches Zomato, Zepto, Blinkit, Amazon, and Uber orders from cached JSON or live Chrome tabs."""

    name = "merchant_web_orders"

    def __init__(
        self,
        zomato_json_path: Path | None = None,
        zepto_json_path: Path | None = None,
        blinkit_json_path: Path | None = None,
        amazon_json_path: Path | None = None,
        uber_json_path: Path | None = None,
        enable_live_chrome: bool = False,
        gazetteer: Gazetteer | None = None,
    ) -> None:
        self._zomato_path = zomato_json_path
        self._zepto_path = zepto_json_path
        self._blinkit_path = blinkit_json_path
        self._amazon_path = amazon_json_path
        self._uber_path = uber_json_path
        self._enable_live_chrome = enable_live_chrome
        self._gazetteer = gazetteer or Gazetteer()

    def collect(
        self,
        rows: Sequence[StatementRow],
        start_date: date,
        end_date: date,
    ) -> Sequence[EnrichmentEvidence]:
        del end_date
        evidences: list[EnrichmentEvidence] = []

        # 1. Zomato (cached JSON or live Chrome fetch)
        zomato_orders: list[dict[str, Any]] = []
        if self._zomato_path and self._zomato_path.exists():
            raw_zomato = json.loads(self._zomato_path.read_text(encoding="utf-8"))
            if isinstance(raw_zomato, list):
                zomato_orders = raw_zomato
        elif self._enable_live_chrome:
            zomato_orders = self.fetch_live_zomato_orders(start_date.year)
        if zomato_orders:
            evidences.extend(self.match_zomato_orders(rows, zomato_orders))

        # 2. Zepto
        if self._zepto_path and self._zepto_path.exists():
            raw_zepto = json.loads(self._zepto_path.read_text(encoding="utf-8"))
            evidences.extend(self.match_zepto_orders(rows, raw_zepto))

        # 3. Blinkit
        if self._blinkit_path and self._blinkit_path.exists():
            raw_blinkit = json.loads(self._blinkit_path.read_text(encoding="utf-8"))
            evidences.extend(self.match_blinkit_orders(rows, raw_blinkit, start_date.year))

        # 4. Amazon
        if self._amazon_path and self._amazon_path.exists():
            raw_amazon = json.loads(self._amazon_path.read_text(encoding="utf-8"))
            evidences.extend(self.match_amazon_orders(rows, raw_amazon))

        # 5. Uber
        if self._uber_path and self._uber_path.exists():
            raw_uber = json.loads(self._uber_path.read_text(encoding="utf-8"))
            evidences.extend(self.match_uber_trips(rows, raw_uber))

        return evidences

    @staticmethod
    def fetch_live_zomato_orders(target_year: int, max_pages: int = 10) -> list[dict[str, Any]]:
        """Fetch Zomato order history directly from an open zomato.com tab in Google Chrome."""
        js_batch = f"""
        (function() {{
          window.__z_done = false;
          window.__z_orders = [];
          (async () => {{
            for (let p = 1; p <= {max_pages}; p++) {{
              const r = await fetch('/webroutes/user/orders?page=' + p, {{ credentials: 'include' }});
              const j = await r.json();
              const map = (j && j.entities && j.entities.ORDER) ? j.entities.ORDER : {{}};
              const list = Object.values(map);
              if (list.length === 0) break;
              let hasYear = false;
              for (const o of list) {{
                if (String(o.orderDate || '').includes('{target_year}')) hasYear = true;
                window.__z_orders.push({{
                  orderId: o.orderId,
                  totalCost: o.totalCost,
                  orderDate: o.orderDate,
                  dishString: o.dishString,
                  status: o.status,
                  paymentStatus: o.paymentStatus,
                  resName: (o.resInfo && o.resInfo.name) ? o.resInfo.name : ''
                }});
              }}
              if (!hasYear) break;
            }}
            window.__z_done = true;
          }})();
          return "STARTED";
        }})()
        """
        if execute_chrome_tab_js("zomato.com", js_batch) != "STARTED":
            return []
        for _ in range(20):
            time.sleep(0.5)
            if execute_chrome_tab_js("zomato.com", "String(!!window.__z_done)") == "true":
                break
        raw_json = execute_chrome_tab_js("zomato.com", "JSON.stringify(window.__z_orders || [])")
        if not raw_json:
            return []
        try:
            parsed = json.loads(raw_json)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []

    def match_zomato_orders(
        self,
        rows: Sequence[StatementRow],
        orders: Sequence[dict[str, Any]],
    ) -> list[EnrichmentEvidence]:
        zomato_rows = [
            r
            for r in rows
            if not r.is_credit and ("ZOMATO" in r.narration.upper() or "ETERNAL" in r.narration.upper())
        ]
        if not zomato_rows:
            return []

        used_rows: set[int] = set()
        out: list[EnrichmentEvidence] = []

        for order in orders:
            # Support both normalized {"date_iso", "amount_minor", "note"} and raw Zomato web_order_history objects
            if "date_iso" in order and "amount_minor" in order:
                dt_iso = str(order["date_iso"])
                amt_minor = int(order["amount_minor"])
                items_note = str(order.get("note") or "")
                cat = str(order.get("category") or "Meals & Fast Food")
            else:
                if order.get("paymentStatus") == 0:
                    continue
                amt_minor_opt = _parse_minor_amount(str(order.get("totalCost") or ""))
                if amt_minor_opt is None:
                    continue
                amt_minor = amt_minor_opt
                raw_dt = str(order.get("orderDate") or "").split(" at ")[0].strip()
                try:
                    dt_iso = datetime.strptime(raw_dt, "%B %d, %Y").strftime("%Y-%m-%d")
                except ValueError:
                    continue
                dishes = format_zomato_dishes(str(order.get("dishString") or ""))
                res_name = str(order.get("resName") or "").strip()
                items_note = f"{dishes} ({res_name})" if (dishes and res_name) else dishes
                cat = classify_zomato_category(dishes, res_name)

            if not items_note:
                continue
            matched = _match_best_row(zomato_rows, dt_iso, amt_minor, used_rows, max_day_diff=1)
            if matched is not None:
                used_rows.add(matched.row_index)
                out.append(
                    EnrichmentEvidence(
                        source_name=f"{self.name}:zomato",
                        priority=EvidencePriority.WEB_ORDER_API,
                        confidence=0.98,
                        row_index=matched.row_index,
                        category=cat,
                        payee="Zomato",
                        note=items_note,
                        needs_review=False,
                    )
                )

        # Handle ₹1.00 Zomato UPI mandate verification rows
        for row in zomato_rows:
            if row.row_index not in used_rows and row.amount_minor == 100:
                used_rows.add(row.row_index)
                out.append(
                    EnrichmentEvidence(
                        source_name=f"{self.name}:zomato",
                        priority=EvidencePriority.WEB_ORDER_API,
                        confidence=0.98,
                        row_index=row.row_index,
                        category="Meals & Fast Food",
                        payee="Zomato",
                        note="Zomato UPI mandate verification",
                        needs_review=False,
                    )
                )
        return out

    def match_zepto_orders(
        self,
        rows: Sequence[StatementRow],
        raw_zepto: list[dict[str, Any]] | dict[str, Any],
    ) -> list[EnrichmentEvidence]:
        zepto_rows = [r for r in rows if not r.is_credit and "ZEPTO" in r.narration.upper()]
        if not zepto_rows:
            return []

        used_rows: set[int] = set()
        out: list[EnrichmentEvidence] = []

        if isinstance(raw_zepto, dict):
            # Format from zepto_items_2026.json: keyed by row_index or order_id -> raw order page text or dict
            for key, val in raw_zepto.items():
                if isinstance(val, str):
                    note, cat = parse_zepto_order_detail_text(val)
                    if note and key.isdigit():
                        r_idx = int(key)
                        if any(r.row_index == r_idx for r in zepto_rows):
                            used_rows.add(r_idx)
                            out.append(
                                EnrichmentEvidence(
                                    source_name=f"{self.name}:zepto",
                                    priority=EvidencePriority.WEB_ORDER_API,
                                    confidence=0.98,
                                    row_index=r_idx,
                                    category=cat,
                                    payee="Zepto",
                                    note=note,
                                    needs_review=False,
                                )
                            )
                elif isinstance(val, dict) and isinstance(val.get("text"), str) and val["text"]:
                    note, cat = parse_zepto_order_detail_text(val["text"])
                    if note and key.isdigit():
                        r_idx = int(key)
                        used_rows.add(r_idx)
                        out.append(
                            EnrichmentEvidence(
                                source_name=f"{self.name}:zepto",
                                priority=EvidencePriority.WEB_ORDER_API,
                                confidence=0.98,
                                row_index=r_idx,
                                category=cat,
                                payee="Zepto",
                                note=note,
                                needs_review=False,
                            )
                        )
            return out

        for order in raw_zepto:
            dt_iso = str(order.get("date_iso") or order.get("date") or "")
            amt_minor = _parse_minor_amount(order.get("amount_minor") or order.get("amount") or 0)
            if not dt_iso or amt_minor is None:
                continue
            raw_text = str(order.get("raw_text") or "")
            if raw_text:
                items_note, cat = parse_zepto_order_detail_text(raw_text)
            else:
                items_note = str(order.get("note") or "")
                cat = str(order.get("category") or "Groceries")
            if not items_note:
                continue
            matched = _match_best_row(zepto_rows, dt_iso, amt_minor, used_rows, max_day_diff=1)
            if matched is not None:
                used_rows.add(matched.row_index)
                out.append(
                    EnrichmentEvidence(
                        source_name=f"{self.name}:zepto",
                        priority=EvidencePriority.WEB_ORDER_API,
                        confidence=0.98,
                        row_index=matched.row_index,
                        category=cat,
                        payee="Zepto",
                        note=items_note,
                        needs_review=False,
                    )
                )
        return out

    def match_blinkit_orders(
        self,
        rows: Sequence[StatementRow],
        raw_blinkit: list[dict[str, Any]] | dict[str, Any],
        default_year: int = 2026,
    ) -> list[EnrichmentEvidence]:
        blinkit_rows = [r for r in rows if not r.is_credit and "BLINKIT" in r.narration.upper()]
        if not blinkit_rows:
            return []

        orders_list: list[dict[str, Any]] = (
            raw_blinkit.get("blinkit", []) if isinstance(raw_blinkit, dict) else raw_blinkit
        )
        # Blinkit can split a single checkout into 2 sub-shipments on the same timestamp; aggregate by dateStr
        by_dtstr: dict[str, dict[str, Any]] = {}
        for bo in orders_list:
            if "date_iso" in bo and "amount_minor" in bo:
                key = f"{bo['date_iso']}_{bo['amount_minor']}"
                by_dtstr[key] = {
                    "date_iso": str(bo["date_iso"]),
                    "amt": int(bo["amount_minor"]),
                    "items": [str(bo.get("note") or "")],
                }
                continue
            dstr = str(bo.get("dateStr") or "")
            amt_opt = _parse_minor_amount(str(bo.get("amount") or ""))
            if not dstr or amt_opt is None:
                continue
            day_mon = dstr.split(",")[0].strip()
            try:
                dt_iso = datetime.strptime(f"{day_mon} {default_year}", "%d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                continue
            items = [
                f"{it.get('quantity', 1)}x {it['name']}"
                for it in bo.get("items", [])
                if isinstance(it, dict) and it.get("name")
            ]
            if dstr not in by_dtstr:
                by_dtstr[dstr] = {"date_iso": dt_iso, "amt": 0, "items": []}
            by_dtstr[dstr]["amt"] += amt_opt
            by_dtstr[dstr]["items"].extend(items)

        used_rows: set[int] = set()
        out: list[EnrichmentEvidence] = []
        for info in by_dtstr.values():
            matched = _match_best_row(
                blinkit_rows, info["date_iso"], int(info["amt"]), used_rows, max_day_diff=1
            )
            if matched is not None and info["items"]:
                used_rows.add(matched.row_index)
                note = "\n".join(info["items"])
                out.append(
                    EnrichmentEvidence(
                        source_name=f"{self.name}:blinkit",
                        priority=EvidencePriority.WEB_ORDER_API,
                        confidence=0.98,
                        row_index=matched.row_index,
                        category="Groceries",
                        payee="Blinkit",
                        note=note,
                        needs_review=False,
                    )
                )
        return out

    def match_amazon_orders(
        self,
        rows: Sequence[StatementRow],
        orders: Sequence[dict[str, Any]],
    ) -> list[EnrichmentEvidence]:
        amazon_rows = [r for r in rows if not r.is_credit and "AMAZON" in r.narration.upper()]
        if not amazon_rows:
            return []

        used_rows: set[int] = set()
        out: list[EnrichmentEvidence] = []
        for order in orders:
            dt_iso = str(order.get("date_iso") or "")
            amt_minor = int(order.get("amount_minor", -1))
            items_note = str(order.get("note") or "")
            cat = str(order.get("category") or "Shopping")
            if not dt_iso or amt_minor < 0 or not items_note:
                continue
            matched = _match_best_row(amazon_rows, dt_iso, amt_minor, used_rows, max_day_diff=1)
            if matched is not None:
                used_rows.add(matched.row_index)
                out.append(
                    EnrichmentEvidence(
                        source_name=f"{self.name}:amazon",
                        priority=EvidencePriority.WEB_ORDER_API,
                        confidence=0.98,
                        row_index=matched.row_index,
                        category=cat,
                        payee="Amazon",
                        note=items_note,
                        needs_review=False,
                    )
                )
        return out

    def match_uber_trips(
        self,
        rows: Sequence[StatementRow],
        trips: Sequence[dict[str, Any]],
    ) -> list[EnrichmentEvidence]:
        uber_rows = [
            r
            for r in rows
            if not r.is_credit and ("UBER" in r.narration.upper() or "JRK TOURS" in r.narration.upper())
        ]
        if not uber_rows:
            return []

        used_rows: set[int] = set()
        out: list[EnrichmentEvidence] = []
        for trip in trips:
            dt_iso = str(trip.get("date_iso") or "")
            amt_minor = _parse_minor_amount(trip.get("amount_minor") or trip.get("fare") or 0)
            if not dt_iso or amt_minor is None or amt_minor <= 0:
                continue
            vehicle = str(trip.get("vehicle_type") or "Auto").upper()
            category = "Cab" if any(k in vehicle for k in ("GO", "PREMIER", "SEDAN", "XL", "CAB")) else "Auto"
            cancelled = bool(trip.get("cancelled", False))
            pickup = str(trip.get("pickup") or "")
            drop = str(trip.get("drop") or "")

            if trip.get("note"):
                note = str(trip["note"])
            elif cancelled:
                loc = self._gazetteer.normalize_address(drop or pickup)
                note = f"Uber cancellation fee ({loc})" if loc != "Unknown" else "Uber cancellation fee"
            elif pickup and drop:
                src = self._gazetteer.normalize_address(pickup)
                dst = self._gazetteer.normalize_address(drop)
                note = f"{src} to {dst}"
            elif drop:
                dst = self._gazetteer.normalize_address(drop)
                note = f"Ride to {dst}"
            else:
                note = "Uber ride"

            matched = _match_best_row(uber_rows, dt_iso, amt_minor, used_rows, max_day_diff=1)
            if matched is not None:
                used_rows.add(matched.row_index)
                out.append(
                    EnrichmentEvidence(
                        source_name=f"{self.name}:uber",
                        priority=EvidencePriority.WEB_ORDER_API,
                        confidence=0.98,
                        row_index=matched.row_index,
                        category=category,
                        payee="Uber",
                        note=note,
                        needs_review=False,
                    )
                )
        return out
