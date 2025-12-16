"""
Rule: FX-rate and Tip outliers

Policy intent (PoC):
- Surface suspicious FX rates (vs. a simple reference) and unusually large tips.
- These are REVIEW cues, not blocks, in the PoC.

Rule pack knobs:
{
  "fx_rules": {
    "max_deviation_pct": 5.0,         # flag if |tx.fx_rate - ref|/ref * 100 > 5%
    "tip_max_pct": 20.0,              # flag if tip_amount > 20% of amount
    "reference": { "USD": 3.6725 }    # optional per-currency base; fallback map used if absent
  }
}

Behavior:
- If currency != AED and tx.fx_rate present:
    - compare with reference; if deviation exceeds → WARNING + RiskFlag FX_OUTLIER
- If tip_amount present and exceeds tip_max_pct of amount:
    - WARNING + RiskFlag TIP_OUTLIER
- No status change (PASS) in PoC.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.models import PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(rulepack: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract fx_rules config from rulepack."""
    if not isinstance(rulepack, dict):
        return {}
    return rulepack.get("fx_rules") or {}


def _msg(code: str, text: str) -> PolicyMessage:
    return PolicyMessage(
        severity=Severity.WARNING,
        code=code,
        text=text,
        suggested_actions=["verify_rate", "add_justification", "adjust_tip_if_needed"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _reference_rates(cfg: Dict[str, Any]) -> Dict[str, float]:
    """
    Build a reference FX rate map.

    Priority:
      1) cfg["reference"] from rulepack
      2) built-in defaults for common currencies (PoC)
    """
    base = {
        # PoC fallback references (AED pegs/approx)
        "USD": 3.6725,
        "EUR": 4.0,
        "GBP": 4.5,
    }
    ref_cfg = cfg.get("reference") or {}
    # overlay rulepack-specified references
    for k, v in ref_cfg.items():
        f = _float_or_none(v)
        if f is not None:
            base[str(k).upper()] = f
    return base


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

async def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    """
    FX & Tip outlier rule (warn-only):

    - Uses only tx fields + rulepack.fx_rules (no DB dependency).
    - Fully defensive against missing fields and bad data.
    """
    tx = getattr(req, "tx", None)
    if tx is None:
        return None

    cfg = _cfg(rulepack)
    if not cfg:
        # No fx_rules section → no FX/tip policy to apply
        return None

    messages = []
    flags = []

    # -------------------------------------------------------------------
    # FX-rate outlier?
    # -------------------------------------------------------------------
    try:
        currency = (getattr(tx, "currency", "") or "").upper().strip()
        fx_rate = _float_or_none(getattr(tx, "fx_rate", None))
        max_dev_pct = _float_or_none(cfg.get("max_deviation_pct")) or 5.0

        # Only check FX if non-AED currency AND fx_rate provided AND ref available
        if currency and currency != "AED" and fx_rate is not None and fx_rate > 0:
            ref_map = _reference_rates(cfg)
            ref = ref_map.get(currency)

            if ref is not None and ref > 0:
                deviation_pct = abs(fx_rate - ref) / ref * 100.0
                if deviation_pct > max_dev_pct:
                    messages.append(
                        _msg(
                            "FX_OUTLIER",
                            (
                                "Applied FX rate looks off vs. reference. "
                                "Please verify the rate and add a short note if correct."
                            ),
                        )
                    )
                    flags.append(
                        _flag(
                            "FX_OUTLIER",
                            f"currency={currency}, fx_rate={fx_rate}, ref={ref}, deviation_pct={deviation_pct:.2f}",
                        )
                    )
    except Exception:
        # Non-fatal in PoC: silently skip FX outlier if anything goes wrong.
        pass

    # -------------------------------------------------------------------
    # Tip outlier?
    # -------------------------------------------------------------------
    try:
        amount = _float_or_none(getattr(tx, "amount", None)) or 0.0
        tip = _float_or_none(getattr(tx, "tip_amount", None)) or 0.0
        tip_max_pct = _float_or_none(cfg.get("tip_max_pct")) or 20.0

        if amount > 0 and tip > 0 and tip_max_pct > 0:
            tip_pct = (tip / amount) * 100.0
            if tip_pct > tip_max_pct:
                messages.append(
                    _msg(
                        "TIP_OUTLIER",
                        (
                            f"Tip appears above the usual policy threshold ({tip_max_pct:.1f}% of bill). "
                            "Please justify briefly in the description."
                        ),
                    )
                )
                flags.append(
                    _flag(
                        "TIP_OUTLIER",
                        f"amount={amount:.2f}, tip={tip:.2f}, tip_pct={tip_pct:.2f}",
                    )
                )
    except Exception:
        # Same: warn-only rule, do not break evaluation.
        pass

    if not messages:
        return None

    # Warn-only rule: no status_delta, no hard requirements
    return RuleOutcome(
        status_delta=None,
        messages=messages,
        flags=flags,
        hard_requirements=[],
    )
