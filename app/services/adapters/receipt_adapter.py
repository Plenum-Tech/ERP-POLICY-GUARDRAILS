# app/services/adapters/receipt_adapter.py
from __future__ import annotations

from typing import Any, Dict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.erp_models import receipt_docs
from app.schemas.policy_io import PolicyInput, TxPayload, PolicyContext


def _get_first_non_null(*values):
    for v in values:
        if v not in (None, "", "null"):
            return v
    return None


async def build_policy_input_from_receipt(
    db: AsyncSession,
    doc_id: UUID,
    org_id: str,
    user_id: str,
) -> PolicyInput:
    """
    Load a receipt_doc row and map its JSON `fields` and `items` into PolicyInput.

    We:
      - Use ONLY the JSON columns: fields (header) + items (lines)
      - Map a minimal set into TxPayload (date, amount, currency, category_code, etc.)
      - Put the full JSON into context.additional["raw_doc"] for downstream rules.
    """

    stmt = (
        select(receipt_docs)
        .where(receipt_docs.c.id == doc_id)
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.fetchone()
    if not row:
        raise ValueError(f"Receipt not found for id={doc_id}")

    row_dict: Dict[str, Any] = dict(row._mapping)

    # JSON blobs from the table
    fields_json: Dict[str, Any] = row_dict.get("fields") or {}
    items_json = row_dict.get("items")

    # --- Minimal canonical mapping into TxPayload ----------------------

    # date: optional; PolicyEngine will enforce via required_fields
    tx_date = fields_json.get("transaction_date")

    # amount / currency
    amount = _get_first_non_null(
        fields_json.get("total"),
        fields_json.get("amount"),
        0.0,
    )
    currency = fields_json.get("currency") or "AED"

    # category_code: if your extractor doesn’t set it, we can leave a default
    category_code = (
        fields_json.get("category_code")
        or fields_json.get("category")
        or "ENTERTAINMENT"
    )

    description = (
        fields_json.get("description")
        or fields_json.get("merchant_name")
        or row_dict.get("source_filename")
        or ""
    )

    tip_amount = _get_first_non_null(
        fields_json.get("tip"),
        fields_json.get("tip_amount"),
        0.0,
    )

    mcc = fields_json.get("mcc") or ""
    fx_rate = float(fields_json.get("fx_rate") or 1.0)
    payment_type = (
        fields_json.get("payment_method")
        or fields_json.get("payment_type")
        or ""
    )

    # Attachments list will usually come from the outer system; from-doc is
    # more like "policy-only" test, so we leave it empty here.
    attachments: list[str] = []

    tx = TxPayload(
        date=tx_date,
        amount=float(amount),
        currency=str(currency),
        category_code=str(category_code),
        description=str(description),
        attachments=attachments,
        project_type=str(fields_json.get("project_type") or ""),
        tip_amount=float(tip_amount),
        mcc=str(mcc),
        fx_rate=fx_rate,
        payment_type=str(payment_type),
    )

    # --- Context carries full JSON for later rules / debugging ----------

    ctx = PolicyContext(
        additional={
            "raw_doc": {
                "header": fields_json,
                "items": items_json,
                "model_id": row_dict.get("model_id"),
                "source_filename": row_dict.get("source_filename"),
            }
        }
    )

    return PolicyInput(
        org_id=org_id,
        user_id=user_id,
        tx=tx,
        context=ctx,
    )
