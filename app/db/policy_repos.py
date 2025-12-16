from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional
from enum import Enum

from sqlalchemy import select, update, insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_policy import policy_rulepacks, policy_audit_log
from app.db.models_policy import policy_rulepacks, policy_audit_log, policy_overrides
from app.utils import is_weekend
from app.core.logger import logger

# Optional fuzzy matcher (RapidFuzz). If not installed, we fall back to simple checks.
try:
    from rapidfuzz.fuzz import token_set_ratio
except Exception:
    token_set_ratio = None


# ----------------------------------------------------------------------
# JSON-safe helper
# ----------------------------------------------------------------------
def _to_json_safe(obj: Any) -> Any:
    """
    Recursively convert Python objects into JSON-serializable structures.

    - date/datetime -> ISO string
    - Decimal       -> float
    - Enum          -> value
    - Pydantic      -> model_dump()
    - dict/list/set/tuple -> walk children
    """
    if obj is None:
        return None

    # Pydantic-style models
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        obj = obj.model_dump()

    if isinstance(obj, (date, datetime)):
        return obj.isoformat()

    if isinstance(obj, Decimal):
        return float(obj)

    if isinstance(obj, Enum):
        return obj.value

    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set)):
        return [_to_json_safe(v) for v in obj]

    return obj


