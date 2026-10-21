"""Priority-based EvidenceMerger that joins StatementRows with multi-source EnrichmentEvidence."""

from collections import defaultdict
from collections.abc import Sequence

from expensieve.domain.models import (
    CanonicalTransaction,
    EnrichmentEvidence,
    PaymentMethod,
    StatementRow,
    TransactionKind,
)


class EvidenceMerger:
    """Deterministically merges multiple EnrichmentEvidence items per StatementRow by priority."""

    def __init__(self, account_name: str = "HDFC", confidence_threshold: float = 0.85) -> None:
        self._account_name = account_name
        self._confidence_threshold = confidence_threshold

    def merge(
        self,
        rows: Sequence[StatementRow],
        evidences: Sequence[EnrichmentEvidence],
    ) -> list[CanonicalTransaction]:
        """Produce a sorted list of CanonicalTransactions covering every StatementRow."""
        by_row_idx: dict[int, list[EnrichmentEvidence]] = defaultdict(list)
        by_rrn: dict[str, list[EnrichmentEvidence]] = defaultdict(list)

        for ev in evidences:
            if ev.row_index is not None:
                by_row_idx[ev.row_index].append(ev)
            elif ev.rrn_12:
                by_rrn[ev.rrn_12].append(ev)

        results: list[CanonicalTransaction] = []
        for row in rows:
            row_evs = list(by_row_idx.get(row.row_index, ()))
            if row.rrn_12 and row.rrn_12 in by_rrn:
                row_evs.extend(by_rrn[row.rrn_12])

            # Sort by (priority, confidence) ascending so higher priority overwrites lower priority
            row_evs.sort(key=lambda e: (int(e.priority), e.confidence))

            kind = TransactionKind.INCOME if row.is_credit else TransactionKind.EXPENSE
            pay_method = PaymentMethod.TRANSFER
            category = "Others"
            payee = ""
            note = row.narration
            transfer_acc: str | None = None
            timestamp_iso = f"{row.date_iso}T06:30:00Z"
            labels_set: list[str] = []
            best_conf = 0.5
            needs_review = True
            sources: list[str] = []

            for ev in row_evs:
                if ev.source_name not in sources:
                    sources.append(ev.source_name)
                if ev.timestamp_iso:
                    timestamp_iso = ev.timestamp_iso
                if ev.kind is not None:
                    kind = ev.kind
                if ev.payment_method is not None:
                    pay_method = ev.payment_method
                if ev.category is not None:
                    category = ev.category
                if ev.payee is not None:
                    payee = ev.payee
                if ev.note is not None:
                    note = ev.note
                if ev.transfer_account is not None:
                    transfer_acc = ev.transfer_account
                for lbl in ev.labels:
                    if lbl not in labels_set:
                        labels_set.append(lbl)
                best_conf = max(best_conf, ev.confidence)
                if ev.needs_review is not None:
                    needs_review = ev.needs_review

            if best_conf < self._confidence_threshold:
                needs_review = True
            if needs_review and "Needs Review" not in labels_set:
                labels_set.append("Needs Review")
            elif not needs_review and "Needs Review" in labels_set:
                labels_set.remove("Needs Review")

            results.append(
                CanonicalTransaction(
                    row_index=row.row_index,
                    account_name=self._account_name,
                    date_iso=row.date_iso,
                    timestamp_iso=timestamp_iso,
                    amount_minor=row.amount_minor,
                    signed_delta_minor=row.signed_delta_minor,
                    kind=kind,
                    payment_method=pay_method,
                    category=category,
                    payee=payee,
                    note=note,
                    transfer_account=transfer_acc,
                    labels=tuple(labels_set),
                    confidence=best_conf,
                    needs_review=needs_review,
                    evidence_sources=tuple(sources),
                )
            )

        return results
