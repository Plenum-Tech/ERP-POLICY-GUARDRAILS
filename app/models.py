"""
Pydantic models for the Policy & Risk Guardrails PoC.

These schemas are the single source of truth for:
- API request/response contracts (/policy/evaluate, etc.)
- Internal rule I/O (messages, flags, hard requirements)
- Serialization to audit/risk tables

Pydantic v2 compatible.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------
# Enums
# ---------------------------

class BlockStatus(str, Enum):
    PASS_ = "PASS"
    SOFT_BLOCK = "SOFT_BLOCK"
    HARD_BLOCK = "HARD_BLOCK"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# ---------------------------
# Transaction input
# ---------------------------

class TxInput(BaseModel):
    """
    Minimal transaction payload evaluated by the policy engine.

    UPDATED:
    - `date` is OPTIONAL at the model level because OCR may not provide it.
      Missing date is now handled as a policy HARD_BLOCK via required_fields
      instead of a Pydantic validation error.
    """

    # ⬇⬇⬇  THE IMPORTANT CHANGE  ⬇⬇⬇
    date: Optional[dt.date] = Field(
        default=None,
        description="Transaction date. Optional; PolicyEngine enforces via required_fields."
    )
    # ⬆⬆⬆  THE IMPORTANT CHANGE  ⬆⬆⬆

    amount: float = Field(..., ge=0.0, description="Transaction amount in the tx currency.")
    currency: str = Field(default="AED", min_length=3, max_length=10,
                          description="ISO currency code (e.g., AED, USD).")
    category_code: str = Field(..., min_length=1, description="Internal category code.")

    description: Optional[str] = Field(default=None, description="Free-text description.")
    attachments: List[str] = Field(default_factory=list,
                                   description="References to uploaded receipts or notes.")
    project_type: Optional[str] = Field(default=None,
                                       description="PO|RETAINER|ADHOC (drives PO/PCC rules).")
    tip_amount: Optional[float] = Field(default=0.0, ge=0.0,
                                        description="Tip value for tip policy.")
    mcc: Optional[str] = Field(default=None, description="Merchant Category Code.")
    fx_rate: Optional[float] = Field(default=None, gt=0.0,
                                     description="Applied FX rate.")
    payment_type: Optional[str] = Field(default=None,
                                        description="FOREX_CARD|PERSONAL|CORP_CARD")

    @field_validator("currency", mode="before")
    @classmethod
    def _norm_currency(cls, v: str) -> str:
        if not v:
            return "AED"
        v = str(v).upper().strip()
        if len(v) != 3:
            raise ValueError("currency must be a 3-letter ISO code")
        return v


class EvalRequest(BaseModel):
    """Request for /policy/evaluate."""
    org_id: str = Field(..., description="Organization identifier.")
    user_id: str = Field(..., description="End user (employee) identifier.")
    tx: TxInput = Field(..., description="Transaction details to evaluate.")
    context: Dict[str, Any] = Field(default_factory=dict,
                                    description="Optional context (team_id, project_id, etc.).")


# ---------------------------
# Engine outputs
# ---------------------------

class PolicyMessage(BaseModel):
    """Inline guidance rendered in UI."""
    severity: Severity = Field(..., description="info|warning|error.")
    code: str = Field(..., description="Stable reason code.")
    text: str = Field(..., description="User-friendly message.")
    suggested_actions: List[str] = Field(default_factory=list,
                                         description="split_expense|attach_manager_approval.")


class HardRequirement(BaseModel):
    """Requirement needed during HARD_BLOCK."""
    code: str
    label: str


class RiskFlag(BaseModel):
    """Atomic risk signal."""
    code: str
    weight: Optional[int] = Field(default=None)
    reason: str


class RiskPayload(BaseModel):
    score: float = Field(..., ge=0.0, le=100.0)
    flags: List[RiskFlag] = Field(default_factory=list)


class EvalResponse(BaseModel):
    """Response for /policy/evaluate."""
    status: BlockStatus
    messages: List[PolicyMessage] = Field(default_factory=list)
    hard_requirements: List[HardRequirement] = Field(default_factory=list)
    risk: RiskPayload


# ---------------------------
# Override flow (submit time)
# ---------------------------

class OverrideRequest(BaseModel):
    """
    Payload to finalize a submission that previously produced a SOFT/HARD block.
    """
    audit_id: str = Field(..., description="Audit row id.")
    justification: Optional[str] = Field(default=None)
    attachments: List[str] = Field(default_factory=list)
