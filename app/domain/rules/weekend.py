"""
Rule: Weekend awareness (+ optional weekend-specific caps & approvals)

Fixes in this version:
- If tx.date is None → rule SKIPS gracefully (avoids .weekday() crash).
- If tx.category_code is None → rule SKIPS.
- Case-insensitive category handling for caps + require_approval_for.
- Adds CAP_OVER_TX risk flag when weekend cap is breached.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from app.models import HardRequirement, PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enabled(rp: Dict[str, Any]) -> bool:
    wk = (rp.get("weekend") or {})
    return bool(wk.get("enabled", True))


def _msg(code: str, text: str, severity: Severity = Severity.WARNING) -> PolicyMessage:
    return PolicyMessage(
        severity=severity,
        code=code,
        text=text,
        suggested_actions=["split_expense", "attach_manager_approval"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _hard(code: str, label: str) -> HardRequirement:
    return HardRequirement(code=code, label=label)


def _norm_category(value: Optional[str]) -> Optional[str]:
    """Normalize category strings for case-insensitive matching."""
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


def _caps_for_category(rp: Dict[str, Any], category: str, weekend: bool) -> Optional[Decimal]:
    """
    Safely extract caps for category, case-insensitive.

    Priority:
    - If weekend=True and caps.per_tx_weekend has an entry → use that
    - Else fall back to caps.per_tx
    """
    norm_cat = _norm_category(category)
    if not norm_cat:
        return None

    caps = (rp.get("caps") or {})
    wk_caps_raw = (caps.get("per_tx_weekend") or {})
    gen_caps_raw = (caps.get("per_tx") or {})

    # Normalize keys to lowercase for case-insensitive access
    wk_caps = { _norm_category(k): v for k, v in wk_caps_raw.items() if _norm_category(k) }
    gen_caps = { _norm_category(k): v for k, v in gen_caps_raw.items() if _norm_category(k) }

    if weekend and norm_cat in wk_caps:
        return _to_decimal(wk_caps.get(norm_cat))

    if norm_cat in gen_caps:
        return _to_decimal(gen_caps.get(norm_cat))

    return None


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

async def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    """
    Safe weekend rule:
    - If date is missing → skip rule (required_fields rule will handle).
    - If category missing → skip rule.
    - Weekend detection via repos.is_weekend().
    - Case-insensitive category matching for caps + approval list.
    """
    if not _enabled(rulepack):
        return None

    tx = req.tx

    # SAFETY: Date missing → SKIP (let required_fields handle)
    if not getattr(tx, "date", None):
        return None

    # SAFETY: Category missing → SKIP (cannot apply weekend cap or approval rules)
    if not getattr(tx, "category_code", None):
        return None

    # Check weekend via repo helper (safe)
    try:
        is_weekend = await repos.is_weekend(tx.date, (rulepack.get("weekend") or {}))
    except Exception:
        # If weekend detection fails, SKIP silently (rule error handled at engine level)
        return None

    if not is_weekend:
        # Not a weekend → weekend rule does nothing
        return None

    # Normalized category for comparisons
    norm_cat = _norm_category(tx.category_code)

    # -----------------------------------------
    # Always FLAG weekend usage (recon feature)
    # -----------------------------------------
    messages = [
        _msg("WEEKEND", f"Weekend submission detected for category '{tx.category_code}'.")
    ]
    flags = [
        _flag("WEEKEND", f"{tx.date.isoformat()} is a weekend day.")
    ]
    hard_reqs: list[HardRequirement] = []
    status_delta: Optional[str] = None

    # -----------------------------------------
    # Weekend caps
    # -----------------------------------------
    cap = _caps_for_category(rulepack, tx.category_code, weekend=True)

    if cap is not None:
        amount = _to_decimal(getattr(tx, "amount", 0) or 0)
        if amount is not None and amount > cap:
            over = amount - cap
            messages.append(
                _msg(
                    "CAP_OVER_TX",
                    (
                        f"Weekend cap for '{tx.category_code}' is {cap:.2f}; "
                        f"you’re over by {over:.2f}. Consider splitting or attaching an approval note."
                    )
                )
            )
            # Risk flag for CAP_OVER_TX so risk_weights.CAP_OVER_TX applies
            flags.append(
                _flag(
                    "CAP_OVER_TX",
                    f"Weekend cap breach for category '{tx.category_code}': amount={amount:.2f}, cap={cap:.2f}"
                )
            )
            status_delta = "SOFT_BLOCK"

    # -----------------------------------------
    # Weekend approval requirement
    # -----------------------------------------
    wk_cfg = (rulepack.get("weekend") or {})
    require_for_raw = wk_cfg.get("require_approval_for") or []

    require_for = {
        _norm_category(c) for c in require_for_raw if _norm_category(c)
    }

    if norm_cat and norm_cat in require_for:
        hard_reqs.append(
            _hard("REQUIRE_MANAGER_NOTE", "Attach weekend manager approval note")
        )
        # Upgrade to HARD_BLOCK if not already set
        status_delta = "HARD_BLOCK"

    # -----------------------------------------
    # Build outcome
    # -----------------------------------------
    return RuleOutcome(
        status_delta=status_delta,
        messages=messages,
        flags=flags,
        hard_requirements=hard_reqs,
    )
