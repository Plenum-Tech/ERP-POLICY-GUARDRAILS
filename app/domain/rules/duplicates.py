"""
Rule: Potential duplicate claim

Policy intent (PoC):
- Prevent/flag accidental double-claims of the same expense.

Rule pack knobs:
{
  "duplicate_rules": {
    "enabled": true,                # optional; default True
    "window_days": 14,              # lookback window
    "same_amount_tolerance": 1.00,  # ± amount tolerance (tx currency units)
    "desc_fuzzy_threshold": 0.85,   # 0..1 (RapidFuzz token_set_ratio/100)
    "time_bucket_hours": 24         # PoC approximates with same-date bucket
  }
}

Behavior (uses repos.duplicate_suspect() when available):
- If suspect duplicate found within window:
    → SOFT_BLOCK with guidance to add justification or cancel
    → Risk flag: DUPLICATE

Safety:
- If duplicate_rules missing/disabled → no-op.
- If tx.date/amount missing → no-op (let required_fields rule handle).
- If repos has no duplicate_suspect() helper → no-op (no RULE_ERROR spam).
- If helper raises, we swallow and treat as no-op (engine keeps flow healthy).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.models import PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(rp: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(rp, dict):
        return {}
    cfg = rp.get("duplicate_rules") or {}
    # allow a simple on/off switch
    if not isinstance(cfg, dict):
        return {}
    return cfg


def _enabled(cfg: Dict[str, Any]) -> bool:
    # default to enabled=True if not specified
    return bool(cfg.get("enabled", True))


def _msg(code: str, text: str, severity: Severity = Severity.WARNING) -> PolicyMessage:
    return PolicyMessage(
        severity=severity,
        code=code,
        text=text,
        suggested_actions=["add_justification", "cancel_if_duplicate"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

async def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    cfg = _cfg(rulepack)
    if not cfg or not _enabled(cfg):
        # No duplicate policy configured or explicitly disabled → no-op
        return None

    tx = getattr(req, "tx", None)
    if tx is None:
        return None

    # SAFETY: if critical fields missing, skip and let required_fields handle
    if not getattr(tx, "date", None):
        return None
    if getattr(tx, "amount", None) is None:
        return None

    description = (getattr(tx, "description", None) or "").strip()

    # If the repo doesn't implement duplicate_suspect yet, don't error, just skip.
    helper = getattr(repos, "duplicate_suspect", None)
    if helper is None or not callable(helper):
        return None

    # Build a minimal tx dict for the helper (keeps repo decoupled from Pydantic model)
    tx_payload = {
        "date": tx.date,
        "amount": tx.amount,
        "description": description,
    }

    # Call helper as async or sync, be forgiving with signatures
    suspect: Any = None
    try:
        try:
            # Preferred: async helper
            suspect = await helper(
                org_id=getattr(req, "org_id", "") or "",
                user_id=getattr(req, "user_id", "") or "",
                tx=tx_payload,
                dup_rules=cfg,
            )
        except TypeError:
            # Fallback: maybe helper is sync or has different signature
            suspect = helper(
                org_id=getattr(req, "org_id", "") or "",
                user_id=getattr(req, "user_id", "") or "",
                tx=tx_payload,
                dup_rules=cfg,
            )
    except Exception:
        # Any failure → treat as no-op; engine should not see RULE_ERROR for this PoC
        return None

    # If helper indicates "no suspect", rule is a no-op
    if not suspect:
        return None

    # Try to extract some extra context for the risk reason (optional)
    reason = "Similar amount & description within lookback window"
    if isinstance(suspect, dict):
        # Allow repo to pass richer info e.g. {"reason": "...", "matched_tx_id": "..."}
        reason = suspect.get("reason") or reason

    return RuleOutcome(
        status_delta="SOFT_BLOCK",
        messages=[
            _msg(
                "DUPLICATE",
                (
                    "This looks similar to a recent claim (same-day/amount/description). "
                    "If this is not a duplicate, add a short justification before submitting."
                ),
            )
        ],
        flags=[_flag("DUPLICATE", reason)],
        hard_requirements=[],
    )
