"""Tests for tool_integrity.py (tool content hashing) and agentmesh_integration.py.

Scenario: both features degrade gracefully when agent-governance-toolkit-core
is NOT installed (sandbox environment). When installed they use the real
ContentHashInterceptor and agentmesh APIs.

Tests
-----
tool_integrity:
  1. hash_tool returns a 64-char hex string
  2. Same tool produces the same hash (deterministic)
  3. Different tool functions produce different hashes
  4. build_tool_manifest returns correct keys
  5. verify_tool_integrity passes for unmodified tool
  6. verify_tool_integrity fails when tool is monkey-patched
  7. ToolIntegrityGuard.check passes for registered tool
  8. ToolIntegrityGuard.check raises ToolIntegrityError for tampered tool
  9. Strict mode blocks tools with no registered hash
  10. Non-strict mode allows tools with no registered hash

agentmesh_integration:
  11. run_startup_checks returns dict with expected keys
  12. run_supply_chain_check returns list (possibly empty)
  13. run_prompt_defense_check returns dict with grade key (or empty)
  14. get_agent_card returns None gracefully when agentmesh absent
  15. get_audit_chain returns None gracefully when agentmesh absent
  16. wrap_tools_with_trust_verification returns same tools when agentmesh absent
  17. append_audit_entry is safe to call when chain unavailable
"""

from __future__ import annotations

import asyncio
import sys
import os
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("IBD_TESTING", "1")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fresh_import(module_name: str):
    """Remove cached module and reimport fresh."""
    for key in list(sys.modules):
        if key == module_name:
            del sys.modules[key]
    return importlib.import_module(module_name)


def _make_tool(name: str, return_value: str = '{"ok":1}'):
    """Create a minimal StructuredTool-shaped mock."""
    from langchain_core.tools import StructuredTool
    t = MagicMock(spec=StructuredTool)
    t.name = name
    t.description = f"Test tool {name}"
    t.args_schema = None
    t.run = MagicMock(return_value=return_value)
    t.arun = AsyncMock(return_value=return_value)
    # Give it a real callable for the hash to inspect
    t.func = lambda **kw: return_value
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def ti(add_agent_to_path):
    """Import tool_integrity fresh."""
    for k in list(sys.modules):
        if k in ("tool_integrity",):
            del sys.modules[k]
    return importlib.import_module("tool_integrity")


@pytest.fixture()
def ami(add_agent_to_path):
    """Import agentmesh_integration fresh."""
    for k in list(sys.modules):
        if k in ("agentmesh_integration",):
            del sys.modules[k]
    return importlib.import_module("agentmesh_integration")


