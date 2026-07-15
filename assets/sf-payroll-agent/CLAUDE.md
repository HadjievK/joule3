# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An A2A (Agent-to-Agent) protocol AI agent for SAP SuccessFactors payroll operations: natural-language payroll queries, statistical anomaly detection, tax-compliance review, and compensation writes. Built on LangChain/LangGraph + LiteLLM + the SAP Cloud SDK, deployed as an SAP BTP "asset". Python 3.13.

The repo root is `joule3/`, but **all agent development happens in the asset root `assets/sf-payroll-agent/`** — run every command from there.

## Commands

All from `assets/sf-payroll-agent/`:

```bash
pip install -r requirements.txt          # runtime deps (includes vendored SAP Cloud SDK wheel)
pip install -r requirements-test.txt     # test deps (pytest, ruff)

pytest                                   # ALWAYS run bare — pytest.ini supplies -v, coverage, timeout, markers
pytest tests/test_governance_enabled.py::test_name   # single test (only exception to bare invocation)
ruff check app/                          # lint

python app/main.py --port 5000           # run server locally (needs IBD_TESTING=1 for mock tools)
```

- Do **not** pass `--cov`, `--json-report`, or paths to a full `pytest` run — extra flags conflict with `pytest.ini` and suppress `test_report.json`, which is only written on a bare full run.
- Coverage threshold is **≥ 70%** (see `.coveragerc` for what's excluded from measurement — `main.py`, `agent_executor.py`, `mcp_tools.py`, `util.py`, `load_skill_resources.py`).
- `conftest.py` sets `IBD_TESTING=1` and adds `app/` (not the asset root) to `sys.path` before any agent code runs, so agent modules import as top-level names (`from agent import ...`), matching how `main.py` runs at runtime.

## Architecture

Request flow (per A2A request), all orchestrated in `app/agent_executor.py::execute`:

1. **JWT extraction** — `main.py`'s `JWTContextMiddleware` pulls the bearer token off the `Authorization` header into a `ContextVar` (`mcp_tools.set_user_token`). Tools read it at call time, so cached tools use per-request credentials.
2. **Load MCP tools** — `mcp_tools.get_mcp_tools(user_token)` returns user-scoped tools. Tools are fetched **per request** (listings are user-specific), not cached across users.
3. **Attach write tools** — `tools/manage_compensation.py` builds `update_salary` and `update_bonus_eligibility` and wires them to a matching MCP write tool discovered by keyword.
4. **Tamper detection** — `tool_integrity.ToolIntegrityGuard` SHA-256-hashes each write tool's source at startup (manifest at `governance/tool-hashes.json`) and re-checks before calls.
5. **Trust verification** — `agentmesh_integration.wrap_tools_with_trust_verification` gates write tools on a caller trust score (≥ 600).
6. **Governance** — `governance.apply_governance(tools, input_text=query)` wraps every tool with a policy check against `governance/policy.yaml`.
7. **Stream** — `agent.SampleAgent.stream` builds the LangGraph via `create_agent`, invokes it, and streams status/artifact events back through the A2A `TaskUpdater`.

### Dual-mode MCP (critical to understand)

`mcp_tools.py` is the **owned indirection layer** between agent code and the SAP Agent Gateway. Always import `get_mcp_tools` / `set_user_token` from here — never import `sap_cloud_sdk.agentgateway` directly.

- **Production** (`IBD_TESTING` unset): connects to Agent Gateway over mTLS, with a **circuit breaker** (opens after 3 consecutive failures, 60s cooldown) and connection pooling of the AGW client.
- **Test/local** (`IBD_TESTING=1`): builds `StructuredTool`s from `mcp-mock.json` — no network. `manage_compensation.py` write tools also short-circuit to deterministic mock responses in this mode.

MCP tool names are namespaced at runtime (`{resource}_{version}__{tool_name}`, see `util.enhance_tool_name`). **Never hard-code MCP tool names** — resolve tools by capability/keyword (as `manage_compensation._find_mcp_tool` and the `tools/*.py` builders do).

### Write-tool safety model (three independent layers)

Salary/bonus writes are the sensitive surface, guarded by three separate mechanisms:

1. **Two-step confirmation** (in the tool itself): first call with `confirmed=False` returns a `CONFIRMATION_REQUIRED` JSON summary and touches nothing; only `confirmed=True` after explicit user approval executes. The agent must **never** call a write tool with `confirmed=True` on first invocation — this is enforced by the system prompt.
2. **Content-hash integrity** (`tool_integrity.py`): defeats name-based policy bypass via wrapper injection.
3. **AGT policy** (`governance/policy.yaml`): PII blocks (SSN/IBAN/CC), prompt-injection/jailbreak blocks, salary cap ($1M), bonus caps (0–100%), bulk-write dual approval, and approval gates on confirmed writes. Maps to OWASP ASI 2026 ASI-01…ASI-06.

`governance.py` and `agentmesh_integration.py` both **degrade gracefully** to no-ops when `agent-governance-toolkit` / `agentmesh` isn't installed — logged as warnings, tools returned unwrapped.

### Runtime skills

`load_skill_resources.py` exposes a `load(path)` tool that reads `app/skills/<name>/SKILL.md` (and companion files) on demand — path-traversal-guarded to `app/skills/`. Add domain instructions as new skill folders here rather than bloating the system prompt.

## Project conventions (from specification/guidelines-agent.md)

- **Never call SAP APIs directly** (no `requests`/`httpx`/hand-rolled OData). All SAP access goes through MCP tools.
- **Never use `create_react_agent`** (deprecated). Use `from langchain.agents import create_agent`.
- **`app/agent.py` has exactly three decorated functions** (`@agent_model`, `@agent_config` for temperature, `@prompt_section`) — this set is final. Do not add more decorated functions; other tunables must be plain Python constants. `@agent_config` only exposes temperature to the SAP UI.
- **`auto_instrument()` must run at the top of `main.py` before any AI-framework imports** (it currently does, right after `set_aicore_config()`).
- Business logic is instrumented with milestone logs: `[MILESTONE_ID].[achieved|missed]: description` (e.g. `M2.achieved`, `M3.missed`).
- **Never use `with tracer.start_as_current_span(...)` inside an async generator** (any method with `yield`) — it raises `ValueError: Token was created in a different Context` on `GeneratorExit`. Extract logic into a plain async helper and instrument that.
- The system prompt requires `$top` (or equivalent) capped at **100** on every OData tool call, and forbids fabricating data.
- No `.env` files, no git operations, no `sys.path` mutation in app code. Update `requirements.txt` for any new dependency.
- All LLM calls **must be mocked in tests** — AI Core credentials are not available in the test environment. Patch `ChatLiteLLM`.

## Layout

- `app/main.py` — A2A Starlette server, JWT middleware, agent card. Entry point.
- `app/agent_executor.py` — per-request orchestration (steps 1–7 above); runs one-time startup checks at import.
- `app/agent.py` — `SampleAgent`, LangGraph construction, system prompt, thread TTL eviction (1h).
- `app/mcp_tools.py` — dual-mode MCP loader + circuit breaker + user-token ContextVar.
- `app/governance.py`, `app/tool_integrity.py`, `app/agentmesh_integration.py` — the three write-safety layers.
- `app/tools/` — tool builders (`manage_compensation`, `query_payroll`, `detect_anomalies`).
- `app/skills/` — runtime skills loaded via `load(path)`.
- `governance/policy.yaml`, `governance/tool-hashes.json` — AGT policy + integrity manifest.
- `mcp-mock.json` — mock MCP servers/tools for tests (ec-payroll, ec-tax-declaration, ec-compensation).
- `prebuilt_tests/` — structure/server tests; **never modify**. `tests/` — agent-owned unit + integration tests.
- `asset.yaml` / `../../solution.yaml` — SAP BTP deployment descriptors.
