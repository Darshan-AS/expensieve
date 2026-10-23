"""Wallet by BudgetBakers CouchDB LedgerSink adapter powered by `sourdough`."""

import sys
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from expensieve.domain.models import (
    CanonicalTransaction,
    DebtLinkGroup,
    PaymentMethod,
    TransactionKind,
)


def _load_sourdough() -> Any:
    """Dynamically load the sibling `sourdough` SDK so ExpenSieve remains standalone in CI."""
    try:
        import sourdough  # noqa: F401
    except ImportError:
        for cand_src in (
            Path.cwd().parent / "sourdough" / "src",
            Path(__file__).resolve().parents[5] / "sourdough" / "src",
        ):
            if cand_src.is_dir() and str(cand_src) not in sys.path:
                sys.path.insert(0, str(cand_src))
                break

    from sourdough.config import resolve_paths
    from sourdough.domain.builders import (
        build_record,
        build_transfer_pair,
        normalize_timestamp_input,
        utc_now_iso,
    )
    from sourdough.domain.enums import PaymentType, RecordState, RecordType
    from sourdough.domain.models import CurrencyDocument, LabelDocument, RecordDocument
    from sourdough.services.records import WalletService

    return {
        "resolve_paths": resolve_paths,
        "build_record": build_record,
        "build_transfer_pair": build_transfer_pair,
        "normalize_timestamp_input": normalize_timestamp_input,
        "utc_now_iso": utc_now_iso,
        "PaymentType": PaymentType,
        "RecordState": RecordState,
        "RecordType": RecordType,
        "CurrencyDocument": CurrencyDocument,
        "LabelDocument": LabelDocument,
        "RecordDocument": RecordDocument,
        "WalletService": WalletService,
    }


def _map_payment_type(method: PaymentMethod, payment_type_enum: Any) -> Any:
    if method == PaymentMethod.DEBIT_CARD:
        return payment_type_enum.DEBIT_CARD
    if method == PaymentMethod.CREDIT_CARD:
        return payment_type_enum.CREDIT_CARD
    if method == PaymentMethod.CASH:
        return payment_type_enum.CASH
    return payment_type_enum.MOBILE_PAYMENT


