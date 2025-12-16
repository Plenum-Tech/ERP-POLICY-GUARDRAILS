"""
PolicyEngine — orchestrates policy rules and risk scoring (POC scope).

POC Scope (no role-based/freelancer rules):
- Receipts required
- Cutoff (informational)
- Weekend awareness (+ weekend caps/approval)
- General per-transaction caps
- Monthly budget (MTD)
- Duplicate detection
- MCC/category mismatch (warn)
- FX & tip outliers (warn)
- Forex card allowlist
- Travel lead-time (soft)
- Upgrades need approval (hard)
- PO required for PO projects (hard)
- PCC required at payment stage (hard)
- Holiday flag/soft/hard (configurable)

Rules are optional: if a module has no rule fn yet, it’s skipped gracefully.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.models import (
    BlockStatus,
    EvalRequest,
    EvalResponse,
    HardRequirement,
    PolicyMessage,
    RiskFlag,
    RiskPayload,
    Severity,
    TxInput,
)
from app.services.risk_engine import RiskEngine


# ---------------------------
# Rule outcome contract (internal)
# ---------------------------

@dataclass
class RuleOutcome:
    """
    A single rule’s contribution to:
      - status_delta: SOFT_BLOCK / HARD_BLOCK / None
      - messages: user-visible guidance
      - flags: risk flags
      - hard_requirements: things the user must supply to proceed
    """
    status_delta: Optional[str] = None                 # "SOFT_BLOCK" | "HARD_BLOCK" | None
    messages: List[PolicyMessage] = None
    flags: List[RiskFlag] = None
    hard_requirements: List[HardRequirement] = None

    def __post_init__(self) -> None:
        self.messages = self.messages or []
        self.flags = self.flags or []
        self.hard_requirements = self.hard_requirements or []


# ---------------------------
# PolicyEngine
# ---------------------------

# Accept both modern and legacy function names
CANDIDATE_FN_NAMES: Tuple[str, ...] = ("check", "apply")

# POC rule order (no role/freelancer rules):
DEFAULT_RULE_MODULES = [
    "app.domain.rules.receipts",
    "app.domain.rules.cutoff_24th",
    "app.domain.rules.holiday_flag",
    "app.domain.rules.weekend",          # weekend-specific caps/approval first
    "app.domain.rules.caps_per_tx",      # then general per-tx caps
    "app.domain.rules.monthly_budget",
    "app.domain.rules.duplicates",
    "app.domain.rules.mcc_mismatch",
    "app.domain.rules.fx_tip_outliers",
    "app.domain.rules.forex_card",
    "app.domain.rules.travel_leadtime",
    "app.domain.rules.upgrades",
    "app.domain.rules.po_required",
    "app.domain.rules.pcc_required",
]


# Requirement codes that should be treated as HARD vs SOFT at engine level.
# (Rules still may set explicit status_delta if they want.)
_HARD_REQUIREMENT_CODES = {
    "REQUIRE_RECEIPT",      # receipts required
    "REQUIRE_ATTACHMENTS",  # generic attachments field
    "PO_REQUIRED",          # PO required for PO projects
    "PCC_REQUIRED",         # PCC required at payment stage
    "UPGRADE_NOT_ALLOWED",  # hard ban on upgrades
}

# Soft requirements still block submit until user does something, but are
# conceptually softer (manager approval, justification, etc.).
_SOFT_REQUIREMENT_CODES = {
    "MANAGER_APPROVAL_REQUIRED",
    "JUSTIFICATION_REQUIRED",
    "TRAVEL_LEADTIME_APPROVAL",  # soft lead-time checks
}


class PolicyEngine:
    def __init__(
        self,
        repos: Any,
        risk_engine: Optional[RiskEngine] = None,
        rule_modules: Optional[List[str]] = None,
    ) -> None:
        self.repos = repos
        self.risk_engine = risk_engine or RiskEngine()
        self.rule_modules = rule_modules or DEFAULT_RULE_MODULES

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------
    async def evaluate(self, req: EvalRequest) -> EvalResponse:
        """
        Orchestrate:
          1) load org rulepack
          2) run built-in required-fields / receipts-required
          3) run each rule module (weekend, caps, budget, duplicates, MCC, FX/tip, forex card, travel, upgrades, PO, PCC...)
          4) derive final status (PASS / SOFT_BLOCK / HARD_BLOCK)
          5) compute risk score + flags
          6) emit EvalResponse
        """
        # 1) Load rule pack
        rulepack: Dict[str, Any] = await self._safe_load_rulepack(req.org_id)

        # 2) Accumulators
        status: str = BlockStatus.PASS_.value
        messages: List[PolicyMessage] = []
        flags: List[RiskFlag] = []
        hard_reqs: List[HardRequirement] = []

        # 3) Built-in required-fields + receipts-required logic
        builtin_outcome = self._evaluate_builtin_requireds(req.tx, rulepack)
        if builtin_outcome:
            messages.extend(builtin_outcome.messages)
            flags.extend(builtin_outcome.flags)
            hard_reqs.extend(builtin_outcome.hard_requirements)
            if builtin_outcome.status_delta == BlockStatus.HARD_BLOCK.value:
                status = BlockStatus.HARD_BLOCK.value
            elif builtin_outcome.status_delta == BlockStatus.SOFT_BLOCK.value:
                status = BlockStatus.SOFT_BLOCK.value

        # 4) Run rule modules (weekend, caps, budget, etc.), sync/async-safe
        for fn in self._registry():
            try:
                outcome: Optional[RuleOutcome] = await self._invoke_rule(fn, req, rulepack)
            except Exception as e:
                # Rule errors should not break evaluation; they add a warning + risk flag.
                messages.append(
                    PolicyMessage(
                        severity=Severity.WARNING,
                        code="RULE_ERROR",
                        text=(
                            f"Rule {getattr(fn, '__module__', 'unknown')}."
                            f"{getattr(fn, '__name__', 'unknown')} failed; flagged for review."
                        ),
                        suggested_actions=[],
                    )
                )
                flags.append(RiskFlag(code="RULE_ERROR", reason=str(e)))
                continue

            if not outcome:
                continue

            messages.extend(outcome.messages)
            flags.extend(outcome.flags)
            hard_reqs.extend(outcome.hard_requirements)

            # status_delta from rules: HARD_BLOCK dominates, then SOFT_BLOCK
            if outcome.status_delta == BlockStatus.HARD_BLOCK.value:
                status = BlockStatus.HARD_BLOCK.value
            elif (
                outcome.status_delta == BlockStatus.SOFT_BLOCK.value
                and status != BlockStatus.HARD_BLOCK.value
            ):
                status = BlockStatus.SOFT_BLOCK.value

        # 5) Derive final status from accumulated hard requirements (multi-scenario)
        status = self._derive_status_from_requirements(status, hard_reqs)

        # 6) Risk scoring + audit stub
        weights = (rulepack.get("risk_weights") if isinstance(rulepack, dict) else {}) or {}
        risk_payload: RiskPayload = self.risk_engine.score(flags, weights)

        try:
            await self._safe_audit_stub(req, status, messages, risk_payload)
        except Exception:
            # Audit failures must never block user flows
            pass

        return EvalResponse(
            status=status,  # type: ignore
            messages=messages,
            hard_requirements=hard_reqs,
            risk=risk_payload,
        )

    # ------------------------------------------------------------------
    # Async/sync-safe rule invocation
    # ------------------------------------------------------------------
    async def _invoke_rule(
        self,
        fn: Callable[..., Any],
        req: EvalRequest,
        rulepack: Dict[str, Any],
    ) -> Optional[RuleOutcome]:
        """
        Safely invoke a rule function that may be:
        - sync and return RuleOutcome
        - async and return RuleOutcome
        - sync but internally return a coroutine (we still await it)

        Signature expected by rules: check(req, rulepack, repos)
        """
        result = fn(req, rulepack, self.repos)

        # If it's awaitable (coroutine / async generator), await it
        if inspect.isawaitable(result):
            result = await result

        # Now result should be RuleOutcome | None
        return result

    # ------------------------------------------------------------------
    # Built-in required-fields / receipts-required logic
    # ------------------------------------------------------------------
    def _evaluate_builtin_requireds(self, tx: TxInput, rulepack: Dict[str, Any]) -> Optional[RuleOutcome]:
        """
        Handle generic required_fields + special receipts-required scenario.

        This covers:
          - generic required fields from rulepack.required_fields
          - receipts-required: attachments missing => HARD_BLOCK + risk flag
        """
        req_fields = (rulepack.get("required_fields") if isinstance(rulepack, dict) else []) or []
        if not req_fields:
            return None

        missing: List[str] = self._missing_required_fields(tx, req_fields)
        if not missing:
            return None

        messages: List[PolicyMessage] = []
        flags: List[RiskFlag] = []
        hard_reqs: List[HardRequirement] = []

        # General "required field missing" error
        messages.append(
            PolicyMessage(
                severity=Severity.ERROR,
                code="REQUIRED_FIELD_MISSING",
                text=f"Missing required field(s): {', '.join(missing)}",
                suggested_actions=[],
            )
        )

        # For each missing field, add a HardRequirement code
        for key in missing:
            code = f"REQUIRE_{key.upper()}"
            label = f"{key.replace('_', ' ').title()} is required"
            hard_reqs.append(HardRequirement(code=code, label=label))

        # Special handling for receipts-required (attachments)
        # We treat missing attachments as a dedicated scenario:
        if "attachments" in missing:
            messages.append(
                PolicyMessage(
                    severity=Severity.ERROR,
                    code="MISSING_RECEIPT",
                    text="Receipt/invoice is required (min 1). Please attach a valid document.",
                    suggested_actions=["attach_receipt"],
                )
            )
            hard_reqs.append(
                HardRequirement(
                    code="REQUIRE_RECEIPT",
                    label="Attach valid receipt/invoice",
                )
            )
            flags.append(
                RiskFlag(
                    code="MISSING_RECEIPT",
                    reason="No receipt attached; required min=1",
                )
            )

        # Required fields are considered a hard-blocking condition out of the box
        return RuleOutcome(
            status_delta=BlockStatus.HARD_BLOCK.value,
            messages=messages,
            flags=flags,
            hard_requirements=hard_reqs,
        )

    # ------------------------------------------------------------------
    # Status derivation from hard requirements (multi-scenario)
    # ------------------------------------------------------------------
    def _derive_status_from_requirements(
        self,
        current_status: str,
        hard_reqs: List[HardRequirement],
    ) -> str:
        """
        Decide final status (PASS / SOFT_BLOCK / HARD_BLOCK) based on:
          - existing status derived from rule outcomes
          - the set of hard_requirements and their codes

        Logic:
          - If any HARD requirement code present → HARD_BLOCK
          - Else if any SOFT requirement code present → SOFT_BLOCK (unless already HARD_BLOCK)
          - Else keep current_status as is
        """
        if not hard_reqs:
            return current_status

        # If a rule has already escalated to HARD_BLOCK, keep it
        if current_status == BlockStatus.HARD_BLOCK.value:
            return current_status

        codes = {hr.code for hr in hard_reqs if getattr(hr, "code", None)}

        # Any explicit HARD requirement code → HARD_BLOCK
        if codes & _HARD_REQUIREMENT_CODES:
            return BlockStatus.HARD_BLOCK.value

        # Any explicit SOFT requirement code → at least SOFT_BLOCK
        if codes & _SOFT_REQUIREMENT_CODES:
            if current_status != BlockStatus.HARD_BLOCK.value:
                return BlockStatus.SOFT_BLOCK.value

        # Fallback: if we have some requirements but they don't match the
        # known sets, treat them as soft-block by default.
        if current_status == BlockStatus.PASS_.value:
            return BlockStatus.SOFT_BLOCK.value

        return current_status

    # ------------------------------------------------------------------
    # Helpers: rulepack, audit, registry, missing-fields
    # ------------------------------------------------------------------
    async def _safe_load_rulepack(self, org_id: str) -> Dict[str, Any]:
        try:
            rp = await self.repos.load_rulepack(org_id)
            return rp or {}
        except Exception:
            return {}

    async def _safe_audit_stub(
        self,
        req: EvalRequest,
        status: str,
        messages: List[PolicyMessage],
        risk: RiskPayload,
    ) -> None:
        try:
            await self.repos.audit_stub(
                req=req,
                status=status,
                messages=[m.model_dump() for m in messages],
                risk={
                    "score": risk.score,
                    "flags": [f.model_dump() for f in risk.flags],
                },
            )
        except Exception:
            # Audit is best-effort; never block user flows
            return

    def _missing_required_fields(self, tx: TxInput, req_fields: List[str]) -> List[str]:
        """
        Generic required-fields checker used by _evaluate_builtin_requireds.

        Treats as missing if:
          - attribute is None
          - empty/whitespace string
          - empty list (e.g. attachments[])
        """
        missing: List[str] = []
        for key in req_fields:
            val = getattr(tx, key, None)
            if val is None:
                missing.append(key)
            elif isinstance(val, str) and not val.strip():
                missing.append(key)
            elif isinstance(val, list) and len(val) == 0:
                missing.append(key)
        return missing

    def _registry(self) -> List[Callable[..., Any]]:
        """Load rule callables from configured modules, accepting 'check' or 'apply'."""
        fns: List[Callable[..., Any]] = []
        for mod_name in self.rule_modules:
            try:
                mod = importlib.import_module(mod_name)
            except Exception as e:
                print(f"[policy] FAILED to import {mod_name}: {e}")
                continue

            fn = None
            for name in CANDIDATE_FN_NAMES:
                cand = getattr(mod, name, None)
                if callable(cand):
                    fn = cand
                    break

            if fn:
                fns.append(fn)
                print(f"[policy] loaded: {mod_name}.{fn.__name__}")
            else:
                print(
                    f"[policy] {mod_name}: loaded but no callable named "
                    f"{CANDIDATE_FN_NAMES}"
                )

        return fns
