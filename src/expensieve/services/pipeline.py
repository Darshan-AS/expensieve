"""End-to-end sync pipeline orchestrator for ExpenSieve."""

from collections.abc import Sequence
from dataclasses import dataclass

from expensieve.domain.models import (
    CanonicalTransaction,
    DebtLinkGroup,
    EnrichmentEvidence,
)
from expensieve.ports.protocols import (
    EvidenceCollector,
    LedgerSink,
    StatementSource,
)
from expensieve.services.evidence_merger import EvidenceMerger


@dataclass(frozen=True, slots=True)
class SyncRunSummary:
    """Summary report produced by a pipeline run."""

    account_name: str
    row_count: int
    opening_balance_minor: int
    closing_balance_minor: int
    net_delta_minor: int
    needs_review_count: int
    transactions: tuple[CanonicalTransaction, ...]
    journal_id: str | None = None


class SyncPipeline:
    """Coordinates StatementSource -> EvidenceCollectors -> EvidenceMerger -> LedgerSink."""

    def __init__(
        self,
        statement_source: StatementSource,
        collectors: Sequence[EvidenceCollector],
        merger: EvidenceMerger | None = None,
        sink: LedgerSink | None = None,
    ) -> None:
        self._source = statement_source
        self._collectors = tuple(collectors)
        self._merger = merger or EvidenceMerger(account_name=statement_source.account_name)
        self._sink = sink

    def run(
        self,
        debt_groups: Sequence[DebtLinkGroup] = (),
        push_to_sink: bool = False,
        dry_run: bool = True,
    ) -> SyncRunSummary:
        rows = self._source.parse()
        opening_minor = self._source.opening_balance_minor()
        closing_minor = self._source.closing_balance_minor()
        net_delta = sum(r.signed_delta_minor for r in rows)

        if opening_minor + net_delta != closing_minor:
            raise ValueError(
                f"Statement math error: opening ({opening_minor}) + delta ({net_delta}) != closing ({closing_minor})"
            )

        all_evidences: list[EnrichmentEvidence] = []
        if rows:
            start_d = min(r.statement_date for r in rows)
            end_d = max(r.statement_date for r in rows)
            for collector in self._collectors:
                evs = collector.collect(rows, start_d, end_d)
                all_evidences.extend(evs)

        merged = self._merger.merge(rows, all_evidences)
        nr_count = sum(1 for t in merged if t.needs_review)

        journal_id: str | None = None
        if push_to_sink and self._sink is not None and rows:
            journal_id = self._sink.push_transactions(
                account_name=self._source.account_name,
                transactions=merged,
                debt_groups=debt_groups,
                expected_closing_minor=closing_minor,
                dry_run=dry_run,
            )

        return SyncRunSummary(
            account_name=self._source.account_name,
            row_count=len(rows),
            opening_balance_minor=opening_minor,
            closing_balance_minor=closing_minor,
            net_delta_minor=net_delta,
            needs_review_count=nr_count,
            transactions=tuple(merged),
            journal_id=journal_id,
        )
