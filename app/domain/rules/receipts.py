"""
Rule: Receipt/Invoice evidence required

Policy intent (PoC):
- Every expense must include at least one valid receipt/invoice attachment.
- Configurable via rule pack, but defaults to "required for all" if not present.

Rule pack knobs (optional):
{
  "receipts": {
    "enabled": true,
    "required_for_all": true,
    "min_count": 1,
    "required_for_categories": ["Entertainment","Taxi"]   # optional list (used only when required_for_all=false)
  }
}

Interaction with required_fields:
- If rulepack.required_fields contains "attachments" AND there are zero attachments AND min_count == 1,
  the builtin required_fields rule will already emit MISSING_RECEIPT.
  In that base scenario we SKIP here to avoid duplicate messages.
- This rule still enforces:
    - min_count > 1
    - category scoping
    - situations where some attachments exist but are fewer than min_count.

Behavior:
- If receipts.enabled is false → no-op.
- If required_for_all=true → require >= min_count attachments for every tx.
- Else → require attachments only for categories in required_for_categories (case-insensitive).
- Outcome: HARD_BLOCK with a clear message and a concrete HardRequirement.
- Risk flag: MISSING_RECEIPT
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.models import HardRequirement, PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(rulepack: Dict[str, Any]) -> Dict[str, Any]:
    return (rulepack.get("receipts") or {}) if isinstance(rulepack, dict) else {}


def _msg(code: str, text: str, severity: Severity = Severity.ERROR) -> PolicyMessage:
    return PolicyMessage(
        severity=severity,
        code=code,
        text=text,
        suggested_actions=["attach_receipt"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _hard(code: str, label: str) -> HardRequirement:
    return HardRequirement(code=code, label=label)


def _norm_category(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s.lower()


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    tx = req.tx
    cfg = _cfg(rulepack)

    # Global toggle: if disabled, do nothing
    if cfg.get("enabled", True) is False:
        return None

    # Required-for-all vs category-scoped
    required_for_all = bool(cfg.get("required_for_all", True))
    required_list_raw = cfg.get("required_for_categories") or []
    required_list = { _norm_category(c) for c in required_list_raw if _norm_category(c) }

    tx_cat_norm = _norm_category(getattr(tx, "category_code", None))

    # Decide if this tx needs receipts at all
    if required_for_all:
        needs_receipt = True
    else:
        needs_receipt = bool(tx_cat_norm and tx_cat_norm in required_list)

    if not needs_receipt:
        return None

    # Configured minimum attachments (default 1)
    try:
        min_count = int(cfg.get("min_count", 1))
    except Exception:
        min_count = 1

    # Attachments from tx (list or None)
    attachments = getattr(tx, "attachments", None)
    if attachments is None:
        attachments = []

    # -----------------------------------------------------------------------
    # De-duplication with builtin required_fields rule
    # -----------------------------------------------------------------------
    # If "attachments" is part of required_fields AND we have *zero* attachments
    # AND min_count == 1, then the builtin required_fields logic will already:
    #   - mark REQUIRED_FIELD_MISSING
    #   - add MISSING_RECEIPT flag/requirement
    # To avoid duplicate messages, we SKIP here in that base case.
    required_fields = (rulepack.get("required_fields") or []) if isinstance(rulepack, dict) else []
    if "attachments" in required_fields and not attachments and min_count <= 1:
        return None

    # -----------------------------------------------------------------------
    # Core enforcement: not enough receipts
    # -----------------------------------------------------------------------
    if len(attachments) >= min_count:
        # Sufficient evidence → no outcome
        return None

    # Not enough evidence → HARD block + risk flag + explicit requirement
    return RuleOutcome(
        status_delta="HARD_BLOCK",
        messages=[
            _msg(
                "MISSING_RECEIPT",
                f"Receipt/invoice is required (min {min_count}). Please attach a valid document."
            )
        ],
        flags=[_flag("MISSING_RECEIPT", f"No or insufficient receipts; required min={min_count}, got={len(attachments)}")],
        hard_requirements=[_hard("REQUIRE_RECEIPT", "Attach valid receipt/invoice")],
    )
