"""
Unified utilities module for the ERP-AI Policy & Risk Guardrails service.

Contains:
- Date utilities (weekend/holiday, parsing, working-day calculations)
- Amount utilities (decimal-safe, percent calculations)
- JSON utilities (safe serialization)
- String utilities (normalization, safe conversions)
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any, Dict, List, Optional


# ============================================================
# DATE UTILITIES
# ============================================================

WEEKDAY_MAP = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}


def parse_date(value: Any) -> dt.date:
    """
    Safely parse date input (ISO string or date instance).
    Raises ValueError if invalid.
    """
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.datetime.strptime(value, "%Y-%m-%d").date()
        except Exception:
            raise ValueError(f"Invalid date string: {value}")
    raise ValueError(f"Unsupported date type: {type(value)}")


def is_weekend(date_value: dt.date, weekend_cfg: Dict[str, Any]) -> bool:
    """
    weekend_cfg:
    {
        "enabled": true,
        "days": ["FRI", "SAT"]
    }
    """
    if not weekend_cfg or not weekend_cfg.get("enabled", True):
        return False

    days: List[str] = weekend_cfg.get("days") or ["FRI", "SAT"]

    weekday = date_value.weekday()  # Monday=0, Sunday=6
    weekend_numbers = {WEEKDAY_MAP[d] for d in days if d in WEEKDAY_MAP}

    return weekday in weekend_numbers


def is_holiday(date_value: dt.date, holiday_list: List[str]) -> bool:
    """
    holiday_list example: ["2025-12-01", "2025-12-02"]
    """
    if not holiday_list:
        return False
    return date_value.isoformat() in holiday_list


def working_days_between(start: dt.date, end: dt.date) -> int:
    """
    Count working days (Mon–Fri) between two dates.
    """
    if end < start:
        return 0
    delta = (end - start).days
    count = 0
    for i in range(delta):
        day = (start + dt.timedelta(days=i)).weekday()
        if day < 5:  # Mon–Fri
            count += 1
    return count


# ============================================================
# AMOUNT / DECIMAL UTILITIES
# ============================================================

def to_decimal(v: Any) -> Decimal:
    """
    Safely convert input to Decimal.
    Returns Decimal("0") on failure.
    """
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal("0")
    except Exception:
        return Decimal("0")


def percent_of(base: Any, pct: Any) -> Decimal:
    """
    Return pct% of base amount.
    Example: percent_of(100, 20) => 20
    """
    b = to_decimal(base)
    p = to_decimal(pct) / Decimal("100")
    return b * p


# ============================================================
# JSON UTILITIES
# ============================================================

def safe_json(obj: Any) -> str:
    """
    Serialize to JSON safely, returning '{}' on failure.
    Useful for audit logs.
    """
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return "{}"


def parse_json(raw: Any) -> Dict[str, Any]:
    """
    Safely parse JSON string → dict.
    Returns {} if invalid or empty.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ============================================================
# STRING UTILITIES
# ============================================================

def norm_str(v: Any) -> str:
    """
    Normalize string: strip → lowercase.
    """
    return v.strip().lower() if isinstance(v, str) else ""


def empty_if_none(v: Optional[str]) -> str:
    """
    Convert None to empty string.
    """
    return v or ""


# ============================================================
# SAFE ACCESS / GENERIC HELPERS
# ============================================================

def get_or_default(obj: Dict[str, Any], key: str, default: Any = None) -> Any:
    """
    Safe dictionary access with default.
    """
    try:
        value = obj.get(key, default)
        return value if value not in (None, "") else default
    except Exception:
        return default


def try_float(v: Any, default: float = 0.0) -> float:
    """
    Safely convert input to float.
    """
    try:
        return float(v)
    except Exception:
        return default


def try_int(v: Any, default: int = 0) -> int:
    """
    Safely convert input to int.
    """
    try:
        return int(v)
    except Exception:
        return default