class SourdoughLedgerSink:
    """Pushes CanonicalTransactions and DebtLinkGroups to Wallet by BudgetBakers via sourdough."""

    name = "sourdough_wallet"

    def __init__(self, service: Any | None = None) -> None:
        sd = _load_sourdough()
        self._sd = sd
        if service is not None:
            self._service: Any = service
        else:
            explicit_state: str | None = None
            if not (Path.cwd() / ".wallet_state.json").exists():
                for cand_state in (
                    Path.cwd().parent / "sourdough" / ".wallet_state.json",
                    Path(__file__).resolve().parents[5] / "sourdough" / ".wallet_state.json",
                ):
                    if cand_state.is_file():
                        explicit_state = str(cand_state)
                        break
            self._service = sd["WalletService"](paths=sd["resolve_paths"](explicit_state))

    def _compute_live_account_balance(self, account_id: str) -> int:
        snap = self._service.cache.get_snapshot()
        acc = snap.accounts_by_id.get(account_id)
        acc_curr_id = acc.currency_id if acc is not None else None
        all_raw = self._service.client.all_docs_by_prefix("Record_")
        total_minor = 0
        for r in all_raw:
            if (
                r.get("reservedModelType") == "Record"
                and not r.get("_deleted")
                and r.get("accountId") == account_id
            ):
                rec_curr_id = r.get("currencyId")
                if acc_curr_id and rec_curr_id and rec_curr_id != acc_curr_id:
                    amt = int(r.get("refAmount", r.get("amount", 0)))
                else:
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

    @staticmethod
    def _find_existing_non_transfer_match(
        *,
        existing_raw_records: Sequence[dict[str, Any]],
        consumed_record_ids: set[str],
        account_id: str,
        record_type: int,
        amount_minor: int,
        timestamp_iso: str,
    ) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
        """Find an existing 1-to-1 record or 1-to-N split group on account_id within ±1 day."""
        try:
            target_dt = date.fromisoformat(timestamp_iso[:10])
        except ValueError:
            return (None, ())

        exact_candidates: list[tuple[int, dict[str, Any]]] = []
        by_date: dict[str, list[dict[str, Any]]] = {}

        for raw in existing_raw_records:
            rec_id = str(raw.get("_id", ""))
            if (
                not rec_id
                or rec_id in consumed_record_ids
                or raw.get("_deleted")
                or raw.get("reservedModelType") != "Record"
                or raw.get("transfer")
                or raw.get("accountId") != account_id
                or int(raw.get("type", 1)) != record_type
            ):
                continue

            rec_dt_str = str(raw.get("recordDate", ""))[:10]
            try:
                rec_dt = date.fromisoformat(rec_dt_str)
            except ValueError:
                continue

            day_diff = abs((rec_dt - target_dt).days)
            if day_diff > 1:
                continue

            rec_amt = int(raw.get("amount", 0))
            rec_ref = int(raw.get("refAmount", rec_amt))
            if rec_amt == amount_minor or rec_ref == amount_minor:
                exact_candidates.append((day_diff, raw))
            by_date.setdefault(rec_dt_str, []).append(raw)

        if exact_candidates:
            exact_candidates.sort(key=lambda pair: (pair[0], str(pair[1].get("recordDate", ""))))
            return (exact_candidates[0][1], ())

        for group in by_date.values():
            if len(group) >= 2 and (
                sum(int(r.get("amount", 0)) for r in group) == amount_minor
                or sum(int(r.get("refAmount", r.get("amount", 0))) for r in group) == amount_minor
            ):
                return (None, tuple(str(r["_id"]) for r in group))

        return (None, ())

    def push_transactions(
        self,
        account_name: str,
        transactions: Sequence[CanonicalTransaction],
        debt_groups: Sequence[DebtLinkGroup],
        expected_closing_minor: int,
        dry_run: bool = False,
    ) -> str:
        """Validate all documents, verify closing balance math, and bulk-push to CouchDB."""
        CurrencyDocument = self._sd["CurrencyDocument"]
        LabelDocument = self._sd["LabelDocument"]
        RecordDocument = self._sd["RecordDocument"]
        PaymentType = self._sd["PaymentType"]
        RecordState = self._sd["RecordState"]
        RecordType = self._sd["RecordType"]
        build_record = self._sd["build_record"]
        build_transfer_pair = self._sd["build_transfer_pair"]
        utc_now_iso = self._sd["utc_now_iso"]

        if dry_run:
            self._service.cache.ttl_seconds = 10**9

        snap = self._service.cache.get_snapshot(force_refresh=not dry_run)
        label_id_by_name = {lbl.name.lower(): lbl.doc_id for lbl in snap.labels}
        currency_by_code = {c.code.upper(): c for c in snap.currencies}
        default_fx_ratios = {
            "SGD": 1.0 / 75.29,
            "MYR": 1.0 / 23.62,
            "USD": 1.0 / 84.00,
            "EUR": 1.0 / 91.00,
        }
        needed_fcy_codes = sorted(
            {
                txn.foreign_currency.upper()
                for txn in transactions
                if txn.foreign_currency and txn.foreign_amount_minor
            }
        )
        for idx_c, fcy_code in enumerate(needed_fcy_codes):
            if fcy_code not in currency_by_code:
                ratio = default_fx_ratios.get(fcy_code, 0.01)
                if dry_run or not hasattr(self._service, "add_currency"):
                    currency_by_code[fcy_code] = CurrencyDocument(
                        doc_id=f"-Currency_{uuid.uuid4()}",
                        code=fcy_code,
                        referential=False,
                        ratio_to_referential=ratio,
                    )
                else:
                    self._service.add_currency(fcy_code, ratio, position=3000 + idx_c * 1000)
                    snap = self._service.cache.get_snapshot(force_refresh=True)
                    currency_by_code = {c.code.upper(): c for c in snap.currencies}

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
        normalize_timestamp_input = self._sd["normalize_timestamp_input"]

        cached_recs = (
            self._service.cache.get_cached_records()
            if dry_run and hasattr(self._service.cache, "get_cached_records")
            else None
        )
        existing_raw_records = (
            cached_recs
            if cached_recs is not None
            else self._service.client.all_docs_by_prefix("Record_")
        )
        consumed_record_ids: set[str] = set()
        before_docs: list[dict[str, Any]] = []

        # Map debt groups by row_index (reusing open -Debt_ docs when available)
        now_iso = str(utc_now_iso())
        row_to_debt_ref: dict[int, tuple[dict[str, Any], ...]] = {}
        debt_docs_to_put: list[dict[str, Any]] = []
        tx_by_row = {t.row_index: t for t in transactions}

        for dg in debt_groups:
            existing_debt = (
                self._service.find_open_debt(dg.person_name)
                if hasattr(self._service, "find_open_debt")
                else None
            )
            if existing_debt is not None:
                debt_id = str(existing_debt["_id"])
                before_docs.append(dict(existing_debt))
                updated_debt = dict(existing_debt)
                updated_debt["paidBack"] = dg.paid_back
                updated_debt["remainingAmount"] = (
                    0 if dg.paid_back else max(0, int(existing_debt.get("remainingAmount", 0)))
                )
                updated_debt["reservedUpdatedAt"] = now_iso
                debt_docs_to_put.append(updated_debt)
            else:
                debt_id = f"-Debt_{uuid.uuid4()}"
                lent_dates = [
                    tx_by_row[i].timestamp_iso for i in dg.lent_row_indices if i in tx_by_row
                ]
                repay_dates = [
                    tx_by_row[i].timestamp_iso for i in dg.repayment_row_indices if i in tx_by_row
                ]
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
            ref_tuple = ({"id": debt_id, "type": 1},)
            for r_idx in (*dg.lent_row_indices, *dg.repayment_row_indices):
                row_to_debt_ref[r_idx] = ref_tuple

        docs_to_put: list[dict[str, Any]] = list(debt_docs_to_put)
        resolve_cat_fn = getattr(
            self._service.resolver,
            "resolve_leaf_category",
            self._service.resolver.resolve_category,
        )

        for txn in transactions:
            amt_major = (Decimal(txn.amount_minor) / Decimal(100)).quantize(Decimal("0.01"))
            p_type = _map_payment_type(txn.payment_method, PaymentType)
            resolved_labels = tuple(
                label_id_by_name[lbl.lower()] for lbl in txn.labels if lbl.lower() in label_id_by_name
            )
            debt_ref = row_to_debt_ref.get(txn.row_index, ())
            if (
                not debt_ref
                and txn.category in ("Loan and Debts", "Lending, renting")
                and hasattr(self._service, "find_open_debt")
            ):
                open_d = self._service.find_open_debt(txn.payee or txn.note)
                if open_d is not None:
                    debt_ref = ({"id": str(open_d["_id"]), "type": 1},)

            if txn.kind == TransactionKind.TRANSFER and txn.transfer_account:
                target_acc = self._service.resolver.resolve_account(txn.transfer_account)
                target_curr = self._service.resolver.resolve_currency(target_acc.currency_id)
                if txn.signed_delta_minor > 0:
                    from_acc, from_curr = target_acc, target_curr
                    to_acc, to_curr = primary_acc, primary_curr
                else:
                    from_acc, from_curr = primary_acc, primary_curr
                    to_acc, to_curr = target_acc, target_curr

                unconsumed_raw = [
                    r for r in existing_raw_records if str(r.get("_id", "")) not in consumed_record_ids
                ]
                matched_pair = (
                    self._service.find_matching_transfer(
                        from_account_id=from_acc.doc_id,
                        to_account_id=to_acc.doc_id,
                        amount_minor=txn.amount_minor,
                        date_iso=txn.timestamp_iso,
                        tolerance_days=2,
                        raw_records=unconsumed_raw,
                    )
                    if hasattr(self._service, "find_matching_transfer")
                    else None
                )
                rehomed_to_primary = False
                if (
                    matched_pair is None
                    and account_name.lower() == "niyox forex"
                    and hasattr(self._service, "find_matching_transfer")
                ):
                    try:
                        legacy_niyo = self._service.resolver.resolve_account("NiyoX")
                        legacy_from = legacy_niyo.doc_id if from_acc.doc_id == primary_acc.doc_id else from_acc.doc_id
                        legacy_to = legacy_niyo.doc_id if to_acc.doc_id == primary_acc.doc_id else to_acc.doc_id
                        matched_pair = self._service.find_matching_transfer(
                            from_account_id=legacy_from,
                            to_account_id=legacy_to,
                            amount_minor=txn.amount_minor,
                            date_iso=txn.timestamp_iso,
                            tolerance_days=2,
                            raw_records=unconsumed_raw,
                        )
                        if matched_pair is not None:
                            rehomed_to_primary = True
                    except LookupError, ValueError, RuntimeError:
                        matched_pair = None

                if matched_pair is not None:
                    out_raw, in_raw = matched_pair
                    consumed_record_ids.add(str(out_raw["_id"]))
                    consumed_record_ids.add(str(in_raw["_id"]))
                    out_existing = RecordDocument.from_couch_dict(out_raw)
                    in_existing = RecordDocument.from_couch_dict(in_raw)
                    need_update = (
                        rehomed_to_primary
                        or (txn.note and not out_existing.note)
                        or (txn.payee and not out_existing.payee)
                        or (resolved_labels and not out_existing.labels)
                    )
                    if need_update:
                        before_docs.extend([dict(out_raw), dict(in_raw)])
                        upd_out = replace(
                            out_existing,
                            account_id=from_acc.doc_id,
                            transfer_account_id=to_acc.doc_id,
                            note=txn.note if rehomed_to_primary and txn.note else (out_existing.note or txn.note),
                            payee=txn.payee if rehomed_to_primary and txn.payee else (out_existing.payee or txn.payee),
                            labels=out_existing.labels or resolved_labels,
                            reserved_updated_at=now_iso,
                        ).to_couch_dict()
                        upd_in = replace(
                            in_existing,
                            account_id=to_acc.doc_id,
                            transfer_account_id=from_acc.doc_id,
                            note=txn.note if rehomed_to_primary and txn.note else (in_existing.note or txn.note),
                            payee=txn.payee if rehomed_to_primary and txn.payee else (in_existing.payee or txn.payee),
                            labels=in_existing.labels or resolved_labels,
                            reserved_updated_at=now_iso,
                        ).to_couch_dict()
                        docs_to_put.extend([upd_out, upd_in])
                    continue

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
                try:
                    cat_doc = resolve_cat_fn(txn.category)
                except LookupError:
                    if txn.category.lower() == "refunds and cashbacks":
                        cat_doc = resolve_cat_fn("Refunds (tax, purchase)")
                    else:
                        raise
                r_type = RecordType.INCOME if txn.signed_delta_minor > 0 else RecordType.EXPENSE

                existing_match, split_ids = self._find_existing_non_transfer_match(
                    existing_raw_records=existing_raw_records,
                    consumed_record_ids=consumed_record_ids,
                    account_id=primary_acc.doc_id,
                    record_type=int(r_type),
                    amount_minor=txn.amount_minor,
                    timestamp_iso=txn.timestamp_iso,
                )
                if split_ids:
                    consumed_record_ids.update(split_ids)
                    continue
                if existing_match is not None:
                    consumed_record_ids.add(str(existing_match["_id"]))
                    existing_rec = RecordDocument.from_couch_dict(existing_match)
                    norm_ts = normalize_timestamp_input(txn.timestamp_iso)
                    has_better_ts = (
                        existing_rec.record_date.endswith("T00:00:00.000Z")
                        and not norm_ts.endswith("T00:00:00.000Z")
                    )
                    is_debt_linked = bool(existing_rec.ref_objects)
                    upgrade_cat = not is_debt_linked and existing_rec.category_id != cat_doc.doc_id
                    upgrade_note = bool(txn.note) and (
                        not existing_rec.note
                        or (
                            not is_debt_linked
                            and existing_rec.note.strip().lower() == "credit interest capitalised"
                            and txn.note != existing_rec.note
                        )
                    )
                    upgrade_payee = bool(txn.payee) and (
                        not existing_rec.payee
                        or (
                            not is_debt_linked
                            and existing_rec.payee.strip().lower() == "niyox"
                            and txn.payee != existing_rec.payee
                        )
                    )
                    need_rec_upd = (
                        has_better_ts
                        or upgrade_cat
                        or upgrade_note
                        or upgrade_payee
                        or (debt_ref and not existing_rec.ref_objects)
                    )
                    if need_rec_upd:
                        before_docs.append(dict(existing_match))
                        upd_rec = replace(
                            existing_rec,
                            record_date=norm_ts if has_better_ts else existing_rec.record_date,
                            category_id=cat_doc.doc_id if upgrade_cat else existing_rec.category_id,
                            note=txn.note if upgrade_note else (existing_rec.note or txn.note),
                            payee=txn.payee if upgrade_payee else (existing_rec.payee or txn.payee),
                            ref_objects=existing_rec.ref_objects or debt_ref,
                            reserved_updated_at=now_iso,
                        ).to_couch_dict()
                        docs_to_put.append(upd_rec)
                    continue

                rec_curr = primary_curr
                rec_amt_major = amt_major
                rec_ref_major: Decimal | None = None
                if (
                    txn.foreign_currency
                    and txn.foreign_amount_minor
                    and txn.foreign_currency.upper() in currency_by_code
                ):
                    rec_curr = currency_by_code[txn.foreign_currency.upper()]
                    rec_amt_major = (Decimal(txn.foreign_amount_minor) / Decimal(100)).quantize(
                        Decimal("0.01")
                    )
                    rec_ref_major = amt_major

                rec_doc = build_record(
                    owner_id=self._service.replication.owner_id,
                    account=primary_acc,
                    currency=rec_curr,
                    category=cat_doc,
                    amount_major=rec_amt_major,
                    ref_amount_major=rec_ref_major,
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
            before_docs=before_docs,
            after_docs=saved_after,
        )
        live_after = self._compute_live_account_balance(primary_acc.doc_id)
        if live_after != expected_closing_minor:
            raise RuntimeError(
                f"Post-sync balance mismatch for {account_name}: "
                f"live={live_after}, expected={expected_closing_minor}"
            )
        return str(entry.entry_id)