class DBPolicyRepos:
    """
    Dynamic, DB-backed rulepack + helpers for policy engine.

    Responsibilities:
    - load_rulepack(org_id)
    - save_rulepack(org_id, rulepack)
    - audit_stub(...)
    - helpers (used by rules):
        * is_weekend(tx_date, weekend_cfg)
        * is_holiday(tx_date, rulepack)
        * resolve_budget(org_id, category_code, tx_date)
        * mtd_spend(org_id, user_id, category_code, tx_date)
        * duplicate_suspect(org_id, user_id, tx_dict, dup_rules)
        * fx_deviation_exceeds(tx_dict, fx_rules_cfg)
        * tip_exceeds(tx_dict, fx_rules_cfg)
        * po_on_file(context)
        * pcc_on_file(project_id)

    Schemas:
      - policy_rulepacks:
          id | org_id | version | rulepack(JSONB) | created_at | updated_at
      - policy_audit_log:
          id | org_id | user_id | status | risk_score | risk_flags(JSONB)
             | messages(JSONB) | tx(JSONB) | created_at
    """

    def __init__(self, db: AsyncSession):
        # AsyncSession is injected per-request
        self.db = db

    # ----------------------------------------------------------------------
    # Load rulepack
    # ----------------------------------------------------------------------
    async def load_rulepack(self, org_id: str) -> Dict[str, Any]:
        """
        Load the rulepack for this organization.

        If not found → return a safe minimal default.
        """
        try:
            stmt = select(policy_rulepacks.c.rulepack).where(
                policy_rulepacks.c.org_id == org_id
            )
            res = await self.db.execute(stmt)
            rp = res.scalar_one_or_none()

            if rp is None:
                logger.warning(
                    f"[policy] No rulepack found for org='{org_id}'. Using default."
                )
                return self.default_rulepack()

            logger.info(f"[policy] Loaded rulepack for org='{org_id}'.")
            return rp

        except Exception as e:
            logger.error(f"[policy] ERROR loading rulepack for org='{org_id}': {e}")
            return self.default_rulepack()

    # ----------------------------------------------------------------------
    # Save / update rulepack
    # ----------------------------------------------------------------------
    async def save_rulepack(
        self, org_id: str, rulepack: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Insert or update an org rulepack.
        - Auto-increments version if updating.
        - Returns the saved rulepack JSON.
        """
        try:
            stmt = select(policy_rulepacks).where(policy_rulepacks.c.org_id == org_id)
            res = await self.db.execute(stmt)
            row = res.first()

            # -----------------------
            # UPDATE
            # -----------------------
            if row:
                new_version = (row.version or 0) + 1

                upd_stmt = (
                    update(policy_rulepacks)
                    .where(policy_rulepacks.c.org_id == org_id)
                    .values(
                        rulepack=rulepack,
                        version=new_version,
                        updated_at=text("NOW()"),
                    )
                    .returning(policy_rulepacks.c.rulepack)
                )

                updated = await self.db.execute(upd_stmt)
                await self.db.commit()

                logger.info(
                    f"[policy] Updated rulepack org={org_id} → version={new_version}"
                )
                return updated.scalar_one()

            # -----------------------
            # INSERT
            # -----------------------
            ins_stmt = (
                insert(policy_rulepacks)
                .values(
                    org_id=org_id,
                    rulepack=rulepack,
                    version=1,
                )
                .returning(policy_rulepacks.c.rulepack)
            )

            created = await self.db.execute(ins_stmt)
            await self.db.commit()

            logger.info(f"[policy] Created new rulepack org={org_id} → version=1")
            return created.scalar_one()

        except Exception as e:
            logger.error(f"[policy] save_rulepack failed for org={org_id}: {e}")
            raise

    # ----------------------------------------------------------------------
    # Audit logging
    # ----------------------------------------------------------------------
    async def audit_stub(self, **kwargs) -> None:
        """
        Persist one audit row per evaluation.

        Expected kwargs from PolicyEngine._safe_audit_stub:
          - req: EvalRequest
          - status: str
          - messages: List[dict]   (PolicyMessage.model_dump())
          - risk: { "score": int, "flags": [dict, ...] }

        This is best-effort: failure is logged but never raised.
        """
        try:
            req = kwargs.get("req")
            status = kwargs.get("status", "UNKNOWN")
            messages_raw = kwargs.get("messages") or []
            risk = kwargs.get("risk") or {}

            org_id = getattr(req, "org_id", None) or "unknown"
            user_id = getattr(req, "user_id", None)

            # tx payload → JSON-able dict
            tx_obj = getattr(req, "tx", None)
            if tx_obj is None:
                tx_data: Dict[str, Any] = {}
            elif hasattr(tx_obj, "model_dump"):
                tx_data = tx_obj.model_dump()
            elif isinstance(tx_obj, dict):
                tx_data = tx_obj
            else:
                # Fallback to string representation if weird type
                tx_data = {"_raw": str(tx_obj)}

            # Risk score + flags
            try:
                risk_score = int(risk.get("score", 0))
            except Exception:
                risk_score = 0
            risk_flags_raw = risk.get("flags") or []

            # Make everything JSON-serializable
            messages = _to_json_safe(messages_raw)
            risk_flags = _to_json_safe(risk_flags_raw)
            tx_json = _to_json_safe(tx_data)

            stmt = insert(policy_audit_log).values(
                org_id=org_id,
                user_id=user_id,
                status=status,
                risk_score=risk_score,
                risk_flags=risk_flags,
                messages=messages,
                tx=tx_json,
            )
            await self.db.execute(stmt)
            await self.db.commit()
        except Exception as e:
            logger.error(f"[policy] audit_stub failed: {e}")
            # Do NOT raise; audit must not block user flows
            return

    # ----------------------------------------------------------------------
    # Weekend helper
    # ----------------------------------------------------------------------
    async def is_weekend(self, tx_date: date, weekend_cfg: Dict[str, Any]) -> bool:
        """
        Shared utility helper used by weekend rules.
        Delegates to app.utils.is_weekend().
        """
        try:
            return is_weekend(tx_date, weekend_cfg)
        except Exception as e:
            logger.error(f"[policy] is_weekend failed: {e}")
            return False

    # ----------------------------------------------------------------------
    # Holiday helper (used by holiday_flag & travel_leadtime)
    # ----------------------------------------------------------------------
    async def is_holiday(self, tx_date: date, rulepack: Dict[str, Any]) -> bool:
        """
        Simple holiday check based on rulepack['holiday_dates'].

        - tx_date: datetime.date or datetime
        - rulepack: full org rulepack JSON

        Returns True if tx_date.isoformat() is listed in holiday_dates.
        """
        try:
            if isinstance(tx_date, datetime):
                d_str = tx_date.date().isoformat()
            elif isinstance(tx_date, date):
                d_str = tx_date.isoformat()
            else:
                return False

            if not isinstance(rulepack, dict):
                return False

            holiday_dates = (rulepack.get("holiday_dates") or []) or []
            if not isinstance(holiday_dates, list):
                return False

            return d_str in holiday_dates

        except Exception as e:
            logger.error(f"[policy] is_holiday failed: {e}")
            return False

    # ----------------------------------------------------------------------
    # Monthly budget helper (used by monthly_budget rule)
    # ----------------------------------------------------------------------
    async def resolve_budget(
        self,
        org_id: str,
        category_code: str,
        tx_date: date,
    ) -> Optional[Decimal]:
        """
        Resolve monthly budget limit for a transaction (DB-first).

        Current PoC:
        - No budgets table yet → always returns None,
          so monthly_budget rule falls back to rulepack.budgets.monthly_limits
          or treats it as "no limit".
        - Signature matches monthly_budget.check(...) usage.

        When you wire a budgets_monthly table, implement:
          SELECT limit
          FROM erp_ai.budgets_monthly
          WHERE org_id = ...
            AND category_code = ...
            AND month_start = first day of tx_date's month
        """
        try:
            # TODO: Implement DB lookup here later.
            return None
        except Exception as e:
            logger.error(f"[policy] resolve_budget failed for org={org_id}: {e}")
            return None

    # ----------------------------------------------------------------------
    # MTD spend helper (used by monthly_budget rule)
    # ----------------------------------------------------------------------
    async def mtd_spend(
        self,
        org_id: str,
        user_id: str,
        category_code: str,
        tx_date: date,
    ) -> Decimal:
        """
        Month-to-date spend helper for the monthly_budget rule.

        Current PoC:
        - Returns Decimal('0.00') so rules can run without a tx history table.
        - monthly_budget then effectively compares just this tx against the limit.

        Later:
        - Sum from an erp_ai.<expenses> / tx_history table where:
            org_id, user_id, category_code,
            and date in same calendar month as tx_date.
        """
        try:
            # TODO: Implement real MTD aggregation later.
            return Decimal("0.00")
        except Exception as e:
            logger.error(f"[policy] mtd_spend failed for org={org_id}: {e}")
            return Decimal("0.00")

    # ----------------------------------------------------------------------
    # Duplicate detection helper (used by duplicates rule)
    # ----------------------------------------------------------------------
    async def duplicate_suspect(
        self,
        org_id: str,
        user_id: str,
        tx: Dict[str, Any],
        dup_rules: Dict[str, Any],
    ) -> bool:
        """
        Heuristic duplicate detection for expense claims.

        Expected tx dict fields:
          - date: date | datetime | ISO string (YYYY-MM-DD)
          - amount: numeric
          - description: str

        dup_rules (from rulepack.duplicate_rules):
          - window_days: int (lookback window in days, default 14)
          - same_amount_tolerance: float (± tolerance in tx units, default 1.0)
          - desc_fuzzy_threshold: float in [0,1] (default 0.85)
          - time_bucket_hours: int (not used; we approximate via same-date bucket in PoC)

        Logic:
          1) Convert tx.date to a date and amount to Decimal.
          2) Query erp_ai.poc_transactions for rows:
                - same org_id & user_id
                - tx_date between [tx_date - window_days, tx_date]
                - ABS(amount - tx.amount) <= tolerance
          3) For each candidate:
                - if RapidFuzz is available → use token_set_ratio/100
                - else → simple normalized string equality / containment
             If similarity >= desc_fuzzy_threshold → treat as suspect duplicate.

        Returns:
          True  -> suspect duplicate found
          False -> no suspect duplicate
        """
        try:
            if not tx:
                return False

            raw_date = tx.get("date")
            raw_amount = tx.get("amount")
            raw_desc = (tx.get("description") or "").strip()

            if not raw_date or raw_amount in (None, "", 0):
                # Not enough info to judge duplicates
                return False

            # --- 1) Normalize date ------------------------------------------------
            tx_date: Optional[date]
            if isinstance(raw_date, date) and not isinstance(raw_date, datetime):
                tx_date = raw_date
            elif isinstance(raw_date, datetime):
                tx_date = raw_date.date()
            elif isinstance(raw_date, str):
                try:
                    tx_date = datetime.fromisoformat(raw_date[:10]).date()
                except Exception:
                    return False
            else:
                return False

            # --- 2) Normalize amount ---------------------------------------------
            try:
                amount = Decimal(str(raw_amount))
            except Exception:
                return False

            # --- 3) Extract duplicate-rule knobs ---------------------------------
            window_days = int(dup_rules.get("window_days", 14) or 14)
            # same_amount_tolerance in *currency units* (e.g., AED)
            try:
                tol = Decimal(str(dup_rules.get("same_amount_tolerance", 1.0)))
            except Exception:
                tol = Decimal("1.0")

            # fuzzy description threshold in [0,1]
            try:
                desc_threshold = float(dup_rules.get("desc_fuzzy_threshold", 0.85))
            except Exception:
                desc_threshold = 0.85

            # PoC approximation: same-date bucket via BETWEEN from/to
            from_date = tx_date - timedelta(days=window_days)
            to_date = tx_date

            # --- 4) Query candidate transactions from history --------------------
            # Expected PoC table schema (you can adjust to your actual table):
            #   erp_ai.poc_transactions(
            #       id BIGINT,
            #       org_id VARCHAR,
            #       user_id VARCHAR,
            #       tx_date DATE,
            #       amount NUMERIC,
            #       description TEXT,
            #       ...
            #   )
            sql = text(
                """
                SELECT
                    id,
                    tx_date,
                    amount,
                    description
                FROM erp_ai.poc_transactions
                WHERE org_id = :org_id
                  AND user_id = :user_id
                  AND tx_date BETWEEN :from_date AND :to_date
                  AND ABS(amount - :amount) <= :tol
                """
            )

            res = await self.db.execute(
                sql,
                {
                    "org_id": org_id,
                    "user_id": user_id,
                    "from_date": from_date,
                    "to_date": to_date,
                    "amount": amount,
                    "tol": tol,
                },
            )
            rows = res.fetchall()

            if not rows:
                return False

            # --- 5) Fuzzy/heuristic description comparison ----------------------
            def _norm(s: str) -> str:
                return " ".join(s.lower().split())

            desc_norm = _norm(raw_desc) if raw_desc else ""

            # If no description at all, treat amount+date as insufficient to flag duplicate
            if not desc_norm:
                return False

            for row in rows:
                cand_desc = row.description if hasattr(row, "description") else row[3]
                cand_desc_norm = _norm(cand_desc or "")

                if not cand_desc_norm:
                    continue

                # Try RapidFuzz if available
                if token_set_ratio is not None:
                    try:
                        score = token_set_ratio(desc_norm, cand_desc_norm) / 100.0
                    except Exception:
                        score = 0.0
                else:
                    # Fallback: simple equality / containment heuristic
                    if desc_norm == cand_desc_norm:
                        score = 1.0
                    elif desc_norm in cand_desc_norm or cand_desc_norm in desc_norm:
                        score = 0.9
                    else:
                        score = 0.0

                if score >= desc_threshold:
                    logger.info(
                        "[policy] duplicate_suspect: found candidate id=%s score=%.3f "
                        "(tx_date=%s, amount=%s)",
                        getattr(row, "id", None),
                        score,
                        getattr(row, "tx_date", None),
                        getattr(row, "amount", None),
                    )
                    return True

            return False

        except Exception as e:
            logger.error(f"[policy] duplicate_suspect failed for org={org_id}: {e}")
            return False


    # ----------------------------------------------------------------------
    # FX deviation helper (used by fx_tip_outliers rule)
    # ----------------------------------------------------------------------
    async def fx_deviation_exceeds(
        self,
        tx: Dict[str, Any],
        fx_cfg: Dict[str, Any],
    ) -> bool:
        """
        Check if applied FX rate deviates too much from reference.

        tx: dict from tx.model_dump()
        fx_cfg: rulepack['fx_rules']

        Logic (PoC):
        - If currency == 'AED' or fx_rate missing → False
        - ref = fx_cfg.reference[currency] if present, else no-op (False)
        - deviation_pct = abs(fx_rate - ref) / ref * 100
        - if deviation_pct > max_deviation_pct → True
        """
        try:
            currency = (tx.get("currency") or "").upper()
            fx_rate_raw = tx.get("fx_rate")
            if not currency or fx_rate_raw in (None, "", 0):
                return False
            if currency == "AED":
                return False

            try:
                fx_rate = Decimal(str(fx_rate_raw))
            except Exception:
                return False

            ref_map = (fx_cfg.get("reference") or {}) if isinstance(fx_cfg, dict) else {}
            ref_raw = ref_map.get(currency)
            if ref_raw is None:
                # No reference → cannot judge deviation in PoC
                return False

            try:
                ref = Decimal(str(ref_raw))
            except Exception:
                return False
            if ref <= 0:
                return False

            max_dev_pct = Decimal(str(fx_cfg.get("max_deviation_pct", 5.0)))
            deviation_pct = (abs(fx_rate - ref) / ref) * Decimal("100")

            return deviation_pct > max_dev_pct
        except Exception as e:
            logger.error(f"[policy] fx_deviation_exceeds failed: {e}")
            return False

    # ----------------------------------------------------------------------
    # Tip outlier helper (used by fx_tip_outliers rule)
    # ----------------------------------------------------------------------
    async def tip_exceeds(
        self,
        tx: Dict[str, Any],
        fx_cfg: Dict[str, Any],
    ) -> bool:
        """
        Check if tip_amount exceeds allowed percentage of amount.

        tx: dict from tx.model_dump()
        fx_cfg: rulepack['fx_rules']

        Logic:
        - If tip_amount or amount missing/zero → False
        - tip_pct = tip_amount / amount * 100
        - if tip_pct > tip_max_pct → True
        """
        try:
            amount_raw = tx.get("amount")
            tip_raw = tx.get("tip_amount")

            if amount_raw in (None, "", 0) or tip_raw in (None, "", 0):
                return False

            try:
                amount = Decimal(str(amount_raw))
                tip = Decimal(str(tip_raw))
            except Exception:
                return False

            if amount <= 0 or tip <= 0:
                return False

            tip_max_pct = Decimal(str(fx_cfg.get("tip_max_pct", 20.0)))
            tip_pct = (tip / amount) * Decimal("100")

            return tip_pct > tip_max_pct
        except Exception as e:
            logger.error(f"[policy] tip_exceeds failed: {e}")
            return False

    # ----------------------------------------------------------------------
    # PO / PCC helpers (used by po_required & pcc_required rules)
    # ----------------------------------------------------------------------
    async def po_on_file(self, context: Dict[str, Any]) -> bool:
        """
        PoC helper: does this context have a PO on file?

        Current behavior:
        - If context contains 'po_number' or 'po_id' → True
        - Else → False

        Wire this to your real project / procurement store later.
        """
        try:
            po_number = context.get("po_number") or context.get("po_id")
            return bool(po_number)
        except Exception as e:
            logger.error(f"[policy] po_on_file failed: {e}")
            return False

    async def pcc_on_file(self, project_id: Optional[str]) -> bool:
        """
        PoC helper: does this project have PCC / compliance doc on file?

        Current behavior:
        - Always returns False (so rule hard-blocks when enabled & stage=payment).
        - Adjust when you wire real compliance data.

        You can temporarily relax by returning bool(project_id).
        """
        try:
            # TODO: Implement real PCC lookup (project_id -> compliance table).
            return False
        except Exception as e:
            logger.error(f"[policy] pcc_on_file failed for project_id={project_id}: {e}")
            return False

    # ----------------------------------------------------------------------
    # Default fallback rulepack
    # ----------------------------------------------------------------------
    def default_rulepack(self) -> Dict[str, Any]:
        """
        Fallback rulepack when no custom rulepack is uploaded.

        Safe minimal behaviour:
        - Basic required fields
        - Basic weekend detection (Fri/Sat)
        - No advanced caps, FX, MCC, budgets unless configured later
        """
        return {
            "required_fields": ["date", "amount", "currency", "category_code"],

            "holidays": {"policy": "flag"},
            "holiday_dates": [],

            "weekend": {
                "enabled": True,
                "days": ["FRI", "SAT"],
                "require_approval_for": [],
            },

            "caps": {
                "per_tx": {},
                "per_tx_weekend": {},
            },

            "budgets": {
                "monthly_limits": {},
            },

            "mcc_rules": {
                "category_allowed_mcc": {},
            },

            "fx_rules": {
                "max_deviation_pct": 5.0,
                "tip_max_pct": 20.0,
            },

            "forex_card": {
                "enabled": False,
                "allowed_categories": [],
            },

            "travel": {
                "min_notice_working_days": 7,
                "upgrades_require_approval": False,
            },

            "project_docs": {
                "po_required_for": [],
                "pcc_required_for_payment": False,
            },

            "receipts": {
                "enabled": True,
                "min_count": 1,
                "hard_block": True,
            },

            "risk_weights": {},
        }

    
    # ----------------------------------------------------------------------
    # Override logging
    # ----------------------------------------------------------------------
    async def log_override(
        self,
        *,
        org_id: str,
        user_id: str,
        action: str,
        reason: str,
        audit_id: Optional[int] = None,
        actor_role: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Record an override event for a policy evaluation.

        Inputs:
          - org_id:       Organization id (for safety / filtering)
          - user_id:      The user performing the override (manager/finance/etc.)
          - action:       e.g. "APPROVE", "REJECT", "FORCE_SUBMIT"
          - reason:       Free-text justification
          - audit_id:     Optional link to policy_audit_log.id
          - actor_role:   Optional label ("MANAGER", "FINANCE", "ADMIN", ...)
          - meta:         Optional extra JSON (e.g. UI version, IP, etc.)

        Behavior:
          - If audit_id is provided, we look up the policy_audit_log row to get
            original_status and (if not SOFT_BLOCK) we still allow an override.
          - new_status is derived as:
                SOFT_BLOCK + APPROVE -> "OVERRIDDEN_APPROVED"
                SOFT_BLOCK + REJECT  -> "OVERRIDDEN_REJECTED"
                HARD_BLOCK + APPROVE -> "OVERRIDDEN_APPROVED"
                otherwise            -> "OVERRIDDEN_" + action.upper()
          - Returns a small dict with override_id + statuses.
        """
        original_status = "UNKNOWN"

        # If an audit row is referenced, fetch it for original_status
        if audit_id is not None:
            try:
                stmt = (
                    select(
                        policy_audit_log.c.org_id,
                        policy_audit_log.c.status,
                        policy_audit_log.c.risk_score,
                    )
                    .where(policy_audit_log.c.id == audit_id)
                )
                res = await self.db.execute(stmt)
                row = res.first()
                if row:
                    # Optional safety: override org_id from the audit row if you want
                    # org_id = row.org_id
                    original_status = row.status or "UNKNOWN"
                else:
                    logger.warning(
                        "[policy] log_override: audit_id=%s not found; "
                        "proceeding with original_status=UNKNOWN",
                        audit_id,
                    )
            except Exception as e:
                logger.error(
                    "[policy] log_override: failed to read audit_id=%s: %s",
                    audit_id,
                    e,
                )

        # Normalize action & derive new_status
        action_norm = (action or "").strip().upper() or "UNKNOWN"
        orig = (original_status or "").strip().upper() or "UNKNOWN"

        if action_norm == "APPROVE":
            new_status = "OVERRIDDEN_APPROVED"
        elif action_norm == "REJECT":
            new_status = "OVERRIDDEN_REJECTED"
        else:
            new_status = f"OVERRIDDEN_{action_norm}"

        meta_json = _to_json_safe(meta or {})

        try:
            stmt = (
                insert(policy_overrides)
                .values(
                    audit_id=audit_id,
                    org_id=org_id,
                    user_id=user_id,
                    actor_role=actor_role,
                    original_status=orig,
                    new_status=new_status,
                    action=action_norm,
                    reason=reason,
                    meta=meta_json,
                )
                .returning(
                    policy_overrides.c.id,
                    policy_overrides.c.audit_id,
                    policy_overrides.c.org_id,
                    policy_overrides.c.user_id,
                    policy_overrides.c.original_status,
                    policy_overrides.c.new_status,
                    policy_overrides.c.action,
                    policy_overrides.c.created_at,
                )
            )

            res = await self.db.execute(stmt)
            await self.db.commit()
            row = res.mappings().one()

            logger.info(
                "[policy] override logged: id=%s org=%s audit_id=%s original=%s new=%s action=%s",
                row["id"],
                row["org_id"],
                row["audit_id"],
                row["original_status"],
                row["new_status"],
                row["action"],
            )

            return {
                "override_id": row["id"],
                "audit_id": row["audit_id"],
                "org_id": row["org_id"],
                "user_id": row["user_id"],
                "original_status": row["original_status"],
                "new_status": row["new_status"],
                "action": row["action"],
                "created_at": row["created_at"].isoformat()
                if row["created_at"]
                else None,
            }

        except Exception as e:
            logger.error(f"[policy] log_override insert failed: {e}")
            # Do not raise, in case you want overrides to be best-effort.
            # If you WANT strict behavior, replace with "raise".
            return {
                "override_id": None,
                "error": str(e),
            }