# app/domain/schemas/policy_io.py
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator


class TxPayload(BaseModel):
    """
    Canonical transaction payload for policy evaluation.

    Note:
    - `date` is optional at the model level so that OCR / upstream
      gaps do NOT cause validation failures. The PolicyEngine will
      enforce `date` via rulepack.required_fields and turn it into a
      proper HARD_BLOCK if missing.
    """
    date: Optional[date] = None
    amount: float
    currency: str
    category_code: str
    description: str = ""
    attachments: List[str] = []
    project_type: str = ""
    tip_amount: float = 0.0
    mcc: str = ""
    fx_rate: float = 1.0
    payment_type: str = ""

    @field_validator("date", mode="before")
    @classmethod
    def _parse_date(cls, v):
        # Allow None / empty string, PolicyEngine will treat it as "missing"
        if v in (None, "", "null"):
            return None
        return v


class PolicyContext(BaseModel):
    # Free-form extra info (raw OCR, vendor, project, etc.)
    additional: Dict[str, Any] = {}


class PolicyInput(BaseModel):
    """
    High-level wrapper for policy calls from other services.

    This gets converted into EvalRequest (engine model) by the adapter.
    """
    org_id: str = ""
    user_id: str = ""
    tx: TxPayload
    context: PolicyContext = PolicyContext()
