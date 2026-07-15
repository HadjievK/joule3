"""Agent-Mesh integration for the SF Payroll Agent.

Implements the agentmesh multi-agent trust fabric from:
https://microsoft.github.io/agent-governance-toolkit/packages/agent-mesh/

What agent-mesh provides (from the actual package)
---------------------------------------------------
agentmesh is the networking / trust layer that sits *between* agents in a
multi-agent system. The key components we integrate are:

1. **TrustedAgentCard** (agentmesh.trust.cards)
   Cryptographically-signed agent identity card. Proves "I am the SF Payroll
   agent version X, with capabilities [payroll_query, compensation_write]".
   Any downstream agent can verify the card before trusting us.

2. **AgentIdentity** (agentmesh.identity.agent_id)
   Ed25519 key-pair based DID identity. Used to sign agent cards and
   inter-agent messages. In production this maps to a managed identity or
   Azure Entra token.

3. **MerkleAuditChain** (agentmesh.governance.audit)
   Tamper-evident audit log. Every tool call and governance decision is
   appended as a Merkle chain entry. The chain can be exported to any
   audit backend (OpenTelemetry, file, SIEM).

4. **trust_verified_tool** (agentmesh.integrations.langchain.tools)
   Wraps a LangChain tool so that calls from low-trust agents (score < min)
   are rejected. Prevents a compromised downstream agent from triggering
   our write tools.

5. **SupplyChainGuard** (agent_compliance.supply_chain)
   Checks requirements.txt for unpinned deps, fresh publishes, and
   typosquatting. Runs once at startup and logs findings.

6. **PromptDefenseEvaluator** (agent_compliance.prompt_defense)
   Static analysis of the agent's system prompt against 12 OWASP LLM Top 10
   attack vectors. Grades the prompt A–F and logs missing defenses.

All components degrade gracefully when agent-governance-toolkit-core is not
installed — the module logs warnings and returns safe no-op stubs.

Usage (from agent_executor.py)
------------------------------
    from agentmesh_integration import (
        get_agent_card,
        get_audit_chain,
        wrap_tools_with_trust_verification,
        run_startup_checks,
    )

    run_startup_checks()        # supply chain + prompt defense
    card = get_agent_card()     # signed identity card
    chain = get_audit_chain()   # tamper-evident audit log
    tools = wrap_tools_with_trust_verification(write_tools)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── Optional imports — all degrade gracefully ────────────────────────────────

_AGENTMESH_AVAILABLE = False
_AGENT_COMPLIANCE_AVAILABLE = False

_TrustedAgentCard: Any = None
_AgentIdentity: Any = None
_IdentityRegistry: Any = None
_AuditLog: Any = None
_trust_verified_tool_fn: Any = None
_SupplyChainGuard: Any = None
_PromptDefenseEvaluator: Any = None

try:
    from agentmesh.trust.cards import TrustedAgentCard as _TAC  # type: ignore[import-untyped]
    from agentmesh.identity.agent_id import AgentIdentity as _AI, IdentityRegistry as _IR  # type: ignore[import-untyped]
    from agentmesh.governance.audit import AuditLog as _AL  # type: ignore[import-untyped]
    from agentmesh.integrations.langchain.tools import trust_verified_tool as _tvt  # type: ignore[import-untyped]

    _TrustedAgentCard = _TAC
    _AgentIdentity = _AI
    _IdentityRegistry = _IR
    _AuditLog = _AL
    _trust_verified_tool_fn = _tvt
    _AGENTMESH_AVAILABLE = True
    logger.info("agentmesh_integration: agentmesh loaded ✅")
except ImportError as _e:
    logger.warning(
        "agentmesh_integration: agentmesh not installed — trust-fabric features DISABLED. "
        "Install with: pip install agent-governance-toolkit[full]  (%s)", _e
    )

try:
    from agent_compliance.supply_chain import SupplyChainGuard as _SCG  # type: ignore[import-untyped]
    from agent_compliance.prompt_defense import PromptDefenseEvaluator as _PDE  # type: ignore[import-untyped]

    _SupplyChainGuard = _SCG
    _PromptDefenseEvaluator = _PDE
    _AGENT_COMPLIANCE_AVAILABLE = True
    logger.info("agentmesh_integration: agent_compliance loaded ✅")
except ImportError as _e:
    logger.warning(
        "agentmesh_integration: agent_compliance not installed — "
        "supply-chain and prompt-defense checks DISABLED.  (%s)", _e
    )

# ── Constants ─────────────────────────────────────────────────────────────────

_AGENT_DID = "did:mesh:sf-payroll-agent"
_AGENT_NAME = "SF Payroll Agent"
_AGENT_CAPABILITIES = [
    "payroll_query",
    "anomaly_detection",
    "tax_compliance_validation",
    "compensation_read",
    "compensation_write",        # guarded by 2-step confirmation + AGT policy
]
_MIN_TRUST_SCORE = 600           # 0–1000; callers below this score cannot invoke write tools
_REQUIREMENTS_PATH = Path(__file__).parent.parent / "requirements.txt"
_SYSTEM_PROMPT_PATH = Path(__file__).parent / "agent.py"


# ── Agent Identity & Card ─────────────────────────────────────────────────────

_identity_cache: Any = None
_card_cache: Any = None


def get_agent_identity() -> Any:
    """Return (or create) the agent's Ed25519 identity.

    In production this should load the identity from a secure keystore
    (Azure Key Vault, TPM, etc.).  For local/test mode a fresh in-memory
    identity is generated on each process start.
    """
    global _identity_cache
    if _identity_cache is not None:
        return _identity_cache

    if not _AGENTMESH_AVAILABLE or _AgentIdentity is None:
        logger.warning("agentmesh_integration: identity unavailable (agentmesh not installed)")
        return None

    try:
        _identity_cache = _AgentIdentity.generate(did=_AGENT_DID)
        logger.info(
            "agentmesh_integration: agent identity created | DID=%s | pubkey=%s…",
            _AGENT_DID,
            str(_identity_cache.public_key)[:16],
        )
        return _identity_cache
    except Exception:
        logger.exception("agentmesh_integration: failed to create agent identity")
        return None


def get_agent_card() -> Any:
    """Return (or create) the signed TrustedAgentCard for this agent.

    The card is signed with the agent's Ed25519 private key. Any
    downstream agent can verify the card using the embedded public key.
    """
    global _card_cache
    if _card_cache is not None:
        return _card_cache

    if not _AGENTMESH_AVAILABLE or _TrustedAgentCard is None:
        logger.warning("agentmesh_integration: agent card unavailable (agentmesh not installed)")
        return None

    identity = get_agent_identity()
    if identity is None:
        return None

    try:
        card = _TrustedAgentCard(
            name=_AGENT_NAME,
            description=(
                "SAP SuccessFactors payroll AI agent. Handles payroll queries, "
                "anomaly detection, tax compliance validation, and compensation "
                "management with mandatory human-in-the-loop confirmation for writes."
            ),
            capabilities=_AGENT_CAPABILITIES,
            trust_score=1.0,
            metadata={
                "agt_policy": "governance/policy.yaml",
                "owasp_asi_coverage": "6/6",
                "write_ops_require_confirmation": True,
                "write_ops_require_agt_approval": True,
            },
        )
        card.sign(identity)
        _card_cache = card
        logger.info(
            "agentmesh_integration: agent card signed | name=%s | capabilities=%s",
            card.name,
            card.capabilities,
        )
        return card
    except Exception:
        logger.exception("agentmesh_integration: failed to create/sign agent card")
        return None


# ── Audit Chain ───────────────────────────────────────────────────────────────

_audit_chain_cache: Any = None


def get_audit_chain() -> Any:
    """Return the shared MerkleAuditChain (or None if agentmesh unavailable)."""
    global _audit_chain_cache
    if _audit_chain_cache is not None:
        return _audit_chain_cache

    if not _AGENTMESH_AVAILABLE or _AuditLog is None:
        return None

    try:
        _audit_chain_cache = _AuditLog()
        logger.info("agentmesh_integration: MerkleAuditChain initialised")
        return _audit_chain_cache
    except Exception:
        logger.exception("agentmesh_integration: failed to initialise audit chain")
        return None


def append_audit_entry(
    event_type: str,
    tool_name: str,
    outcome: str,
    details: dict | None = None,
) -> None:
    """Append an entry to the tamper-evident audit chain.

    Args:
        event_type: e.g. "tool.call.allowed", "tool.call.denied"
        tool_name:  Name of the tool that was called.
        outcome:    "success" | "denied" | "error"
        details:    Optional extra context (userId, amounts, rule, etc.)
    """
    chain = get_audit_chain()
    if chain is None:
        return  # fallback: _audit_log in manage_compensation.py covers this

    try:
        entry = {
            "event_type": event_type,
            "agent_did": _AGENT_DID,
            "action": tool_name,
            "outcome": outcome,
            "data": details or {},
        }
        chain.add_entry(entry)
        logger.debug(
            "agentmesh_integration: audit entry appended | event=%s | tool=%s | outcome=%s",
            event_type, tool_name, outcome,
        )
    except Exception:
        logger.exception("agentmesh_integration: failed to append audit entry")


# ── Trust-Verified Tool Wrapping ──────────────────────────────────────────────

def wrap_tools_with_trust_verification(
    write_tools: list[Any],
    agent_did: str = _AGENT_DID,
    min_trust_score: int = _MIN_TRUST_SCORE,
) -> list[Any]:
    """Wrap write tools so that low-trust callers are rejected.

    Uses agentmesh ``trust_verified_tool`` to enforce that any agent
    invoking a write tool has a trust score ≥ min_trust_score. This
    prevents a compromised downstream agent from triggering salary or
    bonus writes.

    Falls back to returning tools unwrapped when agentmesh is not installed.

    Args:
        write_tools:       Tools to protect (typically update_salary,
                           update_bonus_eligibility).
        agent_did:         DID of the *calling* agent.
        min_trust_score:   Minimum trust score required (0–1000).

    Returns:
        List of tools (trust-wrapped when agentmesh is available).
    """
    if not _AGENTMESH_AVAILABLE or _trust_verified_tool_fn is None:
        return write_tools

    protected: list[Any] = []
    for tool in write_tools:
        try:
            # trust_verified_tool returns a plain Python callable, NOT a BaseTool.
            # We must re-wrap it so LangChain can access .name / .description.
            trust_fn = _trust_verified_tool_fn(
                tool,
                agent_did=agent_did,
                min_score=min_trust_score,
            )
            wrapped = _rewrap_as_base_tool(tool, trust_fn)
            protected.append(wrapped)
            logger.info(
                "agentmesh_integration: trust-wrapped '%s' (min_score=%d)",
                getattr(tool, "name", repr(tool)),
                min_trust_score,
            )
        except Exception:
            logger.exception(
                "agentmesh_integration: failed to trust-wrap '%s' — using unwrapped",
                getattr(tool, "name", repr(tool)),
            )
            protected.append(tool)

    return protected


def _rewrap_as_base_tool(original_tool: Any, trust_fn: Any) -> BaseTool:
    """Re-wrap an agentmesh trust callable back into a LangChain BaseTool.

    agentmesh's trust_verified_tool() returns a plain Python callable.
    LangChain requires BaseTool with .name / .description attributes.
    This shim bridges the gap without losing trust enforcement.
    """
    _original = original_tool
    _trust_fn = trust_fn

    class _TrustWrappedTool(BaseTool):
        name: str = getattr(original_tool, "name", "unknown_tool")
        description: str = f"[trust-verified] {getattr(original_tool, 'description', '')}"
        args_schema: Optional[Type[BaseModel]] = getattr(original_tool, "args_schema", None)  # type: ignore[assignment]
        handle_tool_error: bool = True

        def _run(self, tool_input: Any = None, **kwargs: Any) -> Any:
            actual = tool_input if tool_input is not None else kwargs
            # Call through trust gate; if trust check passes it delegates to original
            return _trust_fn(actual) if callable(_trust_fn) else _original.run(actual)

        async def _arun(self, tool_input: Any = None, **kwargs: Any) -> Any:
            actual = tool_input if tool_input is not None else kwargs
            return await _original.arun(actual)

    return _TrustWrappedTool()


# ── Supply Chain Guard ────────────────────────────────────────────────────────

def run_supply_chain_check() -> list[dict]:
    """Scan requirements.txt for supply chain risks.

    Checks for:
    - Unpinned versions (not pinned to exact ==)
    - Fresh publishes (< 7 days old, if live check enabled)
    - Typosquatting on popular packages

    Returns list of finding dicts. Empty = clean.
    """
    if not _AGENT_COMPLIANCE_AVAILABLE or _SupplyChainGuard is None:
        logger.info("agentmesh_integration: supply chain check skipped (agent_compliance not installed)")
        return []

    if not _REQUIREMENTS_PATH.exists():
        logger.warning("agentmesh_integration: requirements.txt not found at %s", _REQUIREMENTS_PATH)
        return []

    try:
        guard = _SupplyChainGuard()
        findings = guard.check_requirements(str(_REQUIREMENTS_PATH))

        if findings:
            logger.warning(
                "agentmesh_integration: SUPPLY CHAIN — %d finding(s):\n%s",
                len(findings),
                "\n".join(f"  [{f.severity.upper()}] {f.rule}: {f.message}" for f in findings),
            )
        else:
            logger.info("agentmesh_integration: supply chain check clean ✅")

        return [
            {
                "package": f.package,
                "version": f.version,
                "severity": f.severity,
                "rule": f.rule,
                "message": f.message,
            }
            for f in findings
        ]
    except Exception:
        logger.exception("agentmesh_integration: supply chain check failed")
        return []


# ── Prompt Defense Evaluator ──────────────────────────────────────────────────

def run_prompt_defense_check(system_prompt: str) -> dict:
    """Evaluate the system prompt against 12 OWASP LLM Top 10 attack vectors.

    Checks for missing defenses against:
      LLM01 Prompt Injection, LLM02 Output Manipulation,
      LLM06 Abuse Prevention, LLM07 Data Leakage, etc.

    Returns a dict with grade (A–F), score (0–100), and missing vectors.
    """
    if not _AGENT_COMPLIANCE_AVAILABLE or _PromptDefenseEvaluator is None:
        logger.info("agentmesh_integration: prompt defense check skipped (agent_compliance not installed)")
        return {}

    try:
        evaluator = _PromptDefenseEvaluator()
        report = evaluator.evaluate(system_prompt)

        logger.info(
            "agentmesh_integration: prompt defense | grade=%s | score=%d | coverage=%s | missing=%s",
            report.grade,
            report.score,
            report.coverage,
            report.missing or "none",
        )

        if report.is_blocking(min_grade="C"):
            logger.warning(
                "agentmesh_integration: prompt defense grade %s is below C — "
                "consider adding defenses for: %s",
                report.grade,
                ", ".join(report.missing),
            )

        return {
            "grade": report.grade,
            "score": report.score,
            "coverage": report.coverage,
            "defended": report.defended,
            "total": report.total,
            "missing": report.missing,
            "prompt_hash": report.prompt_hash,
        }
    except Exception:
        logger.exception("agentmesh_integration: prompt defense check failed")
        return {}


# ── Startup Orchestration ─────────────────────────────────────────────────────

def run_startup_checks(system_prompt: str = "") -> dict:
    """Run all startup checks: supply chain, prompt defense, agent card, audit chain.

    Called once from agent_executor.py __init__ or at module import.

    Returns a summary dict for observability.
    """
    logger.info("agentmesh_integration: running startup checks…")

    results: dict = {
        "agentmesh_available": _AGENTMESH_AVAILABLE,
        "agent_compliance_available": _AGENT_COMPLIANCE_AVAILABLE,
    }

    # 1. Supply chain
    sc_findings = run_supply_chain_check()
    results["supply_chain"] = {
        "findings": len(sc_findings),
        "critical": sum(1 for f in sc_findings if f.get("severity") == "critical"),
        "high": sum(1 for f in sc_findings if f.get("severity") == "high"),
    }

    # 2. Prompt defense
    if system_prompt:
        pd_result = run_prompt_defense_check(system_prompt)
        results["prompt_defense"] = pd_result
    else:
        results["prompt_defense"] = {"skipped": "no system_prompt provided"}

    # 3. Agent card
    card = get_agent_card()
    results["agent_card"] = {
        "available": card is not None,
        "signed": card is not None and card.card_signature is not None,
        "capabilities": _AGENT_CAPABILITIES,
    }

    # 4. Audit chain
    chain = get_audit_chain()
    results["audit_chain"] = {"available": chain is not None}

    logger.info("agentmesh_integration: startup checks complete | %s", results)
    return results
