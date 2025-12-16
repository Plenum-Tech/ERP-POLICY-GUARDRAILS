# app/services/policy/input_builder.py
from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.policy_io import PolicyInput
from app.services.adapters.receipt_adapter import build_policy_input_from_receipt
from app.services.adapters.invoice_adapter import build_policy_input_from_invoice

SourceType = Literal["receipt", "invoice"]


async def build_policy_input_from_source(
    db: AsyncSession,
    source: SourceType,
    doc_id: str,
    org_id: str,
    user_id: str,
) -> PolicyInput:
    """
    Thin dispatcher that:
      - Normalises doc_id to UUID
      - Routes to the correct adapter (receipt / invoice)
      - Ensures org_id + user_id are always set on PolicyInput
    """
    try:
        doc_uuid = UUID(doc_id)
    except Exception as e:
        raise ValueError(f"Invalid doc_id (expected UUID string): {doc_id}") from e

    if source == "receipt":
        return await build_policy_input_from_receipt(
            db=db,
            doc_id=doc_uuid,
            org_id=org_id,
            user_id=user_id,
        )
    elif source == "invoice":
        return await build_policy_input_from_invoice(
            db=db,
            doc_id=doc_uuid,
            org_id=org_id,
            user_id=user_id,
        )
    else:
        raise ValueError(f"Unsupported source type: {source}")
