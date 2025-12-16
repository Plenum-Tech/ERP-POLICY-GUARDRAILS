"""
Rule: Forex card usage constraints

Policy intent (PoC):
- Company forex cards may be used only for specific categories (e.g., TAXI, F&B).
- Any other usage is a policy breach and must be blocked until justified/approved.

Rule pack knobs:
{
  "forex_card": {
    "enabled": true,
    "allowed_categories": ["TAXI", "F&B"]
  }
}

Behavior:
- If forex_card.enabled is false → no-op.
- If tx.payment_type != "FOREX_CARD" (case-insensitive) → no-op.
- If category_code missing → no-op (let required_fields handle).
- If normalized(category) NOT in normalized(allowed_categories)
    → HARD_BLOCK + risk flag FOREX_MISUSE
      + hard requirement REQUIRE_MANAGER_NOTE.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.models import HardRequirement, PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(rulepack: Dict[str, Any]) -> Dict[str, Any]:
    """Extract forex card config safely from rulepack."""
    if not isinstance(rulepack, dict):
        return {}
    return rulepack.get("forex_card") or {}


def _msg(code: str, text: str, severity: Severity = Severity.ERROR) -> PolicyMessage:
    return PolicyMessage(
        severity=severity,
        code=code,
        text=text,
        suggested_actions=["attach_manager_approval", "use_personal_card_and_claim"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _hard(code: str, label: str) -> HardRequirement:
    return HardRequirement(code=code, label=label)


def _norm_str(value: Optional[str]) -> Optional[str]:
    """Normalize strings for case-insensitive matching."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s.upper()


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    cfg = _cfg(rulepack)

    # If explicitly disabled, skip
    if cfg.get("enabled", True) is False:
        return None

    tx = getattr(req, "tx", None)
    if tx is None:
        return None

    # SAFETY: payment_type missing → no-op
    payment_type = _norm_str(getattr(tx, "payment_type", None))
    if payment_type != "FOREX_CARD":
        # Only care about actual forex card usage
        return None

    # SAFETY: category missing → let required_fields rule handle
    category_raw = getattr(tx, "category_code", None)
    if category_raw is None:
        return None

    category_norm = _norm_str(category_raw)
    if category_norm is None:
        return None

    # Build allowlist (case-insensitive)
    allowed_raw = cfg.get("allowed_categories") or []
    allowed_norm = {
        _norm_str(c) for c in allowed_raw if _norm_str(c) is not None
    }

    if not allowed_norm:
        # No allowlist configured → treat as pass-through in PoC
        return None

    if category_norm not in allowed_norm:
        # Category not allowed for forex card usage
        allowed_str = ", ".join(sorted(allowed_raw)) or "—"

        return RuleOutcome(
            status_delta="HARD_BLOCK",
            messages=[
                _msg(
                    "FOREX_MISUSE",
                    (
                        f"Forex card usage is restricted to: {allowed_str}. "
                        f"Category '{category_raw}' is not permitted."
                    ),
                )
            ],
            flags=[
                _flag(
                    "FOREX_MISUSE",
                    (
                        f"Used forex card for category '{category_raw}' "
                        f"(allowed: {list(allowed_raw)})"
                    ),
                )
            ],
            hard_requirements=[
                _hard(
                    "REQUIRE_MANAGER_NOTE",
                    "Attach Finance/Manager approval or repay and resubmit",
                )
            ],
        )

    # Category is within allowlist → policy satisfied
    return None
