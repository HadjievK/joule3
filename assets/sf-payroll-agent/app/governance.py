"""AGT (Agent Governance Toolkit) integration for the SF Payroll Agent.

This module wraps tools with Microsoft AGT's govern() to enforce the payroll
policy defined in governance/policy.yaml on every tool call.

OWASP ASI 2026 coverage:
  ASI-01 Agent Goal Hijack             block-prompt-injection / block-jailbreak rules
  ASI-02 Tool Misuse & Exploitation    salary cap / bonus cap / destructive-op rules
  ASI-03 Identity & Privilege Abuse    require_approval rules for all confirmed writes
  ASI-04 Data Exfiltration             PII pattern rules (SSN, IBAN, credit card)
  ASI-05 Prompt Injection              block-role-override / block-prompt-injection rules
  ASI-06 Cascading Agent Failures      bulk-write approval gate limits blast radius

Usage
-----
    from governance import apply_governance

    tools = apply_governance(tools, input_text=query)

If `agent-governance-toolkit` is not installed the module degrades gracefully —
tools are returned unwrapped with a warning log.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from typing import Optional, Type
from langchain_core.tools import BaseTool
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Path to the governance policy YAML
_POLICY_PATH = Path(__file__).parent.parent / "governance" / "policy.yaml"

# Whether AGT is available in this environment
_AGT_AVAILABLE = False
_govern: Callable | None = None
_GovernanceDenied: type[Exception] | None = None

try:
    from agentmesh.governance import govern as _govern_fn  # type: ignore[import-untyped]
    from agentmesh.governance.exceptions import GovernanceDenied as _GD  # type: ignore[import-untyped]

    _govern = _govern_fn
    _GovernanceDenied = _GD
    _AGT_AVAILABLE = True
    logger.info("AGT: agent-governance-toolkit loaded — policy: %s", _POLICY_PATH)
except ImportError:
    logger.warning(
        "AGT: agent-governance-toolkit not installed — governance wrapping DISABLED. "
        "Install with: pip install agent-governance-toolkit[full]"
    )


def _wrap_tool(tool: BaseTool, input_text: str) -> BaseTool:
    """Wrap a single LangChain tool with the AGT govern() wrapper.

    Uses a thin BaseTool subclass — NOT StructuredTool.from_function — so the
    raw tool_input dict reaches the governance check intact before being passed
    to the original tool's run / arun.

    Args:
        tool:       LangChain BaseTool to wrap.
        input_text: The original user query — used by input_text-matching rules
                    (PII detection, prompt injection detection).

    Returns:
        A BaseTool subclass instance that evaluates policy.yaml on every call.
    """
    if not _AGT_AVAILABLE or _govern is None:
        return tool

    if not _POLICY_PATH.exists():
        logger.warning("AGT: policy file not found at %s — skipping governance wrap", _POLICY_PATH)
        return tool

    policy_str = str(_POLICY_PATH)
    _tool_ref = tool
    _GovernanceDenied_ref = _GovernanceDenied

    class _AGTWrappedTool(BaseTool):
        name: str = tool.name
        description: str = f"[AGT-governed] {tool.description}"
        args_schema: Optional[Type[BaseModel]] = tool.args_schema  # type: ignore[assignment]
        handle_tool_error: bool = True

        def _policy_check(self, tool_input: Any) -> None:
            """Run AGT policy synchronously. Raises GovernanceDenied if blocked."""
            # Normalise tool_input to a dict for param inspection
            if isinstance(tool_input, dict):
                params = tool_input
            elif isinstance(tool_input, str):
                try:
                    import json as _json
                    params = _json.loads(tool_input)
                except Exception:
                    params = {}
            else:
                params = {}

            call_context = {
                "input_text": input_text,
                "action": {
                    "tool": _tool_ref.name,
                    "type": _tool_ref.name,
                    "params": params,
                },
            }
            try:
                governed_fn = _govern(
                    lambda: None,  # placeholder — we only need the policy check
                    policy=policy_str,
                    context=call_context,
                )
                governed_fn()
            except Exception as exc:
                if _GovernanceDenied_ref and isinstance(exc, _GovernanceDenied_ref):
                    logger.warning(
                        "AGT: GovernanceDenied for tool '%s' | rule='%s' | reason='%s'",
                        _tool_ref.name,
                        getattr(exc, "rule_name", "unknown"),
                        str(exc),
                    )
                raise  # re-raise both GovernanceDenied and unexpected errors

        def _run(self, tool_input: Any = None, **kwargs: Any) -> Any:
            actual_input = tool_input if tool_input is not None else kwargs
            self._policy_check(actual_input)
            return _tool_ref.run(actual_input)

        async def _arun(self, tool_input: Any = None, **kwargs: Any) -> Any:
            actual_input = tool_input if tool_input is not None else kwargs
            self._policy_check(actual_input)
            return await _tool_ref.arun(actual_input)

    return _AGTWrappedTool()


def apply_governance(tools: list[BaseTool], input_text: str = "") -> list[BaseTool]:
    """Apply AGT governance to a list of LangChain tools.

    All tools — read and write — are wrapped for PII and prompt-injection
    detection. Write tools are additionally subject to salary-cap, bonus-cap,
    and approval-gate rules.

    Args:
        tools:      List of LangChain tools to govern.
        input_text: Original user query for text-matching policy rules.

    Returns:
        A new list of tools — each wrapped with AGT (or the original if AGT
        is unavailable).
    """
    if not _AGT_AVAILABLE:
        return tools

    governed: list[BaseTool] = []
    for tool in tools:
        try:
            governed.append(_wrap_tool(tool, input_text))
        except Exception:
            logger.exception("AGT: failed to wrap tool '%s' — using unwrapped fallback", tool.name)
            governed.append(tool)

    governed_count = sum(1 for t in governed if "[AGT-governed]" in (t.description or ""))
    logger.info(
        "AGT: governed %d/%d tool(s) | policy=%s",
        governed_count,
        len(tools),
        _POLICY_PATH.name,
    )
    return governed


def verify_owasp_coverage() -> dict[str, bool]:
    """Run an offline OWASP ASI 2026 coverage check against the policy file.

    Mirrors what `agt verify` reports but works without the CLI binary.
    Returns a dict of {threat_id: covered} booleans.

    Called once at agent startup in agent_executor.py.
    """
    if not _AGT_AVAILABLE or not _POLICY_PATH.exists():
        return {}

    import yaml  # type: ignore[import-untyped]

    try:
        policy = yaml.safe_load(_POLICY_PATH.read_text())
    except Exception:
        logger.exception("AGT: failed to parse policy YAML for OWASP coverage check")
        return {}

    rules = policy.get("rules", [])

    coverage = {
        "ASI-01_Goal_Hijack":        any("ASI-01" in r.get("description", "") for r in rules),
        "ASI-02_Tool_Misuse":        any("ASI-02" in r.get("description", "") for r in rules),
        "ASI-03_Identity_Privilege": any("ASI-03" in r.get("description", "") for r in rules),
        "ASI-04_Data_Exfiltration":  any("ASI-04" in r.get("description", "") for r in rules),
        "ASI-05_Prompt_Injection":   any("ASI-05" in r.get("description", "") for r in rules),
        "ASI-06_Cascading_Failures": any("ASI-06" in r.get("description", "") for r in rules),
    }

    covered = sum(coverage.values())
    total = len(coverage)
    logger.info(
        "AGT OWASP ASI 2026 Coverage: %d/%d risks covered\n%s",
        covered,
        total,
        "\n".join(f"  {'✅' if v else '❌'} {k}" for k, v in coverage.items()),
    )
    return coverage
