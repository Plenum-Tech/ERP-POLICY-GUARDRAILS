"""
Rule: Travel/Class/Hotel upgrades require prior approval

Policy intent (PoC):
- Any upgrade (e.g., flight class, hotel tier, add-on beyond standard policy)
  must have manager/finance approval attached before submission.

Rule pack knobs (all optional, safe defaults):
{
  "travel": {
    "upgrades_require_approval": true,   // master toggle
    "soft_mode": false,                  // if true → SOFT_BLOCK instead of HARD_BLOCK
    "upgrade_scope_categories": [        // optional category scoping (case-insensitive)
      "Travel",
      "Flight",
      "Hotel"
    ]
  }
}

Trigger (PoC):
- We read a simple boolean-ish flag from the request context:
    req.context["upgrade_requested"] == True

  We treat the following as "true" (case-insensitive):
    - True
    - "true", "yes", "y", "1"

Behavior:
- If upgrades_require_approval = true AND upgrade_requested = true:
    → If soft_mode = false (default):
         HARD_BLOCK with explicit hard requirement "REQUIRE_MANAGER_NOTE"
       Else (soft_mode = true):
         SOFT_BLOCK
    → Risk flag: UPGRADE_NO_APPROVAL

- If upgrade_scope_categories is set:
    - Only applies when tx.category_code is in that list (case-insensitive).
- Otherwise → no-op.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.models import HardRequirement, PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(rulepack: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract the 'travel' config section."""
    return (rulepack.get("travel") or {}) if isinstance(rulepack, dict) else {}


def _msg(code: str, text: str, severity: Severity = Severity.ERROR) -> PolicyMessage:
    return PolicyMessage(
        severity=severity,
        code=code,
        text=text,
        suggested_actions=["attach_manager_approval"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _hard(code: str, label: str) -> HardRequirement:
    return HardRequirement(code=code, label=label)


def _truthy(value: Any) -> bool:
    """
    Interpret common truthy forms for upgrade_requested:
    True, "true", "yes", "y", "1" → True
    Everything else → False.
    """
    if value is True:
        return True
    if isinstance(value, str):
        s = value.strip().lower()
        return s in {"true", "yes", "y", "1"}
    return False


def _norm_category(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s.lower()


def _safe_context(req) -> Dict[str, Any]:
    """
    Normalise req.context into a dict.

    Handles:
    - None → {}
    - dict → as-is
    - Pydantic BaseModel → model_dump()
    - anything else → {}
    """
    raw = getattr(req, "context", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    # Try Pydantic-style model_dump()
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    cfg = _cfg(rulepack)

    # Master toggle; default True (upgrades need approval unless explicitly disabled)
    if not bool(cfg.get("upgrades_require_approval", True)):
        return None

    ctx = _safe_context(req)
    upgrade_requested = _truthy(ctx.get("upgrade_requested", False))
    if not upgrade_requested:
        # No upgrade in this tx → no-op
        return None

    tx = getattr(req, "tx", None)
    if tx is None:
        # No transaction payload → nothing to enforce against
        return None

    # Optional category scoping
    scope_raw = cfg.get("upgrade_scope_categories") or []
    scope = {_norm_category(c) for c in scope_raw if _norm_category(c)}

    if scope:
        tx_cat = _norm_category(getattr(tx, "category_code", None))
        # If scope is defined and tx category is outside scope → skip
        if tx_cat not in scope:
            return None

    # Decide whether this is HARD or SOFT block based on soft_mode flag
    soft_mode = bool(cfg.get("soft_mode", False))
    status_delta = "SOFT_BLOCK" if soft_mode else "HARD_BLOCK"

    # Build outcome: upgrade requested but no explicit approval artifact yet
    return RuleOutcome(
        status_delta=status_delta,
        messages=[
            _msg(
                "UPGRADE_NO_APPROVAL",
                (
                    "An upgrade has been requested. Please attach a manager/finance "
                    "approval note to proceed."
                ),
            )
        ],
        flags=[
            _flag(
                "UPGRADE_NO_APPROVAL",
                "Upgrade requested without manager/finance approval attachment",
            )
        ],
        hard_requirements=[
            _hard(
                "REQUIRE_MANAGER_NOTE",
                "Attach manager/finance approval for the upgrade",
            )
        ],
    )
