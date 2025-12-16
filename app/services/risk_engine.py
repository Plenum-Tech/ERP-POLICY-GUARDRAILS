"""
RiskEngine — additive, transparent risk scoring for the Policy & Risk Guardrails PoC.

Design goals:
- Deterministic, explainable scoring (no ML in the PoC).
- Weights are owned by Finance via the rule pack (JSON).
- Flags are deduplicated by code; the highest-weight interpretation wins.
- Total score is the (capped) sum of weights, in [0, 100].

Contract:
- Input flags can be either domain.schemas.RiskFlag models OR plain dicts.
- `score()` returns a RiskPayload (score + weighted flags).
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Union

from app.models import RiskFlag, RiskPayload


FlagLike = Union[RiskFlag, Dict]


class RiskEngine:
    """
    Minimal additive risk model.

    Usage:
        engine = RiskEngine(cap=100, default_weight=5)
        payload = engine.score(flags, rulepack.get("risk_weights", {}))
        # payload.score -> float; payload.flags -> List[RiskFlag] (weights filled in)
    """

    def __init__(self, cap: int = 100, default_weight: int = 5) -> None:
        self.cap = int(cap)
        self.default_weight = int(default_weight)

    # ---------- Public API ----------

    def score(self, flags: Iterable[FlagLike], weights: Optional[Dict[str, int]] = None) -> RiskPayload:
        """
        Compute a RiskPayload from raw flags and optional per-code weights.

        - Normalizes flags to RiskFlag models.
        - Deduplicates by `code` (keeps the highest-weight variant).
        - Applies weights (rulepack overrides > default).
        - Caps the total at `self.cap`.
        """
        weights = weights or {}

        # Normalize → RiskFlag
        normalized: List[RiskFlag] = [self._to_model(f) for f in flags if f is not None]

        # Deduplicate by code; keep the richest reason and highest weight hint (if any)
        deduped: Dict[str, RiskFlag] = {}
        for f in normalized:
            key = f.code
            if key not in deduped:
                deduped[key] = f
            else:
                # Merge reasons (first non-empty kept, or concatenate distinct reasons)
                prev = deduped[key]
                if (prev.reason or "").strip() != (f.reason or "").strip():
                    # Prefer keeping both reasons when different (short & readable)
                    prev.reason = "; ".join([r for r in [prev.reason, f.reason] if r])[:240]
                # If either carried a higher pre-set weight, keep the max
                if (f.weight or 0) > (prev.weight or 0):
                    prev.weight = f.weight

        # Apply rulepack weights (override any pre-set)
        weighted: List[RiskFlag] = []
        total = 0
        for f in deduped.values():
            w = self._resolve_weight(f, weights)
            f.weight = w
            weighted.append(f)
            total += w

        total = float(min(self.cap, total))
        return RiskPayload(score=total, flags=weighted)

    # ---------- Helpers ----------

    def _to_model(self, f: FlagLike) -> RiskFlag:
        """Accept dicts or RiskFlag models and normalize to RiskFlag."""
        if isinstance(f, RiskFlag):
            # Ensure basic cleanliness (truncate overly long reasons)
            f.reason = (f.reason or "")[:240]
            return f
        if isinstance(f, dict):
            code = str(f.get("code", "")).strip()
            reason = str(f.get("reason", "") or "")
            weight = f.get("weight", None)
            weight = int(weight) if isinstance(weight, (int, float)) else None
            return RiskFlag(code=code, reason=reason[:240], weight=weight)
        # Fallback — unknown type; create a generic flag
        return RiskFlag(code="UNKNOWN_FLAG", reason="Non-standard flag payload")

    def _resolve_weight(self, flag: RiskFlag, rp_weights: Dict[str, int]) -> int:
        """
        Order of precedence for per-flag weight:
        1) rule pack override (rp_weights[code]) if present
        2) flag.weight if the caller pre-set a numeric value
        3) engine default_weight
        """
        if flag.code in rp_weights:
            try:
                return int(rp_weights[flag.code])
            except Exception:
                pass
        if flag.weight is not None:
            try:
                return int(flag.weight)
            except Exception:
                pass
        return self.default_weight

    # ---------- Optional: banding utilities (not returned in API, but handy for dashboards) ----------

    @staticmethod
    def band(score: float) -> str:
        """
        Map a score to a qualitative band.
        This is intentionally NOT part of the HTTP response schema, but can be used in Recon UI.
        """
        if score >= 70:
            return "high"
        if score >= 40:
            return "medium"
        return "low"
