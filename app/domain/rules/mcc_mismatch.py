"""
Rule: MCC/category mismatch

Policy intent (PoC):
- If a card transaction's MCC does not align with the selected expense category,
  surface a warning and add a risk flag for Finance review.

Rule pack knobs:
{
  "mcc_rules": {
    "category_allowed_mcc": {
      "Entertainment": ["5813","7932","7929"]
    },
    "mismatch_weight": 15   # (handled by risk_weights; kept here for reference)
  }
}

Behavior:
- If tx.mcc is missing → no-op (not a card tx / MCC unavailable).
- If tx.category_code is missing/blank → no-op.
- Case-insensitive category handling:
    rulepack keys like "ENTERTAINMENT" or "Entertainment" work.
- If the category has an allowlist and MCC NOT in it:
    → WARNING message (no block) + RiskFlag MCC_MISMATCH.
- If no allowlist configured for the category → no-op (PoC-friendly).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.models import PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(rulepack: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract the mcc_rules section."""
    return (rulepack.get("mcc_rules") or {}) if isinstance(rulepack, dict) else {}


def _msg(code: str, text: str) -> PolicyMessage:
    return PolicyMessage(
        severity=Severity.WARNING,
        code=code,
        text=text,
        suggested_actions=["verify_category", "add_justification"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _norm_cat(value: Optional[str]) -> Optional[str]:
    """Normalize category for case-insensitive comparisons."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s.lower()


def _norm_mcc_list(values: Any) -> set[str]:
    """
    Normalize a list of MCC codes to a clean set of strings
    (trimmed, non-empty).
    """
    if not values:
        return set()
    try:
        return {
            str(v).strip()
            for v in values
            if str(v).strip()
        }
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    tx = getattr(req, "tx", None)
    if tx is None:
        return None

    # If MCC is missing → nothing to do
    mcc_raw = getattr(tx, "mcc", None)
    mcc = (str(mcc_raw).strip() if mcc_raw is not None else "")
    if not mcc:
        return None

    # If category is missing/blank → skip (let required_fields handle)
    category = getattr(tx, "category_code", None)
    norm_cat = _norm_cat(category)
    if not norm_cat:
        return None

    cfg = _cfg(rulepack)
    mapping_raw = cfg.get("category_allowed_mcc") or {}

    # Normalize mapping keys for case-insensitive category lookup
    normalized_mapping: Dict[str, set[str]] = {}
    if isinstance(mapping_raw, dict):
        for k, v in mapping_raw.items():
            key_norm = _norm_cat(k)
            if key_norm:
                normalized_mapping[key_norm] = _norm_mcc_list(v)

    allowed = normalized_mapping.get(norm_cat, set())
    if not allowed:
        # No allowlist configured for this category → PoC: treat as no-op
        return None

    if mcc not in allowed:
        allowed_str = ", ".join(sorted(allowed)) or "—"
        return RuleOutcome(
            status_delta=None,  # warn only (no block) in PoC
            messages=[
                _msg(
                    "MCC_MISMATCH",
                    (
                        f"MCC {mcc} doesn’t match the expected set for "
                        f"'{category}' ({allowed_str}). Please verify category or add justification."
                    ),
                )
            ],
            flags=[
                _flag(
                    "MCC_MISMATCH",
                    f"mcc={mcc}, allowed={sorted(list(allowed))}",
                )
            ],
            hard_requirements=[],
        )

    # MCC is within the allowed set → no-op
    return None