# ─────────────────────────────────────────────────────────────────────────────
# Tool Integrity Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestToolIntegrity:

    def test_hash_tool_returns_64_char_hex(self, ti):
        """hash_tool must return a valid SHA-256 hex digest (64 chars)."""
        tool = _make_tool("update_salary")
        h = ti.hash_tool(tool)
        assert isinstance(h, str), "hash must be a string"
        assert len(h) == 64, f"SHA-256 hex must be 64 chars, got {len(h)}"
        assert all(c in "0123456789abcdef" for c in h), "must be hex"

    def test_hash_tool_is_deterministic(self, ti):
        """Same tool object must produce the same hash every time."""
        tool = _make_tool("update_salary")
        h1 = ti.hash_tool(tool)
        h2 = ti.hash_tool(tool)
        assert h1 == h2, "hash must be deterministic"

    def test_different_functions_produce_different_hashes(self, ti):
        """Two tools with different underlying functions must hash differently."""
        def fn_a(**kw): return "salary"
        def fn_b(**kw): return "bonus"

        tool_a = _make_tool("update_salary")
        tool_a.func = fn_a
        tool_b = _make_tool("update_bonus_eligibility")
        tool_b.func = fn_b

        ha = ti.hash_tool(tool_a)
        hb = ti.hash_tool(tool_b)
        assert ha != hb, "different tools must have different hashes"

    def test_build_tool_manifest_returns_correct_keys(self, ti, tmp_path, monkeypatch):
        """build_tool_manifest must return a dict keyed by tool names."""
        monkeypatch.setattr(ti, "_MANIFEST_PATH", tmp_path / "tool-hashes.json")
        tools = [_make_tool("update_salary"), _make_tool("update_bonus_eligibility")]
        manifest = ti.build_tool_manifest(tools)
        assert set(manifest.keys()) == {"update_salary", "update_bonus_eligibility"}
        assert all(len(v) == 64 for v in manifest.values())

    def test_verify_tool_integrity_passes_for_unmodified_tool(self, ti, tmp_path, monkeypatch):
        """An unmodified tool must pass integrity verification."""
        monkeypatch.setattr(ti, "_MANIFEST_PATH", tmp_path / "tool-hashes.json")
        tool = _make_tool("update_salary")
        manifest = ti.build_tool_manifest([tool])
        passed, reason = ti.verify_tool_integrity(tool, manifest)
        assert passed, f"Expected pass, got: {reason}"
        assert "verified" in reason

    def test_verify_tool_integrity_fails_for_tampered_tool(self, ti, tmp_path, monkeypatch):
        """A tool whose hash was registered with a different function must fail."""
        monkeypatch.setattr(ti, "_MANIFEST_PATH", tmp_path / "tool-hashes.json")
        tool = _make_tool("update_salary")
        manifest = ti.build_tool_manifest([tool])

        # Tamper: replace the manifest hash with a fake one
        manifest["update_salary"] = "a" * 64

        passed, reason = ti.verify_tool_integrity(tool, manifest)
        assert not passed, "tampered tool must fail"
        assert "mismatch" in reason.lower()

    def test_integrity_guard_check_passes_for_registered_tool(self, ti, tmp_path, monkeypatch):
        """ToolIntegrityGuard.check must not raise for a registered tool."""
        monkeypatch.setattr(ti, "_MANIFEST_PATH", tmp_path / "tool-hashes.json")
        tool = _make_tool("update_salary")
        guard = ti.ToolIntegrityGuard.from_tools([tool])
        # Should not raise
        guard.check(tool)

    def test_integrity_guard_raises_for_tampered_tool(self, ti, tmp_path, monkeypatch):
        """ToolIntegrityGuard.check must raise ToolIntegrityError for tampered tool."""
        monkeypatch.setattr(ti, "_MANIFEST_PATH", tmp_path / "tool-hashes.json")
        tool = _make_tool("update_salary")
        guard = ti.ToolIntegrityGuard.from_tools([tool])

        # Tamper the manifest
        guard._manifest["update_salary"] = "b" * 64

        with pytest.raises(ti.ToolIntegrityGuard.ToolIntegrityError) as exc_info:
            guard.check(tool)
        assert "mismatch" in str(exc_info.value).lower()

    def test_strict_mode_blocks_unregistered_tool(self, ti, tmp_path, monkeypatch):
        """Strict mode must block a tool with no registered hash."""
        monkeypatch.setattr(ti, "_MANIFEST_PATH", tmp_path / "tool-hashes.json")
        guard = ti.ToolIntegrityGuard(manifest={}, strict=True)
        unknown_tool = _make_tool("unknown_write_tool")
        with pytest.raises(ti.ToolIntegrityGuard.ToolIntegrityError) as exc_info:
            guard.check(unknown_tool)
        assert "no registered" in str(exc_info.value).lower() or "strict" in str(exc_info.value).lower()

    def test_non_strict_mode_allows_unregistered_tool(self, ti, tmp_path, monkeypatch):
        """Non-strict mode must allow an unregistered tool with a warning."""
        monkeypatch.setattr(ti, "_MANIFEST_PATH", tmp_path / "tool-hashes.json")
        guard = ti.ToolIntegrityGuard(manifest={}, strict=False)
        unknown_tool = _make_tool("unknown_write_tool")
        # Should not raise
        guard.check(unknown_tool)

    def test_register_tool_adds_to_manifest(self, ti, tmp_path, monkeypatch):
        """register_tool must add a new entry to the manifest."""
        monkeypatch.setattr(ti, "_MANIFEST_PATH", tmp_path / "tool-hashes.json")
        guard = ti.ToolIntegrityGuard(manifest={}, strict=True)
        tool = _make_tool("new_tool")
        guard.register_tool(tool)
        assert "new_tool" in guard.manifest
        # Now check must pass
        guard.check(tool)


