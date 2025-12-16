# app/routers/policy_audit.py

from __future__ import annotations

from datetime import datetime, date
from typing import Any, Dict, List, Optional, DefaultDict
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.db.models_policy import policy_audit_log, policy_overrides
from app.core.logger import logger

router = APIRouter(prefix="/policy/audit", tags=["policy-audit"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class PolicyOverrideEntry(BaseModel):
    id: int
    audit_id: int
    org_id: str
    user_id: Optional[str] = None
    action: str
    original_status: Optional[str] = None
    new_status: Optional[str] = None
    reason: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PolicyAuditEntry(BaseModel):
    id: int
    org_id: str
    user_id: Optional[str] = None
    status: str
    risk_score: int
    risk_flags: List[Dict[str, Any]] = Field(default_factory=list)
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    tx: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    overrides: List[PolicyOverrideEntry] = Field(default_factory=list)


class PolicyAuditListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[PolicyAuditEntry]


# ---------------------------------------------------------------------------
# Helper: parse optional date strings
# ---------------------------------------------------------------------------

def _parse_date(d: Optional[str]) -> Optional[datetime]:
    if not d:
        return None
    try:
        # Accept YYYY-MM-DD or full ISO; normalise to datetime
        if len(d) <= 10:
            return datetime.fromisoformat(d + "T00:00:00")
        return datetime.fromisoformat(d)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# GET /policy/audit  - list with filters
# ---------------------------------------------------------------------------

@router.get("", response_model=PolicyAuditListResponse)
async def list_policy_audit(
    org_id: str = Query(..., description="Organisation ID"),
    user_id: Optional[str] = Query(None, description="Filter by user_id"),
    status: Optional[str] = Query(
        None,
        description="Filter by status (PASS / SOFT_BLOCK / HARD_BLOCK)",
    ),
    risk_code: Optional[str] = Query(
        None,
        description="Filter by risk flag code (e.g. DUPLICATE, WEEKEND, FX_OUTLIER)",
    ),
    date_from: Optional[str] = Query(
        None,
        description="Filter from created_at (inclusive), format YYYY-MM-DD or ISO8601",
    ),
    date_to: Optional[str] = Query(
        None,
        description="Filter to created_at (inclusive), format YYYY-MM-DD or ISO8601",
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_session),
):
    """
    Finance / Recon list view over policy evaluations.

    Returns paginated audit rows with optional filters:
      - org_id (required)
      - user_id
      - status
      - risk_code
      - date_from / date_to
    """
    # --- Build base WHERE clause --------------------------------------------
    conditions = [policy_audit_log.c.org_id == org_id]

    if user_id:
        conditions.append(policy_audit_log.c.user_id == user_id)

    if status:
        conditions.append(policy_audit_log.c.status == status)

    # Date range filters
    dt_from = _parse_date(date_from)
    dt_to = _parse_date(date_to)

    if dt_from:
        conditions.append(policy_audit_log.c.created_at >= dt_from)
    if dt_to:
        # Make date_to inclusive by bumping to end-of-day if only date was provided
        if len(date_to or "") <= 10:
            dt_to = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999)
        conditions.append(policy_audit_log.c.created_at <= dt_to)

    # Risk-code filter: simple text search on risk_flags JSONB
    # This is PoC-friendly and works fine for small volumes.
    if risk_code:
        pattern = f'%\"code\": \"{risk_code}\"%'
        conditions.append(
            policy_audit_log.c.risk_flags.cast(String).ilike(pattern)
        )

    where_clause = and_(*conditions)

    # --- Total count ---------------------------------------------------------
    count_stmt = select(func.count()).select_from(policy_audit_log).where(where_clause)
    total_res = await db.execute(count_stmt)
    total: int = total_res.scalar_one() or 0

    if total == 0:
        return PolicyAuditListResponse(
            total=0,
            limit=limit,
            offset=offset,
            items=[],
        )

    # --- Page of results -----------------------------------------------------
    stmt = (
        select(policy_audit_log)
        .where(where_clause)
        .order_by(policy_audit_log.c.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    res = await db.execute(stmt)
    rows = res.mappings().all()

    # Collect audit IDs for overrides lookup
    audit_ids = [row["id"] for row in rows]

    overrides_by_audit: DefaultDict[int, List[PolicyOverrideEntry]] = defaultdict(list)

    if audit_ids:
        ov_stmt = (
            select(policy_overrides)
            .where(policy_overrides.c.audit_id.in_(audit_ids))
            .order_by(policy_overrides.c.created_at.asc())
        )
        ov_res = await db.execute(ov_stmt)
        ov_rows = ov_res.mappings().all()

        for r in ov_rows:
            overrides_by_audit[r["audit_id"]].append(
                PolicyOverrideEntry(
                    id=r["id"],
                    audit_id=r["audit_id"],
                    org_id=r["org_id"],
                    user_id=r.get("user_id"),
                    action=r["action"],
                    original_status=r.get("original_status"),
                    new_status=r.get("new_status"),
                    reason=r.get("reason"),
                    meta=r.get("meta") or {},
                    created_at=r["created_at"],
                )
            )

    items: List[PolicyAuditEntry] = []
    for r in rows:
        items.append(
            PolicyAuditEntry(
                id=r["id"],
                org_id=r["org_id"],
                user_id=r.get("user_id"),
                status=r["status"],
                risk_score=r["risk_score"],
                risk_flags=r.get("risk_flags") or [],
                messages=r.get("messages") or [],
                tx=r.get("tx") or {},
                created_at=r["created_at"],
                overrides=overrides_by_audit.get(r["id"], []),
            )
        )

    return PolicyAuditListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


# ---------------------------------------------------------------------------
# GET /policy/audit/{audit_id} - detail view
# ---------------------------------------------------------------------------

@router.get("/{audit_id}", response_model=PolicyAuditEntry)
async def get_policy_audit(
    audit_id: int,
    db: AsyncSession = Depends(get_session),
):
    """
    Detailed view of a single policy evaluation, including all overrides.
    """
    stmt = select(policy_audit_log).where(policy_audit_log.c.id == audit_id)
    res = await db.execute(stmt)
    row = res.mappings().first()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit row id={audit_id} not found",
        )

    ov_stmt = (
        select(policy_overrides)
        .where(policy_overrides.c.audit_id == audit_id)
        .order_by(policy_overrides.c.created_at.asc())
    )
    ov_res = await db.execute(ov_stmt)
    ov_rows = ov_res.mappings().all()

    overrides: List[PolicyOverrideEntry] = []
    for r in ov_rows:
        overrides.append(
            PolicyOverrideEntry(
                id=r["id"],
                audit_id=r["audit_id"],
                org_id=r["org_id"],
                user_id=r.get("user_id"),
                action=r["action"],
                original_status=r.get("original_status"),
                new_status=r.get("new_status"),
                reason=r.get("reason"),
                meta=r.get("meta") or {},
                created_at=r["created_at"],
            )
        )

    return PolicyAuditEntry(
        id=row["id"],
        org_id=row["org_id"],
        user_id=row.get("user_id"),
        status=row["status"],
        risk_score=row["risk_score"],
        risk_flags=row.get("risk_flags") or [],
        messages=row.get("messages") or [],
        tx=row.get("tx") or {},
        created_at=row["created_at"],
        overrides=overrides,
    )
