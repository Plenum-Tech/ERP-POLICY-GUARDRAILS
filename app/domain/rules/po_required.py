"""
Rule: Purchase Order (PO) required for PO-type projects

Policy intent (PoC):
- If a transaction belongs to a project that requires a PO (e.g., project_type == "PO"),
  submission should be blocked until a valid PO is on file.

Rule pack knobs:
{
  "project_docs": {
    "po_required_for": ["PO"]     // list of project_type values that require a PO
  }
}

Trigger:
- tx.project_type (or context project_type) is in rulepack.project_docs.po_required_for

Behavior:
- If po_required_for is empty or not configured → no-op.
- If project_type not in po_required_for → no-op.
- Otherwise:
    -> call repos.po_on_file(project_id=...)  (project_id resolved from context/tx)
    -> if False -> HARD_BLOCK
        - Message explains requirement
        - HardRequirement: REQUIRE_PO
        - Risk flag: PO_MISSING

PoC safety:
- If repos.po_on_file is missing or throws, we treat PO as present (no block)
  so infra gaps do NOT hard-block users during PoC.
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


def _msg(code: str, text: str, severity: Severity = Severity.ERROR) -> PolicyMessage:
    return PolicyMessage(
        severity=severity,
        code=code,
        text=text,
        suggested_actions=["attach_po_reference", "contact_project_admin"],
    )


def _flag(code: str, reason: str) -> RiskFlag:
    return RiskFlag(code=code, reason=reason)


def _hard(code: str, label: str) -> HardRequirement:
    return HardRequirement(code=code, label=label)


def _norm(value: Optional[str]) -> Optional[str]:
    """Normalize project_type strings for case-insensitive comparison."""
    if value is None:
        return None
    s = str(value).strip()
    return s.upper() if s else None


def _resolve_project_type(req) -> Optional[str]:
    """
    Resolve project_type from TxInput and/or context.

    Priority:
      1) tx.project_type
      2) context["project_type"]
    """
    tx = getattr(req, "tx", None)
    if tx is not None:
        pt = getattr(tx, "project_type", None)
        if pt:
            return str(pt)

    ctx = getattr(req, "context", None) or {}
    if isinstance(ctx, dict):
        pt = ctx.get("project_type")
        if pt:
            return str(pt)

    return None


def _resolve_project_id(req) -> Optional[str]:
    """
    Resolve project identifier for PO lookup.

    Priority:
      1) context["project_id"]
      2) tx.project_id
      3) tx.project_code

    All optional; None is allowed (will still run, error text will show project_id=None).
    """
    ctx = getattr(req, "context", None) or {}
    if isinstance(ctx, dict):
        pid = ctx.get("project_id")
        if pid:
            return str(pid)

    tx = getattr(req, "tx", None)
    if tx is not None:
        pid = getattr(tx, "project_id", None)
        if pid:
            return str(pid)

        pcode = getattr(tx, "project_code", None)
        if pcode:
            return str(pcode)

    return None


async def _has_po_on_file(repos: Any, project_id: Optional[str]) -> bool:
    """
    Wrapper around repos.po_on_file with PoC-safe fallbacks.

    - If repos has no po_on_file() attribute → return True (do NOT block).
    - If po_on_file raises any exception → return True (do NOT block).
    - Otherwise: returns bool(result).
    """
    if not hasattr(repos, "po_on_file"):
        # No implementation yet → do not block in PoC
        return True

    try:
        res = repos.po_on_file(project_id=project_id)
        if inspect.isawaitable(res):
            res = await res
        return bool(res)
    except Exception:
        # If check fails, assume PO present to avoid blocking due to infra issues
        return True


# ---------------------------------------------------------------------------
# Main rule
# ---------------------------------------------------------------------------

async def check(req, rulepack: Dict[str, Any], repos) -> Optional[RuleOutcome]:
    cfg = _cfg(rulepack)
    raw_required_for = cfg.get("po_required_for") or []
    if not raw_required_for:
        # No project types configured to require PO
        return None

    required_for = { _norm(v) for v in raw_required_for if _norm(v) }
    if not required_for:
        return None

    # Resolve project_type from tx/context
    project_type_raw = _resolve_project_type(req)
    project_type_norm = _norm(project_type_raw)

    if not project_type_norm:
        # No project_type info → cannot apply rule
        return None

    # Only enforce when project_type is configured to require PO
    if project_type_norm not in required_for:
        return None

    # Resolve project id (optional) for lookup
    project_id = _resolve_project_id(req)

    # Ask repo layer if a PO exists for this project
    has_po = await _has_po_on_file(repos, project_id)
    if has_po:
        return None

    # PO is required and not on file → HARD BLOCK
    return RuleOutcome(
        status_delta="HARD_BLOCK",
        messages=[
            _msg(
                "PO_MISSING",
                "A valid Purchase Order is required for this project type. "
                "Please attach the PO reference or select a project with an approved PO."
            )
        ],
        flags=[
            _flag(
                "PO_MISSING",
                f"PO not found for PO-type project (project_type={project_type_norm!r}, "
                f"project_id={project_id!r})"
            )
        ],
        hard_requirements=[_hard("REQUIRE_PO", "Attach valid PO reference/document")],
    )
