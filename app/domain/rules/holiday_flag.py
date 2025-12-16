"""
Rule: Public holiday awareness

Policy intent (PoC):
- Surface when a transaction occurs on an org-designated public holiday.
- Default behavior is informational FLAG (no block), but can be escalated via rule pack.

Rule pack knobs (optional):
{
  "holidays": {
    "policy": "flag"        // "flag" | "soft_block" | "hard_block"
  },
  "holiday_dates": ["2025-12-01", "2025-12-02", ...]  // ISO date strings (used by repo.is_holiday)
}

Behavior:
- Check tx.date against holiday list via repos.is_holiday(tx.date, rulepack).
- If holiday:
  - Always add RiskFlag HOLIDAY_SPEND.
  - Message severity & status_delta follow the configured policy (default: flag/info).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.models import PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _policy(rulepack: Dict[str, Any]) -> str:
    """
    Extract holiday policy mode: "flag" | "soft_block" | "hard_block".
    Defaults to "flag" on any error.
    """
    try:
        if not isinstance(rulepack, dict):
            return "flag"
        holidays_cfg = rulepack.get("holidays") or {}
        return str(holidays_cfg.get("policy", "flag")).lower()
    except Exception:
        return "flag"


def _msg(code: str, text: str, severity: Severity) -> PolicyMessage:
    return PolicyMessage(
        severity=severity,
        code=code,
        text=text,
        suggested_actions=["add_justification"],  # harmless default; UI may ignore for pure flag mode
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

async def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    """
    Holiday rule:

    - Safely skips if tx.date is missing.
    - Uses repos.is_holiday(tx.date, rulepack) so it matches DBPolicyRepos.is_holiday signature.
    - Policy mode:
        "flag"       → INFO, no status_delta
        "soft_block" → WARNING, status_delta="SOFT_BLOCK"
        "hard_block" → ERROR, status_delta="HARD_BLOCK"
    """
    tx = getattr(req, "tx", None)
    if tx is None:
        return None

    tx_date = getattr(tx, "date", None)
    if not tx_date:
        # Let required_fields handle missing date; holiday rule is a no-op
        return None

    # Ask repo if this date is a holiday (rulepack-driven, no DB table yet)
    try:
        is_hol = await repos.is_holiday(tx_date, rulepack)
    except Exception:
        # Defensive: if helper fails, skip rule (engine will handle RULE_ERROR if we let it bubble)
        return None

    if not is_hol:
        return None

    policy_mode = _policy(rulepack)
    if policy_mode == "hard_block":
        status_delta = "HARD_BLOCK"
        severity = Severity.ERROR
    elif policy_mode == "soft_block":
        status_delta = "SOFT_BLOCK"
        severity = Severity.WARNING
    else:
        # "flag" or anything unknown → info-only, no block
        status_delta = None
        severity = Severity.INFO

    msg_text = f"{tx_date.isoformat()} is an official holiday."
    reason = f"Holiday spend on {tx_date.isoformat()}"

    return RuleOutcome(
        status_delta=status_delta,
        messages=[_msg("HOLIDAY_SPEND", msg_text, severity)],
        flags=[_flag("HOLIDAY_SPEND", reason)],
        hard_requirements=[],
    )
