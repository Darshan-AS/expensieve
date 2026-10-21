"""Wallet by BudgetBakers CouchDB LedgerSink adapter powered by `sourdough`."""

import uuid
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

from sourdough.config import resolve_paths
from sourdough.domain.builders import (
    build_record,
    build_transfer_pair,
    utc_now_iso,
)
from sourdough.domain.enums import PaymentType, RecordState, RecordType
from sourdough.domain.models import LabelDocument, RecordDocument
from sourdough.services.records import WalletService

from expensieve.domain.models import (
    CanonicalTransaction,
    DebtLinkGroup,
    PaymentMethod,
    TransactionKind,
)


def _map_payment_type(method: PaymentMethod) -> PaymentType:
    if method == PaymentMethod.DEBIT_CARD:
        return PaymentType.DEBIT_CARD
    if method == PaymentMethod.CREDIT_CARD:
        return PaymentType.CREDIT_CARD
    if method == PaymentMethod.CASH:
        return PaymentType.CASH
    return PaymentType.MOBILE_PAYMENT


class SourdoughLedgerSink:
    """Pushes CanonicalTransactions and DebtLinkGroups to Wallet by BudgetBakers via sourdough."""

    name = "sourdough_wallet"

    def __init__(self, service: WalletService | None = None) -> None:
        self._service = service or WalletService(paths=resolve_paths())

    def _compute_live_account_balance(self, account_id: str) -> int:
        all_raw = self._service.client.all_docs_by_prefix("Record_")
        total_minor = 0
        for r in all_raw:
            if (
                r.get("reservedModelType") == "Record"
                and not r.get("_deleted")
                and r.get("accountId") == account_id
            ):
                amt = int(r.get("amount", 0))
                if int(r.get("type", 1)) == 0:
                    total_minor += amt
                else:
                    total_minor -= amt
        return total_minor

    def verify_opening_balance(self, account_name: str, expected_minor: int) -> tuple[bool, int]:
        """Verify live CouchDB balance for account_name against expected_minor."""
        acc = self._service.resolver.resolve_account(account_name)
        live_minor = self._compute_live_account_balance(acc.doc_id)
        return (live_minor == expected_minor, live_minor)

    def push_transactions(
        self,
        account_name: str,
        transactions: Sequence[CanonicalTransaction],
        debt_groups: Sequence[DebtLinkGroup],
        expected_closing_minor: int,
        dry_run: bool = False,
    ) -> str:
        """Validate all documents, verify closing balance math, and bulk-push to CouchDB."""
        if dry_run:
            self._service.cache.ttl_seconds = 10**9

        snap = self._service.cache.get_snapshot(force_refresh=not dry_run)
        label_id_by_name = {lbl.name.lower(): lbl.doc_id for lbl in snap.labels}

        # Ensure "Needs Review" label exists if any transaction requires it
        if "needs review" not in label_id_by_name and "needs-review" not in label_id_by_name:
            if dry_run:
                tmp_lbl = LabelDocument.from_couch_dict(
                    {
                        "_id": f"-HashTag_{uuid.uuid4()}",
                        "name": "Needs Review",
                        "color": "#ffa726",
                        "archived": False,
                        "reservedModelType": "HashTag",
                        "reservedOwnerId": self._service.replication.owner_id,
                        "reservedAuthorId": self._service.replication.owner_id,
                        "reservedCreatedAt": utc_now_iso(),
                        "reservedUpdatedAt": utc_now_iso(),
                        "reservedSource": "web",
                    }
                )
                label_id_by_name["needs review"] = tmp_lbl.doc_id
            else:
                res_lbl = self._service.add_label("Needs Review", color="#ffa726")
                snap = self._service.cache.get_snapshot(force_refresh=True)
                label_id_by_name = {lbl.name.lower(): lbl.doc_id for lbl in snap.labels}
                label_id_by_name["needs review"] = res_lbl["id"]

        primary_acc = self._service.resolver.resolve_account(account_name)
        primary_curr = self._service.resolver.resolve_currency(primary_acc.currency_id)
        transfer_cat = self._service.resolver.resolve_transfer_category()

        # Map debt groups by row_index
        now_iso = utc_now_iso()
        row_to_debt_ref: dict[int, tuple[dict[str, Any], ...]] = {}
        debt_docs_to_put: list[dict[str, Any]] = []
        tx_by_row = {t.row_index: t for t in transactions}

        for dg in debt_groups:
            debt_id = f"-Debt_{uuid.uuid4()}"
            ref_tuple = ({"id": debt_id, "type": 1},)
            for r_idx in (*dg.lent_row_indices, *dg.repayment_row_indices):
                row_to_debt_ref[r_idx] = ref_tuple

            lent_dates = [tx_by_row[i].timestamp_iso for i in dg.lent_row_indices if i in tx_by_row]
            repay_dates = [tx_by_row[i].timestamp_iso for i in dg.repayment_row_indices if i in tx_by_row]
            debt_docs_to_put.append(
                {
                    "_id": debt_id,
                    "date": min(lent_dates) if lent_dates else now_iso,
                    "note": dg.reason,
                    "amount": dg.total_amount_minor,
                    "useDebtAmount": False,
                    "payBackTime": max(repay_dates) if repay_dates else now_iso,
                    "forgiven": False,
                    "reservedUpdatedAt": now_iso,
                    "type": 0,
                    "reservedOwnerId": self._service.replication.owner_id,
                    "accountId": primary_acc.doc_id,
                    "remainingAmount": 0 if dg.paid_back else dg.total_amount_minor,
                    "reservedSource": "android",
                    "reservedCreatedAt": now_iso,
                    "reservedAuthorId": self._service.replication.owner_id,
                    "name": dg.person_name,
                    "reservedModelType": "Debt",
                    "paidBack": dg.paid_back,
                }
            )

        docs_to_put: list[dict[str, Any]] = list(debt_docs_to_put)
        for txn in transactions:
            amt_major = (Decimal(txn.amount_minor) / Decimal(100)).quantize(Decimal("0.01"))
            p_type = _map_payment_type(txn.payment_method)
            resolved_labels = tuple(
                label_id_by_name[lbl.lower()]
                for lbl in txn.labels
                if lbl.lower() in label_id_by_name
            )
            debt_ref = row_to_debt_ref.get(txn.row_index, ())

            if txn.kind == TransactionKind.TRANSFER and txn.transfer_account:
                target_acc = self._service.resolver.resolve_account(txn.transfer_account)
                target_curr = self._service.resolver.resolve_currency(target_acc.currency_id)
                if txn.signed_delta_minor > 0:
                    from_acc, from_curr = target_acc, target_curr
                    to_acc, to_curr = primary_acc, primary_curr
                else:
                    from_acc, from_curr = primary_acc, primary_curr
                    to_acc, to_curr = target_acc, target_curr

                out_doc, in_doc = build_transfer_pair(
                    owner_id=self._service.replication.owner_id,
                    from_account=from_acc,
                    from_currency=from_curr,
                    to_account=to_acc,
                    to_currency=to_curr,
                    transfer_category=transfer_cat,
                    from_amount_major=amt_major,
                    payment_type=p_type,
                    record_state=RecordState.CLEARED,
                    note=txn.note,
                    record_date=txn.timestamp_iso,
                )
                if txn.payee or resolved_labels:
                    out_doc = replace(out_doc, payee=txn.payee, labels=resolved_labels)
                    in_doc = replace(in_doc, payee=txn.payee, labels=resolved_labels)

                d_out = out_doc.to_couch_dict()
                d_in = in_doc.to_couch_dict()
                RecordDocument.from_couch_dict(d_out)
                RecordDocument.from_couch_dict(d_in)
                docs_to_put.append(d_out)
                docs_to_put.append(d_in)
            else:
                cat_doc = self._service.resolver.resolve_category(txn.category)
                r_type = RecordType.INCOME if txn.signed_delta_minor > 0 else RecordType.EXPENSE
                rec_doc = build_record(
                    owner_id=self._service.replication.owner_id,
                    account=primary_acc,
                    currency=primary_curr,
                    category=cat_doc,
                    amount_major=amt_major,
                    record_type=r_type,
                    payment_type=p_type,
                    record_state=RecordState.CLEARED,
                    note=txn.note,
                    payee=txn.payee,
                    record_date=txn.timestamp_iso,
                    labels=resolved_labels,
                )
                if debt_ref:
                    rec_doc = replace(rec_doc, ref_objects=debt_ref)
                d_rec = rec_doc.to_couch_dict()
                RecordDocument.from_couch_dict(d_rec)
                docs_to_put.append(d_rec)

        if dry_run:
            return f"DRY_RUN_VALIDATED_{len(docs_to_put)}_DOCS"

        saved_after: list[dict[str, Any]] = []
        chunk_size = 250
        for idx in range(0, len(docs_to_put), chunk_size):
            chunk = docs_to_put[idx : idx + chunk_size]
            res_chunk = self._service.client.bulk_docs(chunk)
            for doc_dict, res_item in zip(chunk, res_chunk, strict=True):
                if "error" in res_item:
                    raise RuntimeError(f"CouchDB bulk_docs error for {doc_dict['_id']}: {res_item}")
                copy_doc = dict(doc_dict)
                copy_doc["_rev"] = res_item.get("rev")
                saved_after.append(copy_doc)

        entry = self._service.journal.record(
            operation=f"EXPENSIEVE_SYNC_{account_name.upper()}",
            before_docs=[],
            after_docs=saved_after,
        )
        live_after = self._compute_live_account_balance(primary_acc.doc_id)
        if live_after != expected_closing_minor:
            raise RuntimeError(
                f"Post-sync balance mismatch for {account_name}: "
                f"live={live_after}, expected={expected_closing_minor}"
            )
        return entry.entry_id
