"""
Rule: PCC (Post Contract/Compliance) required before payment

Policy intent (PoC):
- Certain projects/payments must not be released until a PCC (or equivalent compliance doc) is on file.
- For the PoC, we only enforce this rule when the evaluation context indicates we are at the payment step.

Rule pack knobs:
{
  "project_docs": {
    "pcc_required_for_payment": true
  }
}

Trigger (PoC):
- req.context["stage"] in {"payment","payrun"} OR req.context["is_payment"] == True

Behavior:
- If pcc_required_for_payment is true and we're in the payment stage:
    -> call repos.pcc_on_file(project_id=...)  (project_id is optional; pulled from:
          req.context["project_id"] or tx.project_id / tx.project_code if present)
    -> if False -> HARD_BLOCK + risk flag PCC_MISSING + hard requirement REQUIRE_PCC
- Otherwise → no-op

Notes:
- repos.pcc_on_file() is assumed to be async or sync; this rule handles both.
- If repos.pcc_on_file is missing or errors, we treat PCC as present (no block)
  so infra gaps don't hard-block payments in the PoC.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Optional

from app.models import HardRequirement, PolicyMessage, RiskFlag, Severity
from app.services.policy_engine import RuleOutcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(rulepack: Dict[str, Any]) -> Dict[str, Any]:
    return (rulepack.get("project_docs") or {}) if isinstance(rulepack, dict) else {}


def _is_payment_stage(ctx: Optional[Dict[str, Any]]) -> bool:
    """
    Returns True if the evaluation context indicates that we are at/inside
    a payment stage (payment run, actual disbursement, etc.).
    """
    if not isinstance(ctx, dict):
        return False

    stage = str(ctx.get("stage", "")).lower()
    if stage in {"payment", "payrun"}:
        return True

    if bool(ctx.get("is_payment", False)):
        return True

    return False


def _msg(code: str, text: str, severity: Severity = Severity.ERROR) -> PolicyMessage:
    return PolicyMessage(
        severity=severity,
        code=code,
        text=text,
        suggested_actions=["attach_pcc_reference", "contact_project_admin"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _hard(code: str, label: str) -> HardRequirement:
    return HardRequirement(code=code, label=label)


async def _has_pcc_on_file(repos: Any, project_id: Optional[str]) -> bool:
    """
    Wrapper around repos.pcc_on_file with PoC-safe fallbacks.

    - If repos has no pcc_on_file() attribute → return True (do NOT block).
    - If pcc_on_file raises any exception → return True (do NOT block).
    - Otherwise: returns bool(result).

    This ensures infra gaps do not hard-block payments in the PoC.
    """
    if not hasattr(repos, "pcc_on_file"):
        # No implementation yet → assume PCC present to avoid blocking
        return True

    try:
        res = repos.pcc_on_file(project_id=project_id)
        if inspect.isawaitable(res):
            res = await res
        return bool(res)
    except Exception:
        # If check fails, do not block in PoC
        return True


def _resolve_project_id(req) -> Optional[str]:
    """
    Resolve project identifier from context and/or transaction.

    Priority:
      1) req.context["project_id"]
      2) tx.project_id (if present)
      3) tx.project_code (if present)

    All are optional; returning None is allowed (rule will still run, but
    error messages will show project_id=None).
    """
    ctx = getattr(req, "context", None) or {}
    if isinstance(ctx, dict):
        pid = ctx.get("project_id")
        if pid:
            return str(pid)

    tx = getattr(req, "tx", None)
    if tx is not None:
        # Optional attributes; use getattr to be safe
        pid = getattr(tx, "project_id", None)
        if pid:
            return str(pid)

        pcode = getattr(tx, "project_code", None)
        if pcode:
            return str(pcode)

    return None


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

async def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    cfg = _cfg(rulepack)
    # If PCC requirement is not enabled in rulepack → no-op
    if not bool(cfg.get("pcc_required_for_payment", False)):
        return None

    ctx = req.context or {}
    # Only enforce at payment stage
    if not _is_payment_stage(ctx):
        return None

    # Resolve project identifier (may be None)
    project_id = _resolve_project_id(req)

    # Ask repo if PCC exists; PoC-safe wrapper avoids blocking on infra gaps
    has_pcc = await _has_pcc_on_file(repos, project_id)
    if has_pcc:
        return None

    # PCC is required and not on file → HARD BLOCK
    return RuleOutcome(
        status_delta="HARD_BLOCK",
        messages=[
            _msg(
                "PCC_MISSING",
                "Compliance (PCC) documentation is required before payment. "
                "Please attach a valid PCC reference/document."
            )
        ],
        flags=[
            _flag(
                "PCC_MISSING",
                f"PCC not found for project_id={project_id!r}"
            )
        ],
        hard_requirements=[
            _hard("REQUIRE_PCC", "Attach valid PCC reference/document")
        ],
    )
