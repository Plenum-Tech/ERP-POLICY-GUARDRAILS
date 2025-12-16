"""
Policy router — HTTP contract for the Policy & Risk Guardrails PoC.

Endpoints:
- POST /policy/evaluate
    → Evaluate a transaction passed inline as EvalRequest
      (status: PASS / SOFT_BLOCK / HARD_BLOCK + risk payload)

- POST /policy/evaluate/from-doc
    → Evaluate a transaction by loading a receipt/invoice from erp_ai.*
      (source = receipt | invoice, doc_id = UUID from receipt_docs / invoice_docs)

- GET  /policy/rulepack/get?org_id=...
    → Fetch per-org rulepack (from DB)

- POST /policy/rulepack/update
    → Update per-org rulepack (Finance-configurable)

- GET  /policy/health
    → Quick health check
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

# Models
from app.models import EvalRequest, EvalResponse, TxInput

# Engines
from app.services.policy_engine import PolicyEngine
from app.services.risk_engine import RiskEngine

# DB session
from app.db.session import get_session as get_db

# NEW: DB-backed per-org rulepack repo
from app.db.policy_repos import DBPolicyRepos

# Adapters
from app.schemas.policy_io import PolicyInput
from app.services.adapters.input_builder import build_policy_input_from_source


router = APIRouter(prefix="/policy", tags=["policy"])


# ---------------------------
# Dependency Providers
# ---------------------------

def get_policy_repo(db: AsyncSession = Depends(get_db)) -> DBPolicyRepos:
    return DBPolicyRepos(db=db)


def get_policy_engine(
    repo: DBPolicyRepos = Depends(get_policy_repo)
) -> PolicyEngine:
    return PolicyEngine(repos=repo, risk_engine=RiskEngine())


# ---------------------------
# Helper: PolicyInput → EvalRequest
# ---------------------------

def policy_input_to_eval_request(pi: PolicyInput) -> EvalRequest:
    """
    Convert PolicyInput → EvalRequest.
    TxInput.date is optional in PolicyInput; PolicyEngine handles missing fields via rulepack.
    """

    tx_model = TxInput(**pi.tx.model_dump())

    return EvalRequest(
        org_id=pi.org_id or "",
        user_id=pi.user_id or "",
        tx=tx_model,
        context=pi.context.additional,
    )


# ---------------------------
# Routes
# ---------------------------

@router.post("/evaluate", response_model=EvalResponse)
async def evaluate(
    req: EvalRequest,
    engine: PolicyEngine = Depends(get_policy_engine),
) -> EvalResponse:
    """
    Evaluate a transaction passed directly by frontend/UI.
    """
    return await engine.evaluate(req)


@router.post("/evaluate/from-doc", response_model=EvalResponse)
async def evaluate_from_doc(
    source: Literal["receipt", "invoice"] = Query(
        ..., description="Document type: receipt | invoice"
    ),
    doc_id: str = Query(
        ..., description="UUID from erp_ai.receipt_docs or erp_ai.invoice_docs"
    ),
    org_id: Optional[str] = Query(None, description="(Optional) Organisation ID"),
    user_id: Optional[str] = Query(None, description="(Optional) User ID"),
    db: AsyncSession = Depends(get_db),
    engine: PolicyEngine = Depends(get_policy_engine),
) -> EvalResponse:
    """
    Evaluate a transaction by loading OCR'd data from erp_ai.* tables.
    """

    org_val = org_id or ""
    user_val = user_id or ""

    try:
        policy_input: PolicyInput = await build_policy_input_from_source(
            db=db,
            source=source,
            doc_id=doc_id,
            org_id=org_val,
            user_id=user_val,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load document from source={source}: {e}",
        )

    # Map PolicyInput → EvalRequest
    try:
        eval_req: EvalRequest = policy_input_to_eval_request(policy_input)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to map PolicyInput to EvalRequest: {e}",
        )

    # Evaluate normally
    try:
        return await engine.evaluate(eval_req)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Policy evaluation failed: {e}",
        )


# ---------------------------
# Rulepack Management Endpoints (Finance-configurable)
# ---------------------------

@router.get("/rulepack/get")
async def get_rulepack(
    org_id: str,
    db: AsyncSession = Depends(get_db)
):
    repo = DBPolicyRepos(db)
    rp = await repo.load_rulepack(org_id)
    return {"org_id": org_id, "rulepack": rp}


@router.post("/rulepack/update")
async def update_rulepack(
    org_id: str,
    rulepack: dict,
    db: AsyncSession = Depends(get_db)
):
    repo = DBPolicyRepos(db)
    updated = await repo.save_rulepack(org_id, rulepack)
    return {"org_id": org_id, "updated_rulepack": updated}


# ---------------------------
# Health
# ---------------------------

@router.get("/health")
async def health():
    return {"status": "ok", "repo": "DBPolicyRepos"}
