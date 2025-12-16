"""
Data-access helpers for the Policy & Risk Guardrails PoC.

This module keeps the PoC simple by using:
- Async SQLAlchemy engine/session (see app/db/session.py)
- Lightweight SQL with JSONB where convenient
- Minimal assumptions about upstream ERP schemas

Tables expected (created by db/sql/001_policy_guard_poc.sql):
- policy_rule_packs (org_id, rules JSONB, is_active, created_at, ...)
- budgets_monthly (org_id, category_code, month_start, amount, currency)
- org_holidays (org_id, holiday_date)
- policy_audit_log (... see DDL)
- poc_transactions (optional demo table used by MTD/duplicate queries)

NOTE:
If your real transactions table differs, update the queries accordingly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from rapidfuzz import fuzz
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session


# ---------------------------
# Utilities
# ---------------------------

WEEKDAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _ensure_decimal(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


# ---------------------------
# Repository
# ---------------------------

class PolicyRepos:
    """
    Encapsulates DB queries and small business utilities needed by rules & engine.
    """

    # ---------- Rule pack ----------

    async def load_rulepack(self, org_id: str) -> Dict[str, Any]:
        """
        Return the active rule pack (JSON) for an org. Empty dict if none.
        """
        q = text(
            """
            SELECT rules
            FROM policy_rule_packs
            WHERE org_id = :org_id AND is_active = TRUE
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        async with async_session() as s:  
            row = (await s.execute(q, {"org_id": org_id})).first()
            return dict(row[0]) if row and row[0] is not None else {}

    # ---------- Calendar ----------

    async def is_holiday(self, org_id: str, d: date) -> bool:
        q = text(
            """
            SELECT 1
            FROM org_holidays
            WHERE org_id = :org_id AND holiday_date = :day
            LIMIT 1
            """
        )
        async with async_session() as s:
            row = (await s.execute(q, {"org_id": org_id, "day": d})).first()
            return bool(row)

    async def is_weekend(self, d: date, weekend_cfg: Dict[str, Any]) -> bool:
        """
        UAE default weekend is FRI/SAT. This reads from rulepack.weekend.days if present.
        """
        days = set((weekend_cfg or {}).get("days", ["FRI", "SAT"]))
        day_name = WEEKDAY_NAMES[d.weekday()]
        return day_name in days

    # ---------- Budgets ----------

    async def resolve_budget(self, org_id: str, category_code: str, d: date) -> Optional[Decimal]:
        """
        Return the monthly budget amount for (org, category, month_start) if configured.
        """
        ms = _month_start(d)
        q = text(
            """
            SELECT amount
            FROM budgets_monthly
            WHERE org_id = :org_id
              AND category_code = :cat
              AND month_start = :ms
            LIMIT 1
            """
        )
        async with async_session() as s:
            row = (await s.execute(q, {"org_id": org_id, "cat": category_code, "ms": ms})).first()
            return _ensure_decimal(row[0]) if row else None

    async def mtd_spend(self, org_id: str, user_id: str, category_code: str, d: date) -> Decimal:
        """
        Sum of poc_transactions.amount for the month up to and including `d`.
        """
        ms = _month_start(d)
        q = text(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM poc_transactions
            WHERE org_id = :org_id
              AND user_id = :uid
              AND category_code = :cat
              AND date >= :ms AND date <= :d
            """
        )
        async with async_session() as s:
            row = (await s.execute(q, {"org_id": org_id, "uid": user_id, "cat": category_code, "ms": ms, "d": d})).first()
            return _ensure_decimal(row[0]) if row else Decimal("0")

    # ---------- Duplicate detection ----------

    async def duplicate_suspect(
        self,
        org_id: str,
        user_id: str,
        tx: Dict[str, Any],
        dup_rules: Dict[str, Any],
    ) -> bool:
        """
        Simple duplicate heuristic:
          - within N days window (default 14)
          - same amount within ±tolerance (default 1.00 in tx currency)
          - description similarity ≥ threshold (default 0.85 via token_set ratio)
          - within same-day (24h) bucket for PoC (we only have 'date'; treat equal date as same bucket)
        """
        window_days = int(dup_rules.get("window_days", 14))
        amount_tol = Decimal(str(dup_rules.get("same_amount_tolerance", 1.00)))
        desc_threshold = float(dup_rules.get("desc_fuzzy_threshold", 0.85))
        # time_bucket_hours not applied strictly (PoC has date granularity); approximate with same date

        tx_date: date = tx["date"]
        tx_amount = _ensure_decimal(tx["amount"])
        tx_desc = (tx.get("description") or "").strip()

        q = text(
            """
            SELECT amount, description, date
            FROM poc_transactions
            WHERE org_id = :org_id
              AND user_id = :uid
              AND date >= :since
              AND date <= :until
            ORDER BY date DESC
            """
        )
        since = tx_date - timedelta(days=window_days)
        until = tx_date
        async with async_session() as s:
            rows = (await s.execute(q, {"org_id": org_id, "uid": user_id, "since": since, "until": until})).all()

        for amt, desc, d in rows:
            amt = _ensure_decimal(amt)
            desc = (desc or "").strip()
            # Same-day bucket (approximation)
            same_day = (d == tx_date)
            # Amount within tolerance
            amount_close = abs(amt - tx_amount) <= amount_tol
            # Description similarity 0..1
            sim = fuzz.token_set_ratio(tx_desc, desc) / 100.0 if tx_desc and desc else 0.0
            if amount_close and same_day and sim >= desc_threshold:
                return True
        return False

    # ---------- FX & tip outliers ----------

    async def fx_deviation_exceeds(self, tx: Dict[str, Any], fx_rules: Dict[str, Any]) -> bool:
        """
        Minimal FX outlier detector:
        - If currency is non-AED and tx.fx_rate is present,
          compare to a simple reference map or rulepack-provided reference.
        - Flag if deviation > max_deviation_pct (default 5%).
        """
        currency = (tx.get("currency") or "").upper()
        fx_rate = tx.get("fx_rate")
        if not currency or currency == "AED" or not fx_rate:
            return False

        max_dev_pct = float(fx_rules.get("max_deviation_pct", 5.0)) / 100.0

        # Prefer rulepack-provided reference rates: fx_rules.reference = {"USD": 3.6725, "EUR": 4.00, ...}
        ref_map = fx_rules.get("reference", {}) or {}
        # Fallback simple map for demo purposes
        fallback_map = {"USD": 3.6725, "EUR": 4.0000, "GBP": 4.6000}
        ref = ref_map.get(currency) or fallback_map.get(currency)
        if not ref:
            return False

        try:
            ref = float(ref)
            rate = float(fx_rate)
            dev = abs(rate - ref) / ref
            return dev > max_dev_pct
        except Exception:
            return False

    async def tip_exceeds(self, tx: Dict[str, Any], fx_rules: Dict[str, Any]) -> bool:
        tip_max_pct = float(fx_rules.get("tip_max_pct", 20.0)) / 100.0
        amt = _ensure_decimal(tx.get("amount", 0))
        tip = _ensure_decimal(tx.get("tip_amount", 0))
        if amt <= 0:
            return False
        return (tip / amt) > Decimal(str(tip_max_pct))

    # ---------- PO / PCC / approvals (PoC stubs) ----------

    async def po_on_file(self, tx_context: Dict[str, Any]) -> bool:
        """
        PoC stub: integrate with your project system later.
        Return False to enforce PO requirement; True if present.
        """
        # To integrate: check a project table by project_id for a non-null PO ref
        return False

    async def pcc_on_file(self, project_id: Optional[str]) -> bool:
        """
        PoC stub: PCC required before payment in production.
        """
        return False

    async def has_required_approvals(self, kind: str, amount: Decimal) -> bool:
        """
        PoC stub: check routed approvals for freelancer/non-standard expenses.
        """
        return False

    # ---------- Audit ----------

    async def audit_stub(
        self,
        req: Any,
        status: str,
        messages: List[Dict[str, Any]],
        risk: Dict[str, Any],
    ) -> Optional[str]:
        """
        Insert an audit 'stub' row at evaluation time.
        Returns the created id (as text) if successful.
        """
        codes = [m.get("code") for m in messages]
        q = text(
            """
            INSERT INTO policy_audit_log (org_id, tx_id, user_id, event, reason_codes, details)
            VALUES (:org_id, :tx_id, :user_id, :event, :reason_codes, :details::jsonb)
            RETURNING id
            """
        )
        details = {
            "messages": messages,
            "risk": risk,
        }
        params = {
            "org_id": req.org_id,
            "tx_id": None,  # no tx id yet during evaluate
            "user_id": req.user_id,
            "event": status,
            "reason_codes": codes,
            "details": details,
        }
        async with async_session() as s:
            row = (await s.execute(q, params)).first()
            await s.commit()
            return str(row[0]) if row else None

    async def audit_finalize(
        self,
        audit_id: str,
        justification: Optional[str],
        attachments: Iterable[str],
        override_event: str = "OVERRIDE",
    ) -> None:
        """
        Finalize an audit row when the user completes a required action (e.g., attaches approval).
        """
        q = text(
            """
            UPDATE policy_audit_log
            SET event = :event,
                justification = :just,
                details = COALESCE(details, '{}'::jsonb) || jsonb_build_object('attachments', :atts::jsonb)
            WHERE id = :id
            """
        )
        async with async_session() as s:
            await s.execute(q, {"event": override_event, "just": justification, "atts": list(attachments), "id": audit_id})
            await s.commit()
