"""
Rule: Monthly budget (MTD) per category

Policy intent (PoC):
- Each category can have a monthly spending limit per org.
- When (MTD_spend + current_tx) > limit → warn the user and flag risk.

Rulepack knobs (two sources for limit; DB takes precedence when implemented):
1) DB table budgets_monthly (org_id, category_code, month_start, amount)  <-- future primary
2) Rule pack fallback:
{
  "budgets": {
    "monthly_limits": { "ENTERTAINMENT": 3000 }
  }
}

Behavior:
- If no limit found (DB or rulepack) → no-op.
- Compute MTD from history (via repos.mtd_spend; PoC stub returns 0) and add current tx amount.
- If over limit → SOFT_BLOCK (PoC choice; change to HARD if you want),
  message explains remaining/overage, risk flag BUDGET_OVER.

Implementation notes:
- All money values are converted to Decimal for safe arithmetic.
- If repos.resolve_budget / repos.mtd_spend are not implemented or fail,
  this rule falls back gracefully (no RULE_ERROR).
"""

from __future__ import annotations

import inspect
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


def _to_decimal(value: Any) -> Optional[Decimal]:
    """
    Safely convert arbitrary numeric-ish value to Decimal.
    Returns None if conversion fails.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _norm_cat(value: Optional[str]) -> Optional[str]:
    """Normalize category code for case-insensitive comparisons."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s.lower()


async def _resolve_limit(
    tx,
    rulepack: Dict[str, Any],
    repos: Any,
) -> Optional[Decimal]:
    """
    Resolve the monthly limit for this transaction's category.

    Priority:
      1) DB via repos.resolve_budget(tx=tx, rulepack=rulepack)
         - Expected to return either:
             - dict with {"limit": ...} (as in DBPolicyRepos), or
             - a raw numeric (float/Decimal/int) in any custom impl
      2) rulepack.budgets.monthly_limits[category] (case-insensitive)

    Returns Decimal or None if no usable limit.
    """
    category = getattr(tx, "category_code", None)
    norm_cat = _norm_cat(category)
    if not norm_cat:
        return None

    # --- 1) Try DB-backed resolve_budget, if present ---
    if hasattr(repos, "resolve_budget"):
        try:
            res = repos.resolve_budget(tx=tx, rulepack=rulepack)
            if inspect.isawaitable(res):
                res = await res

            # DBPolicyRepos returns a dict {"limit": ..., "actual": ..., ...}
            if isinstance(res, dict):
                limit_db = _to_decimal(res.get("limit"))
            else:
                # Custom implementations may just return a scalar
                limit_db = _to_decimal(res)

            if limit_db is not None:
                return limit_db
        except Exception:
            # PoC: swallow and fall back to rulepack
            pass

    # --- 2) Rulepack fallback (case-insensitive keys) ---
    budgets_cfg = (rulepack.get("budgets") or {})
    monthly_limits_raw = (budgets_cfg.get("monthly_limits") or {})

    # Normalize keys to lowercase
    monthly_limits_norm: Dict[str, Any] = {}
    for k, v in monthly_limits_raw.items():
        k_norm = _norm_cat(k)
        if k_norm:
            monthly_limits_norm[k_norm] = v

    limit_cfg = _to_decimal(monthly_limits_norm.get(norm_cat))
    return limit_cfg


async def _get_mtd_spend(
    org_id: str,
    user_id: str,
    category: str,
    tx_date,
    repos: Any,
) -> Decimal:
    """
    Get month-to-date spend for this (org, user, category, month).

    If repos.mtd_spend is not implemented or fails, returns Decimal('0').

    Expected (but not enforced) signature if implemented:

        async def mtd_spend(org_id: str, user_id: str, category: str, tx_date: date) -> Decimal | float | None
    """
    if not tx_date:
        return Decimal("0")

    if not hasattr(repos, "mtd_spend"):
        return Decimal("0")

    try:
        res = repos.mtd_spend(org_id, user_id, category, tx_date)
        if inspect.isawaitable(res):
            res = await res
        dec = _to_decimal(res)
        return dec if dec is not None else Decimal("0")
    except Exception:
        # For PoC: if MTD calculation fails, treat as 0 and don't crash rule
        return Decimal("0")


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

async def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    tx = req.tx
    org_id, user_id = req.org_id, req.user_id
    category = getattr(tx, "category_code", None)

    # If we don't have a date, we can't do MTD logic → no-op
    if not getattr(tx, "date", None):
        return None

    # If we don't have a category, we can't map to any limit → no-op
    if not category:
        return None

    # 1) Resolve monthly limit (DB first via repos.resolve_budget, then rulepack fallback)
    limit = await _resolve_limit(
        tx=tx,
        rulepack=rulepack,
        repos=repos,
    )
    if limit is None:
        # no limit configured → pass-through
        return None

    # 2) Compute current MTD spend (existing tx) and add current amount
    current_mtd = await _get_mtd_spend(
        org_id=org_id,
        user_id=user_id,
        category=category,
        tx_date=tx.date,
        repos=repos,
    )

    amount = _to_decimal(getattr(tx, "amount", None))
    if amount is None:
        # if amount is weird, let some other rule handle it; skip budget check
        return None

    projected = current_mtd + amount

    if projected <= limit:
        # within budget → no outcome
        return None

    over = projected - limit
    remaining = limit - current_mtd if limit > current_mtd else Decimal("0.00")

    text = (
        f"Monthly budget for '{category}' is {limit:.2f}. "
        f"Current MTD: {current_mtd:.2f}. This expense {amount:.2f} "
        f"would exceed the limit by {over:.2f}. "
        f"Remaining before this tx: {remaining:.2f}."
    )

    return RuleOutcome(
        status_delta="SOFT_BLOCK",  # PoC choice; flip to HARD_BLOCK if you want stricter behavior
        messages=[_msg("BUDGET_OVER", text)],
        flags=[
            _flag(
                "BUDGET_OVER",
                f"MTD {current_mtd:.2f} + tx {amount:.2f} > limit {limit:.2f}",
            )
        ],
        hard_requirements=[],
    )
