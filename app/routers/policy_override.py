from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.db.policy_repos import DBPolicyRepos

router = APIRouter(prefix="/policy", tags=["policy-overrides"])


class OverrideAction(str):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    FORCE_SUBMIT = "FORCE_SUBMIT"


class PolicyOverrideRequest(BaseModel):
    org_id: str = Field(..., description="Org id (e.g., 'plenum-poc')")
    user_id: str = Field(..., description="User performing the override")
    audit_id: Optional[int] = Field(
        None, description="policy_audit_log.id being overridden (optional but recommended)"
    )
    action: Literal["APPROVE", "REJECT", "FORCE_SUBMIT"] = Field(
        ..., description="Override action"
    )
    reason: str = Field(..., min_length=3, description="Short justification")
    actor_role: Optional[str] = Field(
        None, description="MANAGER / FINANCE / ADMIN etc. (optional)"
    )
    meta: Optional[dict] = Field(
        default=None, description="Optional metadata (UI version, IP, etc.)"
    )


class PolicyOverrideResponse(BaseModel):
    override_id: Optional[int]
    audit_id: Optional[int]
    org_id: str
    user_id: str
    original_status: str
    new_status: str
    action: str
    created_at: Optional[str] = None
    error: Optional[str] = None


@router.post(
    "/override",
    response_model=PolicyOverrideResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_policy_override(
    body: PolicyOverrideRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Record an override of a prior policy evaluation.

    Backend expects caller to pass:
      - org_id, user_id
      - audit_id (from previous /policy/evaluate audit log)
      - action: APPROVE/REJECT/FORCE_SUBMIT
      - reason: free-text justification
    """
    repos = DBPolicyRepos(db)

    # (Optional) you can add extra validation here:
    # - check that audit_id exists and belongs to org_id
    # - check that original status is SOFT_BLOCK/HARD_BLOCK
    # For PoC we'll let DBPolicyRepos.log_override do the lookup.
    result = await repos.log_override(
        org_id=body.org_id,
        user_id=body.user_id,
        action=body.action,
        reason=body.reason,
        audit_id=body.audit_id,
        actor_role=body.actor_role,
        meta=body.meta,
    )

    if result.get("override_id") is None and result.get("error"):
        # Best-effort: we still return 201 but you *could* choose 500 here
        # If you want strict behavior, raise HTTPException instead.
        raise HTTPException(
            status_code=500,
            detail=f"Failed to record override: {result['error']}",
        )

    return PolicyOverrideResponse(**result)
