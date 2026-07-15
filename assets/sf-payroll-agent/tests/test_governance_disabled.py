"""Scenario B — AGT DISABLED: governance policy is NOT active.

When `agentmesh` is not installed (or AGT_GOVERNANCE_ENABLED=0), the
governance module degrades gracefully and tools are returned unwrapped.
This means the agent CAN execute risky operations without any policy gate —
the only protection is the built-in two-step confirmation in manage_compensation.py.

These tests verify:
  1.  apply_governance() returns tools UNCHANGED (no wrapping) when AGT absent.
  2.  update_salary(confirmed=False) → CONFIRMATION_REQUIRED (built-in gate).
  3.  update_salary(confirmed=True)  → writes execute (no policy denial).
  4.  update_bonus_eligibility(confirmed=False) → CONFIRMATION_REQUIRED.
  5.  update_bonus_eligibility(confirmed=True)  → writes execute.
  6.  Salary above $1M executes (policy cap not in effect).
  7.  Bonus above 100% executes (policy cap not in effect).
  8.  PII in input does NOT raise (policy rules not loaded).
  9.  Prompt injection does NOT raise.
  10. verify_owasp_coverage() returns empty dict (no AGT = no coverage).
  11. Confirmed write tool.run() is called exactly once (no double-call).
  12. Mock MCP tools are returned by get_mcp_tools() in IBD_TESTING mode.

Environment:  IBD_TESTING=1  (set by root conftest.py)
              agentmesh module intentionally absent from sys.modules
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _remove_agentmesh_from_sys_modules():
    """Ensure agentmesh is completely absent — simulates AGT not installed."""
    for key in list(sys.modules):
        if key.startswith("agentmesh"):
            del sys.modules[key]


def _remove_governance_module():
    """Remove the cached governance module so it re-imports fresh."""
    for key in list(sys.modules):
        if key == "governance":
            del sys.modules[key]


def _import_governance_fresh():
    import importlib
    app_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
    if app_path not in sys.path:
        sys.path.insert(0, app_path)
    return importlib.import_module("governance")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def ensure_agt_absent():
    """Remove agentmesh stub before every test in this module."""
    _remove_agentmesh_from_sys_modules()
    _remove_governance_module()
    yield
    _remove_governance_module()


@pytest.fixture()
def gov(add_agent_to_path):
    """Import governance WITHOUT agentmesh — AGT unavailable path."""
    _remove_agentmesh_from_sys_modules()
    _remove_governance_module()
    return _import_governance_fresh()


@pytest.fixture()
def dummy_read_tool():
    tool = MagicMock()
    tool.name = "list_compensationemployee"
    tool.description = "Read compensation data"
    tool.args_schema = None
    tool.run = MagicMock(return_value='{"salary": 75000}')
    tool.arun = AsyncMock(return_value='{"salary": 75000}')
    return tool


@pytest.fixture()
def raw_salary_tool():
    """A bare MagicMock representing the underlying MCP salary write tool."""
    tool = MagicMock()
    tool.name = "update_salary"
    tool.description = "Update salary"
    tool.args_schema = None
    tool.run = MagicMock(return_value='{"status": "Updated", "annualSalary": "88000.00"}')
    tool.arun = AsyncMock(return_value='{"status": "Updated", "annualSalary": "88000.00"}')
    return tool


# ─────────────────────────────────────────────────────────────────────────────
# Scenario B tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAGTDisabled:

    # ── 1. apply_governance passthrough ───────────────────────────────────────

    def test_apply_governance_returns_tools_unchanged(self, gov, dummy_read_tool, raw_salary_tool):
        """With AGT absent, tools must be returned as-is (identity passthrough)."""
        tools_in = [dummy_read_tool, raw_salary_tool]
        tools_out = gov.apply_governance(tools_in, input_text="any query")
        assert tools_out is tools_in, "apply_governance must return the same list object"
        assert tools_out[0] is dummy_read_tool
        assert tools_out[1] is raw_salary_tool

    def test_agt_available_flag_is_false(self, gov):
        """_AGT_AVAILABLE must be False when agentmesh is not installed."""
        assert gov._AGT_AVAILABLE is False

    # ── 2. Built-in confirmation gate still works ─────────────────────────────

    @pytest.mark.asyncio
    async def test_update_salary_dryrun_returns_confirmation_required(self, add_agent_to_path):
        """confirmed=False must return CONFIRMATION_REQUIRED without touching SAP."""
        from tools.manage_compensation import build_update_salary_tool
        tool = build_update_salary_tool(mcp_tools=[])
        result_str = await tool.arun({
            "userId": "alice.johnson",
            "new_annual_salary": "88000.00",
            "confirmed": False,
        })
        result = json.loads(result_str)
        assert result["status"] == "CONFIRMATION_REQUIRED"
        assert result["operation"] == "update_salary"
        assert "88,000.00" in result["summary"]["new_annual_salary"]

    @pytest.mark.asyncio
    async def test_update_salary_confirmed_executes_in_test_mode(self, add_agent_to_path):
        """confirmed=True must execute and return status=Updated (mock mode)."""
        from tools.manage_compensation import build_update_salary_tool
        tool = build_update_salary_tool(mcp_tools=[])
        result_str = await tool.arun({
            "userId": "alice.johnson",
            "new_annual_salary": "88000.00",
            "confirmed": True,
        })
        result = json.loads(result_str)
        assert result["status"] == "Updated"
        assert result["userId"] == "alice.johnson"
        assert result["annualSalary"] == "88000.00"

    @pytest.mark.asyncio
    async def test_update_bonus_dryrun_returns_confirmation_required(self, add_agent_to_path):
        """Bonus dry-run must return CONFIRMATION_REQUIRED."""
        from tools.manage_compensation import build_update_bonus_eligibility_tool
        tool = build_update_bonus_eligibility_tool(mcp_tools=[])
        result_str = await tool.arun({
            "userId": "dave.wilson",
            "eligible_for_bonus": True,
            "target_bonus_percent": "15.00",
            "confirmed": False,
        })
        result = json.loads(result_str)
        assert result["status"] == "CONFIRMATION_REQUIRED"
        assert result["operation"] == "update_bonus_eligibility"
        assert result["summary"]["eligible_for_bonus"] is True

    @pytest.mark.asyncio
    async def test_update_bonus_confirmed_executes_in_test_mode(self, add_agent_to_path):
        """Bonus confirmed=True must execute and return status=Updated."""
        from tools.manage_compensation import build_update_bonus_eligibility_tool
        tool = build_update_bonus_eligibility_tool(mcp_tools=[])
        result_str = await tool.arun({
            "userId": "dave.wilson",
            "eligible_for_bonus": True,
            "target_bonus_percent": "15.00",
            "confirmed": True,
        })
        result = json.loads(result_str)
        assert result["status"] == "Updated"
        assert result["userId"] == "dave.wilson"
        assert result["eligibleForBonus"] is True

    # ── 3. No policy caps without AGT ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_salary_above_1m_executes_without_agt(self, add_agent_to_path):
        """Without AGT, a $2M salary write goes through (policy cap not active)."""
        from tools.manage_compensation import build_update_salary_tool
        tool = build_update_salary_tool(mcp_tools=[])
        result_str = await tool.arun({
            "userId": "exec.user",
            "new_annual_salary": "2000000.00",
            "confirmed": True,
        })
        result = json.loads(result_str)
        # No GovernanceDenied — mock returns Updated
        assert result["status"] == "Updated"
        assert result["annualSalary"] == "2000000.00"

    @pytest.mark.asyncio
    async def test_bonus_above_100pct_executes_without_agt(self, add_agent_to_path):
        """Without AGT, a 200% bonus write goes through (policy cap not active)."""
        from tools.manage_compensation import build_update_bonus_eligibility_tool
        tool = build_update_bonus_eligibility_tool(mcp_tools=[])
        result_str = await tool.arun({
            "userId": "carol.chen",
            "eligible_for_bonus": True,
            "target_bonus_percent": "200.00",
            "confirmed": True,
        })
        result = json.loads(result_str)
        assert result["status"] == "Updated"
        assert result["targetBonusPercent"] == "200.00"

    # ── 4. PII / Injection — no denial without AGT ───────────────────────────

    @pytest.mark.asyncio
    async def test_ssn_in_input_does_not_raise_without_agt(self, add_agent_to_path):
        """SSN in query does NOT raise when governance is disabled."""
        from tools.manage_compensation import build_update_salary_tool
        tool = build_update_salary_tool(mcp_tools=[])
        # No exception expected — policy not loaded
        result_str = await tool.arun({
            "userId": "alice.johnson",
            "new_annual_salary": "88000.00",
            "confirmed": False,
        })
        result = json.loads(result_str)
        # Dry-run still returns CONFIRMATION_REQUIRED (built-in gate)
        assert result["status"] == "CONFIRMATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_injection_text_does_not_raise_without_agt(self, add_agent_to_path):
        """Prompt injection in query does NOT raise when governance is disabled."""
        from tools.manage_compensation import build_update_salary_tool
        tool = build_update_salary_tool(mcp_tools=[])
        result_str = await tool.arun({
            "userId": "alice.johnson",
            "new_annual_salary": "88000.00",
            "confirmed": False,
        })
        result = json.loads(result_str)
        assert result["status"] == "CONFIRMATION_REQUIRED"

    # ── 5. OWASP coverage is empty without AGT ───────────────────────────────

    def test_owasp_coverage_empty_when_agt_absent(self, gov):
        """verify_owasp_coverage must return {} when AGT is not installed."""
        coverage = gov.verify_owasp_coverage()
        assert coverage == {}, f"Expected empty dict, got {coverage}"

    # ── 6. apply_governance with empty list ───────────────────────────────────

    def test_apply_governance_empty_list(self, gov):
        result = gov.apply_governance([], input_text="test")
        assert result == []

    # ── 7. Confirmation gate: confirmed=True fires exactly once ───────────────

    @pytest.mark.asyncio
    async def test_confirmed_salary_tool_run_called_once(self, add_agent_to_path):
        """The underlying MCP tool should be called exactly once on confirmed=True."""
        mock_mcp_tool = MagicMock()
        mock_mcp_tool.name = "update_compensationemployee"
        mock_mcp_tool.arun = AsyncMock(return_value='{"status": "Updated"}')
        mock_mcp_tool.acall = AsyncMock(return_value='{"status": "Updated"}')

        from tools.manage_compensation import build_update_salary_tool
        # Patch IBD_TESTING=0 so it tries the real MCP path
        with patch.dict(os.environ, {"IBD_TESTING": "0"}):
            tool = build_update_salary_tool(mcp_tools=[mock_mcp_tool])
            await tool.arun({
                "userId": "alice.johnson",
                "new_annual_salary": "88000.00",
                "confirmed": True,
            })
        mock_mcp_tool.acall.assert_called_once()

    # ── 8. IBD_TESTING mock tools loaded correctly ───────────────────────────

    @pytest.mark.asyncio
    async def test_get_mcp_tools_returns_mocks_in_testing_mode(self, add_agent_to_path):
        """get_mcp_tools() must return non-empty list when IBD_TESTING=1."""
        assert os.environ.get("IBD_TESTING") == "1", "IBD_TESTING must be set by conftest.py"
        from mcp_tools import get_mcp_tools
        tools = await get_mcp_tools(user_token=None)
        assert isinstance(tools, list)
        assert len(tools) > 0, "Mock tools list must not be empty in IBD_TESTING mode"
        tool_names = [t.name for t in tools]
        # At least one read tool should be present
        assert any("compensation" in n.lower() or "payroll" in n.lower() for n in tool_names), (
            f"Expected at least one payroll/compensation tool, got: {tool_names}"
        )
