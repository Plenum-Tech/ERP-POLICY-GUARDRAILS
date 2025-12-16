"""
Rule: Reimbursement submission cutoff (day 24)

Policy intent (PoC):
- Claims submitted after cutoff day (default 24) roll into the next salary cycle.
- This is informational in the PoC (no block); surfaces a friendly warning + risk flag.

Rule pack knobs:
{
  "deadlines": {
    "reimbursement_cutoff_day": 24
  }
}

Behavior:
- Determine "now" in the service timezone (Asia/Dubai by default).
- If today's day > cutoff → add WARN message and flag AFTER_CUTOFF.
- No status change (PASS) — i.e., we do NOT block submission for this rule.

Safety:
- If rulepack is missing/invalid, defaults to cutoff_day = 24.
- If timezone setting is invalid, falls back to UTC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import pytz

from app.core.config import settings
from app.models import PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(code: str, text: str) -> PolicyMessage:
    return PolicyMessage(
        severity=Severity.WARNING,
        code=code,
        text=text,
        suggested_actions=["proceed_submit"],  # purely informational in PoC
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _cutoff(rulepack: Dict[str, Any]) -> int:
    """
    Resolve reimbursement cutoff day from rulepack, with safe fallback.
    """
    try:
        if not isinstance(rulepack, dict):
            return 24
        deadlines = rulepack.get("deadlines") or {}
        return int(deadlines.get("reimbursement_cutoff_day", 24))
    except Exception:
        return 24


def _now_local() -> datetime:
    """
    Resolve "now" in the configured service timezone.

    - Uses settings.APP_TIMEZONE if present.
    - Falls back to Asia/Dubai.
    - If timezone is invalid, falls back to UTC.
    """
    tz_name = getattr(settings, "APP_TIMEZONE", "Asia/Dubai") or "Asia/Dubai"
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.UTC
    return datetime.now(tz)


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    """
    Informational cutoff rule:

    - Uses service-local "today" (submission time), not tx.date.
    - If today's day <= cutoff → no-op.
    - If today's day > cutoff → add AFTER_CUTOFF warning + risk flag.
    - No status_delta → does not block submission.
    """
    today = _now_local()
    cutoff_day = _cutoff(rulepack)

    # Before or on cutoff → no warning
    if today.day <= cutoff_day:
        return None

    # After cutoff → warn user; do not block
    return RuleOutcome(
        status_delta=None,  # informational only (no SOFT/HARD block)
        messages=[
            _msg(
                "AFTER_CUTOFF",
                (
                    f"Submitted after cutoff day {cutoff_day}. "
                    "This claim will be processed in the next cycle."
                ),
            )
        ],
        flags=[
            _flag(
                "AFTER_CUTOFF",
                f"Submitted on {today.date().isoformat()} (day={today.day} > cutoff={cutoff_day})",
            )
        ],
        hard_requirements=[],
    )
