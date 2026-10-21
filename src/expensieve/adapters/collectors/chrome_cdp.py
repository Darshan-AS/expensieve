"""Chrome session order collector (Zomato, Zepto, Amazon) via macOS AppleScript / JSON cache."""

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from expensieve.domain.models import (
    EnrichmentEvidence,
    EvidencePriority,
    StatementRow,
)


class CachedMerchantOrderCollector:
    """Matches Zomato, Zepto, and Amazon orders from cached or live web session JSON dumps."""

    name = "merchant_web_orders"

    def __init__(
        self,
        zomato_json_path: Path | None = None,
        zepto_json_path: Path | None = None,
        amazon_json_path: Path | None = None,
    ) -> None:
        self._zomato_path = zomato_json_path
        self._zepto_path = zepto_json_path
        self._amazon_path = amazon_json_path

    def collect(
        self,
        rows: Sequence[StatementRow],
        start_date: date,
        end_date: date,
    ) -> Sequence[EnrichmentEvidence]:
        del start_date, end_date
        evidences: list[EnrichmentEvidence] = []
        if self._zomato_path and self._zomato_path.exists():
            evidences.extend(self._match_zomato(rows, self._zomato_path))
        if self._zepto_path and self._zepto_path.exists():
            evidences.extend(self._match_zepto(rows, self._zepto_path))
        if self._amazon_path and self._amazon_path.exists():
            evidences.extend(self._match_amazon(rows, self._amazon_path))
        return evidences

    def _match_zomato(self, rows: Sequence[StatementRow], path: Path) -> list[EnrichmentEvidence]:
        orders: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        out: list[EnrichmentEvidence] = []
        for row in rows:
            if "ZOMATO" not in row.narration.upper() and "ETERNAL" not in row.narration.upper():
                continue
            for order in orders:
                if order.get("date_iso") == row.date_iso and int(order.get("amount_minor", -1)) == row.amount_minor:
                    items_note = str(order.get("note") or "")
                    if items_note:
                        out.append(
                            EnrichmentEvidence(
                                source_name=f"{self.name}:zomato",
                                priority=EvidencePriority.WEB_ORDER_API,
                                confidence=0.98,
                                row_index=row.row_index,
                                category="Meals & Fast Food",
                                payee="Zomato",
                                note=items_note,
                                needs_review=False,
                            )
                        )
                        break
        return out

    def _match_zepto(self, rows: Sequence[StatementRow], path: Path) -> list[EnrichmentEvidence]:
        orders: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        out: list[EnrichmentEvidence] = []
        for row in rows:
            if "ZEPTO" not in row.narration.upper():
                continue
            for order in orders:
                if order.get("date_iso") == row.date_iso and int(order.get("amount_minor", -1)) == row.amount_minor:
                    items_note = str(order.get("note") or "")
                    if items_note:
                        out.append(
                            EnrichmentEvidence(
                                source_name=f"{self.name}:zepto",
                                priority=EvidencePriority.WEB_ORDER_API,
                                confidence=0.98,
                                row_index=row.row_index,
                                category="Groceries",
                                payee="Zepto",
                                note=items_note,
                                needs_review=False,
                            )
                        )
                        break
        return out

    def _match_amazon(self, rows: Sequence[StatementRow], path: Path) -> list[EnrichmentEvidence]:
        orders: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        out: list[EnrichmentEvidence] = []
        for row in rows:
            if "AMAZON" not in row.narration.upper():
                continue
            for order in orders:
                if order.get("date_iso") == row.date_iso and int(order.get("amount_minor", -1)) == row.amount_minor:
                    items_note = str(order.get("note") or "")
                    cat = str(order.get("category") or "Shopping")
                    if items_note:
                        out.append(
                            EnrichmentEvidence(
                                source_name=f"{self.name}:amazon",
                                priority=EvidencePriority.WEB_ORDER_API,
                                confidence=0.98,
                                row_index=row.row_index,
                                category=cat,
                                payee="Amazon",
                                note=items_note,
                                needs_review=False,
                            )
                        )
                        break
        return out
