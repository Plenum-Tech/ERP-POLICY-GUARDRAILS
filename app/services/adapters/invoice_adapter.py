# app/services/adapters/invoice_adapter.py
from __future__ import annotations

from typing import Any, Dict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.erp_models import invoice_docs
from app.schemas.policy_io import PolicyInput, TxPayload, PolicyContext


def _get_first_non_null(*values):
    for v in values:
        if v not in (None, "", "null"):
            return v
    return None


async def build_policy_input_from_invoice(
    db: AsyncSession,
    doc_id: UUID,
    org_id: str,
    user_id: str,
) -> PolicyInput:
    """
    Load an invoice_doc row and map its JSON `fields` + `items` into PolicyInput.

    Again:
      - Only use JSON columns (fields, items).
      - Map just enough into TxPayload.
      - Carry the full JSON in context.additional["raw_doc"].
    """

    stmt = (
        select(invoice_docs)
        .where(invoice_docs.c.id == doc_id)
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.fetchone()
    if not row:
        raise ValueError(f"Invoice not found for id={doc_id}")

    row_dict: Dict[str, Any] = dict(row._mapping)

    fields_json: Dict[str, Any] = row_dict.get("fields") or {}
    items_json = row_dict.get("items")

    tx_date = _get_first_non_null(
        fields_json.get("invoice_date"),
        fields_json.get("transaction_date"),
    )

    amount = _get_first_non_null(
        fields_json.get("invoice_total"),
        fields_json.get("amount_due"),
        fields_json.get("total"),
        0.0,
    )
    currency = fields_json.get("currency") or "AED"

    category_code = (
        fields_json.get("category_code")
        or fields_json.get("category")
        or "INVOICE"
    )

    description = (
        fields_json.get("description")
        or fields_json.get("invoice_id")
        or row_dict.get("source_filename")
        or ""
    )

    attachments: list[str] = []

    tx = TxPayload(
        date=tx_date,
        amount=float(amount),
        currency=str(currency),
        category_code=str(category_code),
        description=str(description),
        attachments=attachments,
        project_type=str(fields_json.get("project_type") or ""),
        tip_amount=0.0,
        mcc=str(fields_json.get("mcc") or ""),
        fx_rate=float(fields_json.get("fx_rate") or 1.0),
        payment_type=str(fields_json.get("payment_method") or ""),
    )

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
