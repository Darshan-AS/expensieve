"""Pure domain value objects and enums for ExpenSieve (Zero I/O, App-Agnostic)."""

from dataclasses import dataclass, field
from datetime import date
from enum import IntEnum, StrEnum


class TransactionKind(StrEnum):
    """Canonical transaction classification across any ledger backend."""

    EXPENSE = "EXPENSE"
    INCOME = "INCOME"
    TRANSFER = "TRANSFER"


class PaymentMethod(StrEnum):
    """Canonical payment method."""

    TRANSFER = "TRANSFER"  # UPI / IMPS / NEFT / RTGS / ACH
    DEBIT_CARD = "DEBIT_CARD"
    CREDIT_CARD = "CREDIT_CARD"
    CASH = "CASH"


class EvidencePriority(IntEnum):
    """Deterministic precedence hierarchy when merging multiple enrichment sources."""

    DEFAULT_FALLBACK = 10
    HISTORICAL_PAYEE = 20
    DECLARATIVE_RULE = 40
    UPI_REMARK = 60
    TIMELINE_GPS = 75
    WEB_ORDER_API = 85
    INVOICE_PDF = 95
    EXPLICIT_OVERRIDE = 100


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """WGS-84 latitude/longitude coordinate pair."""

    lat: float
    lng: float

    @classmethod
    def from_latlng_str(cls, raw: str) -> GeoPoint:
        """Parse '13.0250041°, 77.673822°' into a GeoPoint."""
        cleaned = raw.replace("°", "").strip()
        parts = [p.strip() for p in cleaned.split(",")]
        if len(parts) != 2:
            raise ValueError(f"Invalid latLng string: {raw!r}")
        return cls(lat=float(parts[0]), lng=float(parts[1]))


@dataclass(frozen=True, slots=True)
class StatementRow:
    """Normalized row parsed from any bank or card statement (the financial spine)."""

    row_index: int
    date_iso: str  # YYYY-MM-DD
    value_date_iso: str  # YYYY-MM-DD
    narration: str
    reference_no: str  # 16-digit or 12-digit reference / cheque number
    debit_minor: int  # Paise (>= 0)
    credit_minor: int  # Paise (>= 0)
    closing_balance_minor: int  # Signed paise after this row

    @property
    def amount_minor(self) -> int:
        """Absolute transaction amount in minor units (paise)."""
        return self.debit_minor if self.debit_minor > 0 else self.credit_minor

    @property
    def signed_delta_minor(self) -> int:
        """Signed account balance delta in minor units (+credit, -debit)."""
        return self.credit_minor - self.debit_minor

    @property
    def is_credit(self) -> bool:
        return self.credit_minor > 0

    @property
    def rrn_12(self) -> str:
        """Normalized 12-digit UPI RRN / IMPS reference number (stripped of leading zeros)."""
        digits = "".join(ch for ch in self.reference_no if ch.isdigit())
        if len(digits) >= 12:
            return digits[-12:]
        return digits

    @property
    def statement_date(self) -> date:
        return date.fromisoformat(self.date_iso)


@dataclass(frozen=True, slots=True)
class EnrichmentEvidence:
    """A piece of enrichment evidence emitted by a collector or rule for a StatementRow."""

    source_name: str
    priority: EvidencePriority
    confidence: float  # 0.0 to 1.0
    row_index: int | None = None
    rrn_12: str | None = None
    timestamp_iso: str | None = None  # Exact UTC ISO-8601 timestamp (e.g. from SMS or receipt)
    category: str | None = None
    payee: str | None = None
    note: str | None = None
    kind: TransactionKind | None = None
    transfer_account: str | None = None
    payment_method: PaymentMethod | None = None
    labels: tuple[str, ...] = ()
    needs_review: bool | None = None


@dataclass(frozen=True, slots=True)
class CanonicalTransaction:
    """App-agnostic, fully enriched transaction ready for staging or pushing to any LedgerSink."""

    row_index: int
    account_name: str
    date_iso: str
    timestamp_iso: str
    amount_minor: int
    signed_delta_minor: int
    kind: TransactionKind
    payment_method: PaymentMethod
    category: str
    payee: str
    note: str
    transfer_account: str | None = None
    labels: tuple[str, ...] = ()
    confidence: float = 1.0
    needs_review: bool = False
    debt_group_id: str | None = None
    evidence_sources: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DebtLinkGroup:
    """App-agnostic representation of a lent-and-repaid (or borrowed) group of rows."""

    group_id: str
    person_name: str
    reason: str
    total_amount_minor: int
    lent_row_indices: tuple[int, ...]
    repayment_row_indices: tuple[int, ...]
    paid_back: bool = True
