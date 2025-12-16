"""
StaticPolicyRepos — no-DB repository for the Policy & Risk Guardrails PoC.

- Loads a single JSON rule pack from disk (POLICY_RULEPACK_PATH or app/policy_static/rulepack.json).
- Provides minimal lookups so all rules can run without a database.
- Reuses helper methods from PolicyRepos (fx/tip checks, weekend calc, etc.).

Usage:
  # PowerShell
  $env:POLICY_RULEPACK_PATH="app/policy_static/rulepack.json"
  uvicorn app.main:app --reload
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from app.domain.repos import PolicyRepos  # we inherit helper methods from here


RULEPACK_PATH_ENV = "POLICY_RULEPACK_PATH"
DEFAULT_RULEPACK_PATH = os.path.join("app", "policy_static", "rulepack.json")


class StaticPolicyRepos(PolicyRepos):
    def __init__(self) -> None:
        path = os.getenv(RULEPACK_PATH_ENV, DEFAULT_RULEPACK_PATH)
        with open(path, "r", encoding="utf-8") as f:
            self._rulepack: Dict[str, Any] = json.load(f)

        # Precompute holiday dates from rulepack.holiday_dates (ISO strings)
        hols: Set[date] = set()
        for s in (self._rulepack.get("holiday_dates") or []):
            try:
                hols.add(datetime.fromisoformat(str(s)[:10]).date())
            except Exception:
                continue
        self._holiday_dates = hols

    # ------------------ rulepack / lookups ------------------

    async def load_rulepack(self, org_id: str) -> Dict[str, Any]:
        # Single org for PoC; ignore org_id and return the loaded rule pack
        return self._rulepack

    async def is_holiday(self, org_id: str, d: date) -> bool:
        return d in self._holiday_dates

    async def resolve_budget(self, org_id: str, category_code: str, d: date) -> Optional[Decimal]:
        # No DB budgets in static mode; monthly_budget rule will use rulepack fallback
        return None

    async def mtd_spend(self, org_id: str, user_id: str, category_code: str, d: date) -> Decimal:
        # No transaction history in static mode → 0 (so a single big tx can exceed the limit)
        return Decimal("0")

    async def duplicate_suspect(
        self,
        org_id: str,
        user_id: str,
        tx: Dict[str, Any],
        dup_rules: Dict[str, Any],
    ) -> bool:
        # No lookback table in static mode
        return False

    async def po_on_file(self, context: Dict[str, Any]) -> bool:
        # Force requirement in PoC unless you wire an actual lookup
        return False

    async def pcc_on_file(self, project_id: Optional[str]) -> bool:
        # Force requirement in PoC unless you wire an actual lookup
        return False

    async def audit_stub(
        self,
        req: Any,
        status: str,
        messages: List[Dict[str, Any]],
        risk: Dict[str, Any],
    ) -> Optional[str]:
        # No DB write in static mode; return a dummy audit id
        return "static-audit-id"
