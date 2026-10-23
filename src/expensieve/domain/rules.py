"""Declarative classification and enrichment rules for ExpenSieve."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from expensieve.adapters.statements.hdfc_delimited import parse_upi_narration
from expensieve.domain.models import (
    EnrichmentEvidence,
    EvidencePriority,
    PaymentMethod,
    StatementRow,
    TransactionKind,
)

GENERIC_UPI_NOTES: frozenset[str] = frozenset(
    {
        "",
        "UPI",
        "PAID VIA CRED",
        "PAYMENT ON CRED",
        "NO REMARKS",
        "NO REMARK",
        "UPIINTENT",
        "MANDATEEXECUTE",
        "ZOMATO PAYMENT",
        "ZOMATOONLINEORDER",
        "BLINKIT PAYMENT",
        "REQUEST FROM AMAZO",
        "PAYMENT FROM PHONE",
        "YOU ARE PAYING FOR",
        "PAY TO BHARATPE ME",
        "PAY VIA RAZORPAY",
        "PAYVIARAZORPAY",
        "COLLECT",
        "ZEPTO",
        "RAPIDO",
        "NAMMA YATRI",
        "METRO TICKET",
    }
)


@dataclass(frozen=True, slots=True)
class TripWindow:
    """Date range and note prefix for a holiday trip."""

    prefix: str
    start_date: date
    end_date: date

    def matches_date(self, d: date) -> bool:
        return self.start_date <= d <= self.end_date


DEFAULT_TRIP_WINDOWS: tuple[TripWindow, ...] = (
    TripWindow(
        prefix="[Singapore & Malaysia Sep\u2013Oct 2026]",
        start_date=date(2026, 9, 27),
        end_date=date(2026, 10, 6),
    ),
)


def humanize_upi_note(raw_note: str) -> str:
    """Format an uppercase UPI remark into clean sentence case."""
    clean = raw_note.strip()
    if not clean:
        return ""
    if clean.isupper():
        return clean.capitalize()
    return clean


def infer_category_from_upi_note(note_upper: str, payee_upper: str) -> tuple[str, str]:
    """Infer canonical category and clean payee from a user-written UPI remark."""
    if any(k in note_upper for k in ("RAPIDO CAB", "UBER CAB", "CAB TO", "OLA CAB")):
        payee = "Rapido" if "RAPIDO" in note_upper else ("Uber" if "UBER" in note_upper else "")
        return "Cab", payee
    if "RAPIDO" in note_upper:
        return "Auto", "Rapido"
    if "NAMMA YATRI" in note_upper or "NAMMA YAATRI" in note_upper:
        return "Auto", "Namma Yaatri"
    if "UBER" in note_upper:
        return "Auto", "Uber"
    if "AUTO" in note_upper:
        return "Auto", ""
    if "METRO" in note_upper:
        return "Metro", "Namma Metro"
    if "BUS" in note_upper:
        return "Bus", ""
    if "TRAIN" in note_upper and "DINNER" not in note_upper and "FOOD" not in note_upper:
        return "Train", ""

    if any(k in note_upper for k in ("COFFEE", "TEA", "CHAI", "CAFE")):
        return "Coffee, Tea & Cafe", ""
    if any(
        k in note_upper
        for k in (
            "JUICE",
            "MILKSHAKE",
            "FLAVOURED MILK",
            "LASSI",
            "SUGARCANE",
            "TENDER COCONUT",
            "BADAM MILK",
        )
    ):
        return "Juice & Milkshake", ""
    if any(
        k in note_upper
        for k in (
            "ICE CREAM",
            "ICECREAM",
            "KULFI",
            "CAKE",
            "KINDER JOY",
            "CHOCOLATE",
            "SWEET",
            "DESSERT",
            "PASTRY",
        )
    ):
        return "Desserts & Sweets", ""

    if any(
        k in note_upper
        for k in (
            "PANI PURI",
            "CHAT",
            "CHAAT",
            "SNACK",
            "PUFF",
            "MOMO",
            "SAMOSA",
            "BHAJI",
            "BONDA",
            "VADA",
            "POPCORN",
            "CORN",
            "ALOO TWIST",
            "GOBI",
            "BAJJI",
            "TWISTER",
        )
    ):
        return "Snacks & Chaats", ""

    if any(
        k in note_upper
        for k in (
            "PAROTA",
            "PARATHA",
            "CHAPATI",
            "PULAV",
            "FRIED RICE",
            "NOODLES",
            "DOSA",
            "IDLY",
            "IDLI",
            "BIRYANI",
            "CURRY",
            "CURRIES",
            "LUNCH",
            "DINNER",
            "BREAKFAST",
            "MEAL",
            "SHAWARMA",
            "ROLL",
            "MANCHURIAN",
            "PIZZA",
            "BURGER",
            "SANDWICH",
            "THALI",
            "RICE",
            "BATH",
        )
    ):
        return "Meals & Fast Food", ""

    if any(
        k in note_upper for k in ("GROCER", "MILK", "CURD", "VEGETABLE", "FRUIT", "EGG", "BREAD", "ICE APPLE")
    ):
        return "Groceries", ""
    if "WATER BILL" in note_upper or "ELECTRICITY" in note_upper:
        return "Energy, utilities", ""
    if "RENT" in note_upper:
        return "Rent", ""
    if "GIFT" in note_upper:
        return "Gifts, joy", ""
    if "HAIRCUT" in note_upper or "SALON" in note_upper or "COMPACT" in note_upper:
        return "Wellness, beauty", ""
    if "MEDICINE" in note_upper or "TABLET" in note_upper or "PHARMACY" in note_upper:
        return "Drug-store, chemist", ""
    if "JEWELLERY" in note_upper:
        return "Jewels, accessories", ""

    if any(
        k in payee_upper
        for k in (
            "HOTEL",
            "RESTAURANT",
            "CAFE",
            "FOOD",
            "KITCHEN",
            "BHAVAN",
            "UDUPI",
            "EXPRESS",
            "SHAWARMA",
            "BAKERY",
            "CONDIMENTS",
        )
    ):
        return "Meals & Fast Food", ""

    return "Others", ""


class DeclarativeRuleCollector:
    """Baseline rule engine that classifies StatementRows from narrations and UPI remarks."""

    name = "declarative_rules"

    def __init__(self, trip_windows: Sequence[TripWindow] = DEFAULT_TRIP_WINDOWS) -> None:
        self._trip_windows = tuple(trip_windows)

    def collect(
        self,
        rows: Sequence[StatementRow],
        start_date: date,
        end_date: date,
    ) -> Sequence[EnrichmentEvidence]:
        del start_date, end_date
        evidences: list[EnrichmentEvidence] = []
        for row in rows:
            evidences.append(self._classify_row(row))
        return evidences

    def _classify_row(self, row: StatementRow) -> EnrichmentEvidence:
        narr = row.narration
        narr_u = narr.upper()
        kind = TransactionKind.INCOME if row.is_credit else TransactionKind.EXPENSE
        upi = parse_upi_narration(narr, row.reference_no)
        pay_method = PaymentMethod.TRANSFER

        if not upi.is_upi:
            return self._classify_non_upi(row, narr, narr_u, kind)

        payee_u = upi.payee.upper()
        note_u = upi.note.upper()

        # 1. Self-transfers & Wallets
        if payee_u == "LITE" or "UPI-LITE-" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=1.0,
                row_index=row.row_index,
                kind=TransactionKind.TRANSFER,
                payment_method=pay_method,
                category="Transfer, withdraw",
                payee="",
                note="Transfer to UPI Lite Wallet",
                transfer_account="Wallets",
                needs_review=False,
            )

        if "NIYOX" in narr_u or "NIYO GLOBAL" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=1.0,
                row_index=row.row_index,
                kind=TransactionKind.TRANSFER,
                payment_method=pay_method,
                category="Transfer, withdraw",
                payee="NiyoX",
                note="Transfer to NiyoX",
                transfer_account="NiyoX",
                needs_review=False,
            )

        # 2. Platform Merchants (Zomato, Zepto, Blinkit, Swiggy, Amazon, Rapido, Namma Yatri, Uber, Metro)
        if "ZOMATO" in narr_u or "ETERNAL LIMITED" in narr_u:
            custom_note = "" if note_u in GENERIC_UPI_NOTES else humanize_upi_note(upi.note)
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.9 if custom_note else 0.7,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Meals & Fast Food",
                payee="Zomato",
                note=custom_note or "Zomato order",
                needs_review=not bool(custom_note),
            )

        if "ZEPTO" in narr_u:
            custom_note = "" if note_u in GENERIC_UPI_NOTES else humanize_upi_note(upi.note)
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.9 if custom_note else 0.65,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Groceries",
                payee="Zepto",
                note=custom_note or "Zepto order",
                needs_review=not bool(custom_note),
            )

        if "BLINKIT" in narr_u:
            custom_note = "" if note_u in GENERIC_UPI_NOTES else humanize_upi_note(upi.note)
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.9,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Groceries",
                payee="Blinkit",
                note=custom_note or "Blinkit order",
                needs_review=False,
            )

        if "SWIGGY" in narr_u:
            custom_note = "" if note_u in GENERIC_UPI_NOTES else humanize_upi_note(upi.note)
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.9 if custom_note else 0.7,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Meals & Fast Food",
                payee="Swiggy",
                note=custom_note or "Swiggy order",
                needs_review=not bool(custom_note),
            )

        if "AMAZON" in narr_u:
            custom_note = "" if note_u in GENERIC_UPI_NOTES else humanize_upi_note(upi.note)
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.9 if custom_note else 0.7,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Shopping",
                payee="Amazon",
                note=custom_note or "Amazon order",
                needs_review=not bool(custom_note),
            )

        if "BANGALORE METRO" in narr_u or "BMRC" in narr_u:
            is_recharge = row.amount_minor >= 30000
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=1.0 if is_recharge else 0.7,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Metro",
                payee="Namma Metro",
                note="Metro smart card recharge" if is_recharge else "Metro ticket",
                needs_review=not is_recharge,
            )

        if "RAPIDO522347" in narr_u or ("RAPIDO" in narr_u and "METRO" in note_u):
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.7,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Metro",
                payee="Rapido",
                note="Metro ticket (via Rapido)",
                needs_review=True,
            )

        if "RAPIDO" in narr_u:
            custom_note = "" if note_u in GENERIC_UPI_NOTES else humanize_upi_note(upi.note)
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.9 if custom_note else 0.65,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Auto",
                payee="Rapido",
                note=custom_note or "Rapido auto ride",
                needs_review=not bool(custom_note),
            )

        if "NAMMA YATRI" in narr_u or "NAMMA YAATRI" in narr_u:
            custom_note = "" if note_u in GENERIC_UPI_NOTES else humanize_upi_note(upi.note)
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.9 if custom_note else 0.65,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Auto",
                payee="Namma Yaatri",
                note=custom_note or "Namma Yatri auto ride",
                needs_review=not bool(custom_note),
            )

        if "UBER" in narr_u:
            custom_note = "" if note_u in GENERIC_UPI_NOTES else humanize_upi_note(upi.note)
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.9 if custom_note else 0.7,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category="Auto",
                payee="Uber",
                note=custom_note or "Uber ride",
                needs_review=not bool(custom_note),
            )

        # 3. User-written descriptive UPI note
        if note_u and note_u not in GENERIC_UPI_NOTES:
            cat, inferred_payee = infer_category_from_upi_note(note_u, payee_u)
            clean_note = humanize_upi_note(upi.note)
            labels: list[str] = []
            if any(k in note_u for k in ("MOM", "FAMILY", "FATHER", "HOSPITAL", "MANIPAL", "RAMAIAH")):
                labels.append("Family")
            for tw in self._trip_windows:
                if tw.matches_date(row.statement_date) and not clean_note.startswith(tw.prefix):
                    clean_note = f"{tw.prefix} {clean_note}".strip()
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.UPI_REMARK,
                confidence=0.9,
                row_index=row.row_index,
                kind=kind,
                payment_method=pay_method,
                category=cat,
                payee=inferred_payee or upi.payee.title(),
                note=clean_note,
                labels=tuple(labels),
                needs_review=False,
            )

        # 4. Generic/missing UPI note -> Low confidence, route to Needs Review unless Timeline/Invoice resolves it
        return EnrichmentEvidence(
            source_name=self.name,
            priority=EvidencePriority.DEFAULT_FALLBACK,
            confidence=0.4,
            row_index=row.row_index,
            kind=kind,
            payment_method=pay_method,
            category="Others",
            payee=upi.payee.title(),
            note="",
            needs_review=True,
        )

    def _classify_non_upi(
        self,
        row: StatementRow,
        narr: str,
        narr_u: str,
        kind: TransactionKind,
    ) -> EnrichmentEvidence:
        if "GOOGLE IT SERVICES INDIA" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=1.0,
                row_index=row.row_index,
                kind=TransactionKind.INCOME,
                payment_method=PaymentMethod.TRANSFER,
                category="Salary",
                payee="Google",
                note="Salary" if "SALARY" in narr_u else "Google reimbursement / payout",
                needs_review=False,
            )
        if "SHASHIKALA A S" in narr_u and "POCKET MONEY" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=1.0,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.TRANSFER,
                category="Gifts, joy",
                payee="",
                note="Pocket money to Mom",
                labels=("Family",),
                needs_review=False,
            )
        if "INTEREST PAID TILL" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=1.0,
                row_index=row.row_index,
                kind=TransactionKind.INCOME,
                payment_method=PaymentMethod.TRANSFER,
                category="Interests, dividends",
                payee="HDFC",
                note="Credit interest capitalised",
                needs_review=False,
            )
        if narr_u.startswith("ATW-"):
            loc = "Hassan" if "HASSAN" in narr_u else "Bangalore"
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.TRANSFER,
                payment_method=PaymentMethod.TRANSFER,
                category="Transfer, withdraw",
                payee="",
                note=f"ATM withdrawal ({loc})",
                transfer_account="Cash",
                needs_review=False,
            )
        if "TO NIYOX" in narr_u or ("ARASIKERE SHIVANANDA DARSHAN" in narr_u and "DCBL" in narr_u):
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=1.0,
                row_index=row.row_index,
                kind=TransactionKind.TRANSFER,
                payment_method=PaymentMethod.TRANSFER,
                category="Transfer, withdraw",
                payee="NiyoX",
                note="Transfer to NiyoX",
                transfer_account="NiyoX",
                needs_review=False,
            )
        if row.is_credit and (
            "TELE TRANSFER CREDIT" in narr_u or ("IMPS PMT" in narr_u and "REF#" in narr_u)
        ):
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=1.0,
                row_index=row.row_index,
                kind=TransactionKind.TRANSFER,
                payment_method=PaymentMethod.TRANSFER,
                category="Transfer, withdraw",
                payee="CRED",
                note="Credit card bill payment",
                transfer_account="HDFC",
                needs_review=False,
            )
        if "BLOSSOM BOOK HOUSE" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Books, audio, subscriptions",
                payee="Blossom Book House",
                note="",
                needs_review=False,
            )
        if "KAKE DI HATTI" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Meals & Fast Food",
                payee="Kake Di Hatti",
                note="Kake Di Hatti",
                needs_review=False,
            )
        if "REGENTA PLACE" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Meals & Fast Food",
                payee="Regenta Place",
                note="Regenta Place",
                needs_review=False,
            )
        if "REL RETAIL LTD DIGITAL" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Electronics, accessories",
                payee="Reliance Digital",
                note="",
                needs_review=False,
            )
        if "MANIPAL HEALTH ENTERPR" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Healthcare",
                payee="Manipal Hospital",
                note="",
                labels=("Family",),
                needs_review=False,
            )
        if "STORIES BAR AND KITCHE" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Meals & Fast Food",
                payee="Stories Bar and Kitchen",
                note="",
                needs_review=False,
            )
        if "NKP EMPIRE VENTURES" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Meals & Fast Food",
                payee="Empire Restaurant",
                note="",
                needs_review=False,
            )
        if "GOOGLEPLAY" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Charges, fees",
                payee="Google",
                note="",
                needs_review=False,
            )
        if "DESI MASALA" in narr_u:
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.95,
                row_index=row.row_index,
                kind=TransactionKind.EXPENSE,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Meals & Fast Food",
                payee="Desi Masala",
                note="",
                needs_review=False,
            )

        m_parent = re.search(r"\[Parent:\s*([^\]]+)\]", narr)
        parent_ctx = m_parent.group(1).strip() if m_parent else ""
        base_narr_u = re.sub(r"\[Parent:\s*[^\]]+\]", "", narr_u).strip()

        if base_narr_u.startswith("IGST-") or "IGST-VPS" in base_narr_u:
            igst_note = f"IGST on {parent_ctx}" if parent_ctx else "IGST on credit card fee"
            if row.is_credit:
                igst_note = f"{igst_note} reversal"
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.98,
                row_index=row.row_index,
                kind=kind,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Charges, fees",
                payee="HDFC",
                note=igst_note,
                needs_review=False,
            )
        if "FIRST YEAR MEMBERSHIP FEE" in base_narr_u or "RENEWAL MEMBERSHIP FEE" in base_narr_u:
            fee_label = (
                "First year membership fee"
                if "FIRST YEAR" in base_narr_u
                else "Renewal membership fee"
            )
            if row.is_credit:
                fee_label = f"{fee_label} reversal"
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.98,
                row_index=row.row_index,
                kind=kind,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Charges, fees",
                payee="HDFC",
                note=fee_label,
                needs_review=False,
            )
        if (
            "1% ON ALL DCC TRANSACTION" in base_narr_u
            or "CONSOLIDATED FCY MARKUP FEE" in base_narr_u
        ):
            base_fee = (
                "DCC markup fee"
                if "1% ON ALL DCC" in base_narr_u
                else "Foreign currency markup fee"
            )
            fee_note = f"{base_fee} ({parent_ctx})" if parent_ctx else base_fee
            return EnrichmentEvidence(
                source_name=self.name,
                priority=EvidencePriority.DECLARATIVE_RULE,
                confidence=0.98,
                row_index=row.row_index,
                kind=kind,
                payment_method=PaymentMethod.CREDIT_CARD,
                category="Charges, fees",
                payee="HDFC",
                note=fee_note,
                needs_review=False,
            )
        return EnrichmentEvidence(
            source_name=self.name,
            priority=EvidencePriority.DEFAULT_FALLBACK,
            confidence=0.5,
            row_index=row.row_index,
            kind=kind,
            payment_method=PaymentMethod.TRANSFER,
            category="Others",
            payee="",
            note=narr,
            needs_review=True,
        )
