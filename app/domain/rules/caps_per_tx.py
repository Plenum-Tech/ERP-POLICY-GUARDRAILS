"""
Rule: Per-transaction cap (general, non-weekend specific)

Policy intent (PoC):
- Each category may have a per-transaction limit (e.g., ENTERTAINMENT ≤ AED 1500).
- On weekends, a separate weekend cap may apply (handled by weekend.py).
  This rule focuses on the general per-tx cap and avoids double-messaging
  if a weekend-specific cap exists.

Rule pack knobs:
{
  "caps": {
    "per_tx": { "ENTERTAINMENT": 1500 },
    "per_tx_weekend": { "ENTERTAINMENT": 500 }   // optional; handled by weekend.py
  }
}

Behavior:
- Resolve category cap from caps.per_tx (case-insensitive).
- If it's a weekend AND a weekend override exists for this category,
  skip here (weekend.py will handle weekend-specific logic).
- If amount > cap → SOFT_BLOCK with guidance; Risk flag: CAP_OVER_TX.

Safety:
- If tx.date is missing => weekend-specific override check is skipped (treated as weekday).
- If category is missing or empty => rule skips.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from app.models import PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(code: str, text: str, severity: Severity = Severity.WARNING) -> PolicyMessage:
    return PolicyMessage(
        severity=severity,
        code=code,
        text=text,
        suggested_actions=["split_expense", "add_justification"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _norm_category(value: Optional[str]) -> Optional[str]:
    """
    Normalize category strings for case-insensitive matching.
    Returns lowercase or None.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s.lower()


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Safe conversion to Decimal; returns None on failure."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _cap_for_category(rp: Dict[str, Any], category: str) -> Optional[Decimal]:
    """
    Resolve general per-transaction cap from rulepack.caps.per_tx
    using case-insensitive category matching.
    """
    norm_cat = _norm_category(category)
    if not norm_cat:
        return None

    caps = (rp.get("caps") or {})
    per_tx_raw = (caps.get("per_tx") or {})

    # Normalize keys to lowercase for case-insensitive lookups
    per_tx = {
        _norm_category(k): v
        for k, v in per_tx_raw.items()
        if _norm_category(k)
    }

    if norm_cat not in per_tx:
        return None

    return _to_decimal(per_tx.get(norm_cat))


def _has_weekend_override(rp: Dict[str, Any], category: str) -> bool:
    """
    Check if there is a weekend override for this category in caps.per_tx_weekend
    (case-insensitive).
    """
    norm_cat = _norm_category(category)
    if not norm_cat:
        return False

    caps = (rp.get("caps") or {})
    wk_raw = (caps.get("per_tx_weekend") or {})

    wk_caps = {
        _norm_category(k): v
        for k, v in wk_raw.items()
        if _norm_category(k)
    }

    return norm_cat in wk_caps


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

async def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    """
    General per-transaction caps:

    - Skip if category is missing.
    - If weekend + weekend override exists for category, skip (weekend.py handles it).
    - If amount <= cap → no-op.
    - If amount > cap → SOFT_BLOCK + CAP_OVER_TX risk flag.
    """
    tx = req.tx

    category = getattr(tx, "category_code", None)
    if not category:
        # No category → cannot apply category caps
        return None

    cap = _cap_for_category(rulepack, category)
    if cap is None:
        # No general cap configured for this category
        return None

    # Weekend override check: only if we have a date
    is_weekend = False
    if getattr(tx, "date", None) is not None:
        try:
            is_weekend = await repos.is_weekend(
                tx.date,
                (rulepack.get("weekend") or {}),
            )
        except Exception:
            # If weekend detection fails, treat as non-weekend for this rule.
            is_weekend = False

    # If it's weekend AND a weekend override exists for this category,
    # let weekend.py handle it to avoid duplicate messages.
    if is_weekend and _has_weekend_override(rulepack, category):
        return None

    amount = _to_decimal(getattr(tx, "amount", 0) or 0)
    if amount is None or amount <= cap:
        return None

    over = amount - cap
    text = (
        f"Per-transaction cap for '{category}' is {cap:.2f}; "
        f"you're over by {over:.2f}. "
        "Consider splitting the expense or adding a short justification."
    )

    return RuleOutcome(
        status_delta="SOFT_BLOCK",
        messages=[_msg("CAP_OVER_TX", text)],
        flags=[
            _flag(
                "CAP_OVER_TX",
                f"amount={amount:.2f} > cap={cap:.2f}",
            )
        ],
        hard_requirements=[],
    )
