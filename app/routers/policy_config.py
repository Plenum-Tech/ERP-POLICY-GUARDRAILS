from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.policy_repos import DBPolicyRepos
from app.core.logger import logger


router = APIRouter(
    prefix="/policy/config",
    tags=["policy-config"],
)


# -----------------------------------------------------------------------------
# Pydantic models
# -----------------------------------------------------------------------------

class RulepackResponse(BaseModel):
    """
    Wrapper for returning the rulepack for an org.
    We keep it very loose (Dict[str, Any]) so Finance can evolve it over time.
    """
    org_id: str = Field(..., description="Organization ID")
    rulepack: Dict[str, Any] = Field(..., description="Current policy rulepack JSON")


class RulepackPatch(BaseModel):
    """
    Partial update payload.

    Example:
    {
      "caps": {
        "per_tx": { "ENTERTAINMENT": 2000 }
      },
      "budgets": {
        "monthly_limits": { "ENTERTAINMENT": 5000 }
      }
    }

    This will be *deep-merged* into the existing rulepack.
    """
    updates: Dict[str, Any] = Field(
        ...,
        description="Partial rulepack JSON to merge into existing rulepack",
    )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _deep_merge_dicts(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively merge `updates` into `base`.

    - For dict values: merge recursively
    - For all other types: overwrite

    This lets Finance send only the knobs they want to change:
    e.g. { "caps": { "per_tx": { "ENTERTAINMENT": 2000 } } }
    without wiping out other sections like holidays/travel/etc.
    """
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge_dicts(base[key], value)
        else:
            base[key] = value
    return base


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

@router.get("/{org_id}", response_model=RulepackResponse)
async def get_rulepack(
    org_id: str,
    db: AsyncSession = Depends(get_db),
) -> RulepackResponse:
    """
    Get the current rulepack JSON for an org.

    - If a custom rulepack exists in DB → returns that.
    - If not → returns the default rulepack from DBPolicyRepos.default_rulepack().
    """
    repos = DBPolicyRepos(db)
    rulepack = await repos.load_rulepack(org_id)
    return RulepackResponse(org_id=org_id, rulepack=rulepack)


@router.put("/{org_id}", response_model=RulepackResponse, status_code=status.HTTP_200_OK)
async def put_rulepack(
    org_id: str,
    rulepack: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
) -> RulepackResponse:
    """
    Replace the entire rulepack for an org.

    Finance can export / edit a full JSON rulepack and push it here.

    Body should be the full rulepack object, e.g.:

    {
      "version": "1.0",
      "org_id": "plenum-poc",
      "required_fields": [...],
      "weekend": { ... },
      "caps": { ... },
      "budgets": { ... },
      ...
    }
    """
    repos = DBPolicyRepos(db)
    try:
        saved = await repos.save_rulepack(org_id, rulepack)
    except Exception as e:
        logger.error(f"[policy-config] put_rulepack failed for org={org_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save policy rulepack.",
        )
    return RulepackResponse(org_id=org_id, rulepack=saved)


@router.patch("/{org_id}", response_model=RulepackResponse)
async def patch_rulepack(
    org_id: str,
    payload: RulepackPatch,
    db: AsyncSession = Depends(get_db),
) -> RulepackResponse:
    """
    Partially update (deep-merge) the rulepack for an org.

    Example body:

    {
      "updates": {
        "caps": {
          "per_tx": {
            "ENTERTAINMENT": 2000
          }
        },
        "holidays": {
          "policy": "soft_block"
        },
        "holiday_dates": ["2025-12-01", "2025-12-02"]
      }
    }

    - Reads current rulepack via DBPolicyRepos.load_rulepack()
    - Deep-merges payload.updates into it
    - Persists via DBPolicyRepos.save_rulepack()
    """
    repos = DBPolicyRepos(db)

    # Load existing rulepack (DB or default)
    current = await repos.load_rulepack(org_id)
    if not isinstance(current, dict):
        logger.warning(
            f"[policy-config] Existing rulepack for org={org_id} is not a dict; resetting to default."
        )
        current = repos.default_rulepack()

    # Deep-merge updates
    merged = _deep_merge_dicts(dict(current), payload.updates)

    try:
        saved = await repos.save_rulepack(org_id, merged)
    except Exception as e:
        logger.error(f"[policy-config] patch_rulepack failed for org={org_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update policy rulepack.",
        )

    return RulepackResponse(org_id=org_id, rulepack=saved)