# ─────────────────────────────────────────────────────────────────────────────
# Agentmesh Integration Tests (graceful degradation when not installed)
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentmeshIntegration:

    def test_run_startup_checks_returns_expected_keys(self, ami):
        """run_startup_checks must return a dict with required top-level keys."""
        result = ami.run_startup_checks(system_prompt="You are a helpful assistant.")
        assert isinstance(result, dict)
        assert "agentmesh_available" in result
        assert "agent_compliance_available" in result
        assert "supply_chain" in result
        assert "agent_card" in result
        assert "audit_chain" in result

    def test_run_supply_chain_check_returns_list(self, ami):
        """run_supply_chain_check must return a list (possibly empty)."""
        result = ami.run_supply_chain_check()
        assert isinstance(result, list)

    def test_run_prompt_defense_check_returns_dict_or_empty(self, ami):
        """run_prompt_defense_check must return a dict (with grade or empty)."""
        result = ami.run_prompt_defense_check("You are a helpful assistant.")
        assert isinstance(result, dict)
        # Either has grade key (agent_compliance installed) or is empty (not installed)
        if result:
            assert "grade" in result or "skipped" in str(result)

    def test_get_agent_card_returns_none_or_card(self, ami):
        """get_agent_card must return None or a valid card object."""
        # Reset cache to force fresh evaluation
        ami._card_cache = None
        ami._identity_cache = None
        card = ami.get_agent_card()
        # Either None (agentmesh not installed) or a card with capabilities
        if card is not None:
            assert hasattr(card, "capabilities")
            assert "payroll_query" in card.capabilities

    def test_get_audit_chain_returns_none_when_agentmesh_absent(self, ami):
        """get_audit_chain must return None when agentmesh is not installed."""
        if not ami._AGENTMESH_AVAILABLE:
            ami._audit_chain_cache = None
            chain = ami.get_audit_chain()
            assert chain is None

    def test_wrap_tools_returns_same_list_when_agentmesh_absent(self, ami):
        """wrap_tools_with_trust_verification must return the same tools when agentmesh absent."""
        if not ami._AGENTMESH_AVAILABLE:
            tools = [_make_tool("update_salary"), _make_tool("update_bonus_eligibility")]
            result = ami.wrap_tools_with_trust_verification(tools)
            assert result is tools, "Must return same list object when agentmesh absent"

    def test_append_audit_entry_is_safe_when_chain_unavailable(self, ami):
        """append_audit_entry must not raise even when no audit chain is available."""
        ami._audit_chain_cache = None
        # Must not raise
        ami.append_audit_entry(
            event_type="tool.call.allowed",
            tool_name="update_salary",
            outcome="success",
            details={"userId": "alice.johnson", "amount": "88000"},
        )

    def test_startup_checks_supply_chain_has_correct_shape(self, ami):
        """supply_chain result must have findings, critical, high keys."""
        result = ami.run_startup_checks()
        sc = result["supply_chain"]
        assert "findings" in sc
        assert "critical" in sc
        assert "high" in sc
        assert isinstance(sc["findings"], int)

    def test_startup_checks_agent_card_has_capabilities(self, ami):
        """agent_card result must list capabilities."""
        result = ami.run_startup_checks()
        ac = result["agent_card"]
        assert "available" in ac
        assert "capabilities" in ac
        assert "payroll_query" in ac["capabilities"]
        assert "compensation_write" in ac["capabilities"]
