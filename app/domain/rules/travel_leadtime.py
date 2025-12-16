"""
Rule: Travel lead time (working days notice)

Policy intent (PoC):
- Travel should be planned with a minimum working-days notice (default 7).
- Short-notice travel is allowed only with justification/approval.

Rule pack knobs:
{
  "travel": {
    "min_notice_working_days": 7
  }
}

Inputs (PoC):
- We read the intended trip start date from `req.context["travel_start"]`.
  Accepted formats:
    - "YYYY-MM-DD"
    - full ISO datetime string (we take .date())
    - a Python date/datetime (if caller already parsed it)
  If absent, the rule is a no-op to keep the API lean.

"Working days":
- Use weekend config from rule pack (default UAE Fri/Sat).
- Exclude org holidays using repos.is_holiday(org_id, date).

Behavior:
- If working-days gap between "today" (submission time) and travel_start < min_notice:
    → SOFT_BLOCK + TRAVEL_SHORT_NOTICE flag
    → Message suggests attaching client justification or manager approval.
- If travel_start <= today → gap=0 → treated as short notice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, date
from typing import Any, Dict, Optional

import pytz

from app.core.config import settings
from app.models import PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(rp: Dict[str, Any]) -> Dict[str, Any]:
    return (rp.get("travel") or {}) if isinstance(rp, dict) else {}


def _msg(code: str, text: str) -> PolicyMessage:
    return PolicyMessage(
        severity=Severity.WARNING,
        code=code,
        text=text,
        suggested_actions=["attach_client_justification", "attach_manager_approval"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _parse_travel_start(raw: Any) -> Optional[date]:
    """
    Accepts:
      - date
      - datetime
      - ISO string "YYYY-MM-DD" or full ISO datetime
    Returns a date or None if parsing fails.
    """
    if raw is None:
        return None

    # Already a date/datetime?
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()

    # String-ish
    try:
        s = str(raw).strip()
        if not s:
            return None
        # Take only the date part; datetime.fromisoformat handles both date & datetime
        return datetime.fromisoformat(s[:10]).date()
    except Exception:
        return None


async def _is_working_day(d: date, weekend_days: set[str], org_id: str, repos) -> bool:
    """
    A working day is:
      - not in weekend_days
      - not an org holiday (via repos.is_holiday(org_id, d))
    """
    weekday_map = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    day_code = weekday_map[d.weekday()]
    if day_code in weekend_days:
        return False

    try:
        if await repos.is_holiday(org_id, d):
            return False
    except Exception:
        # If holiday check fails, treat as non-holiday (PoC-friendly)
        return True

    return True


async def _working_days_between(
    start: date,
    end: date,
    weekend_days: set[str],
    org_id: str,
    repos,
) -> int:
    """
    Count working days in (start, end], i.e., days strictly after `start`
    up to and including `end`.

    If end <= start, returns 0.
    """
    if end <= start:
        return 0

    days = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if await _is_working_day(cur, weekend_days, org_id, repos):
            days += 1
        cur += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

async def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    cfg = _cfg(rulepack)

    # Min working days notice (defensive parsing)
    try:
        min_notice = int(cfg.get("min_notice_working_days", 7))
    except Exception:
        min_notice = 7

    # Travel start comes from context
    ctx = getattr(req, "context", {}) or {}
    travel_start_raw = ctx.get("travel_start")
    if not travel_start_raw:
        # No travel context → no-op (PoC-friendly)
        return None

    travel_start = _parse_travel_start(travel_start_raw)
    if not travel_start:
        # Invalid date format → WARN but do not block
        return RuleOutcome(
            status_delta=None,
            messages=[
                _msg(
                    "TRAVEL_DATE_INVALID",
                    "Travel start date format is invalid; please use YYYY-MM-DD.",
                )
            ],
            flags=[_flag("TRAVEL_DATE_INVALID", f"value={travel_start_raw!r}")],
            hard_requirements=[],
        )

    # Today in service timezone (submission time)
    tz_name = getattr(settings, "APP_TIMEZONE", "Asia/Dubai")
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("Asia/Dubai")
    today = datetime.now(tz).date()

    # Weekend config: normalize to uppercase day codes
    weekend_cfg = (rulepack.get("weekend") or {}) if isinstance(rulepack, dict) else {}
    weekend_raw = weekend_cfg.get("days", ["FRI", "SAT"])
    weekend_days = {str(d).strip().upper() for d in weekend_raw if str(d).strip()}

    # Compute working-day gap
    gap = await _working_days_between(today, travel_start, weekend_days, req.org_id, repos)

    if gap >= min_notice:
        # Enough notice → no outcome
        return None

    miss = max(min_notice - gap, 0)
    text = (
        f"Travel planned with only {gap} working day(s) notice; minimum required is {min_notice}. "
        f"You're short by {miss} day(s). Please attach client justification or manager approval."
    )

    return RuleOutcome(
        status_delta="SOFT_BLOCK",
        messages=[_msg("TRAVEL_SHORT_NOTICE", text)],
        flags=[
            _flag(
                "TRAVEL_SHORT_NOTICE",
                f"gap={gap}, min={min_notice}, travel_start={travel_start.isoformat()}",
            )
        ],
        hard_requirements=[],
    )
