"""Structural typing.Protocol interfaces (Ports) for ExpenSieve."""

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from expensieve.domain.models import (
    CanonicalTransaction,
    DebtLinkGroup,
    EnrichmentEvidence,
    GeoPoint,
    StatementRow,
)


class StatementSource(Protocol):
    """Port for parsing any bank or credit card statement into normalized StatementRows."""

    account_name: str

    def parse(self) -> Sequence[StatementRow]:
        """Return ordered statement rows."""
        ...

    def opening_balance_minor(self) -> int:
        """Return statement opening balance in minor units (paise)."""
        ...

    def closing_balance_minor(self) -> int:
        """Return statement closing balance in minor units (paise)."""
        ...


class EvidenceCollector(Protocol):
    """Port for any multi-source enrichment collector (SMS, Timeline, Gmail, Chrome, Rules)."""

    name: str

    def collect(
        self,
        rows: Sequence[StatementRow],
        start_date: date,
        end_date: date,
    ) -> Sequence[EnrichmentEvidence]:
        """Collect enrichment evidence for the given statement window."""
        ...


class ReverseGeocoder(Protocol):
    """Port for resolving unknown (lat, lng) coordinates into concise place names."""

    def resolve_place_name(self, point: GeoPoint) -> str | None:
        """Return a short human-readable neighbourhood or building name."""
        ...


class LedgerSink(Protocol):
    """Port for pushing canonical enriched transactions to a target finance app (e.g. Sourdough)."""

    name: str

    def verify_opening_balance(self, account_name: str, expected_minor: int) -> tuple[bool, int]:
        """Check whether the live account balance matches the statement opening balance."""
        ...

    def push_transactions(
        self,
        account_name: str,
        transactions: Sequence[CanonicalTransaction],
        debt_groups: Sequence[DebtLinkGroup],
        expected_closing_minor: int,
        dry_run: bool = False,
    ) -> str:
        """Validate and push transactions + debt links, returning the rollback journal ID."""
        ...
