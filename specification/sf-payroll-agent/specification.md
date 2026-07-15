# Specification: sf-payroll-agent

> **Guidelines**: Read [guidelines.md](../guidelines.md) and [guidelines-agent.md](../guidelines-agent.md) before executing ANY tasks below. Follow all constraints described there throughout execution.

## Basic Setup

- [ ] Read `product-requirements-document.md` and `intent.md` from the project root
- [ ] Bootstrap agent code in `assets/sf-payroll-agent/` using skill `sap-agent-bootstrap` (invoke from inside `assets/sf-payroll-agent/`, use copy commands — do NOT create files manually)
- [ ] Install dependencies, validate the agent starts and responds at `/.well-known/agent.json`

## Project-Specific Tasks

### Agent Identity & System Prompt

- [ ] Set agent name: `SF Payroll AI Agent`
- [ ] Set agent description: `AI agent for SAP SuccessFactors payroll operations: natural language queries, anomaly detection, tax compliance validation, and compensation management`
- [ ] Write system prompt in `app/agent.py` `@prompt_section` that:
  - Identifies the agent as a payroll intelligence assistant for SAP SuccessFactors
  - Instructs the agent NEVER to hallucinate payroll data — all data must come from MCP tool calls
  - Instructs the agent to set `$top=100` on all OData queries to prevent context overflow and inform the user when this limit applies
  - Instructs the agent to require explicit user confirmation before any write operation (compensation updates)
  - Lists all available tools and their purpose clearly

### Tool: query_payroll

- [ ] Implement `query_payroll` tool in `app/tools/query_payroll.py`:
  - Accepts: `employee_id` (str), `period_start` (str, ISO date), `period_end` (str, ISO date), `company_id` (str, optional)
  - Calls MCP tool that maps to `EmployeePayrollRunResults` and `EmployeePayrollRunResultsItems` entities (OData `sap.sf:apiResource:ECEmployeeCentralPayroll:v1`)
  - Returns payroll run results with line items (wage types, amounts, dates) for the given employee and period
  - Applies `$top=100` on all list requests
  - Formats response as structured dict with summary fields
- [ ] Register `query_payroll` in `app/agent.py` tools list

### Tool: detect_anomalies

- [ ] Implement `detect_anomalies` tool in `app/tools/detect_anomalies.py`:
  - Accepts: `employee_ids` (list[str]), `period_start` (str), `period_end` (str), `company_id` (str, optional)
  - Fetches payroll results for all given employees via `query_payroll` logic (reuses MCP call)
  - Applies statistical anomaly detection:
    - Z-score check on `amount` per wage type: flag entries where |z| > 2.5
    - Duplicate detection: flag same employee + wage type + period appearing more than once
    - Missing entry detection: flag employees with no payroll entry for expected period
  - Returns list of flagged anomalies with employee_id, wage_type, amount, reason, severity (high/medium/low)
- [ ] Register `detect_anomalies` in `app/agent.py` tools list

### Tool: validate_tax_compliance

- [ ] Implement `validate_tax_compliance` tool in `app/tools/validate_tax_compliance.py`:
  - Accepts: `employee_id` (str), `fiscal_year` (str), `company_id` (str, optional)
  - Calls MCP tool that maps to `ItDeclaration` entity (OData `sap.sf:apiResource:ECIncomeTaxDeclaration:v1`)
  - Loads tax compliance rules from `app/skills/tax-compliance-rules/SKILL.md` (runtime skill)
  - Evaluates each declaration's `amount` against thresholds per `declarationType` and `category`
  - Returns compliance result: passed declarations, violations list with rule name + recommended action
- [ ] Register `validate_tax_compliance` in `app/agent.py` tools list

### Tool: manage_compensation

- [ ] Implement `manage_compensation` tool in `app/tools/manage_compensation.py`:
  - Accepts: `employee_id` (str), `action` (str: "read" | "update"), `compensation_data` (dict, optional for updates), `start_date` (str, optional)
  - For `action="read"`: calls MCP tool mapping to `EmpCompensation` + `EmpPayCompRecurring` entities (OData `sap.sf:apiResource:ECCompensationInformation:v1`)
    - Returns salary, pay components, compa-ratio, yearly base salary
  - For `action="update"`: requires `compensation_data` dict with fields to update (e.g. `paycompvalue`, `currencyCode`)
    - ALWAYS returns a confirmation request before executing write — does NOT write without user confirmation
    - On confirmed update: calls MCP upsert tool for `EmpPayCompRecurring`
  - Applies `$top=100` on list requests
- [ ] Register `manage_compensation` in `app/agent.py` tools list

### Runtime Skill: Tax Compliance Rules

- [ ] Create runtime skill `app/skills/tax-compliance-rules/SKILL.md` with:
  - Frontmatter: `name: tax-compliance-rules`, `description: Configurable tax compliance rules for income tax declaration validation`
  - Body: rules table covering standard thresholds (e.g. 80C investment limit: ₹150,000; HRA ceiling rules; maximum TDS percentage per income bracket)
  - Note: rules are illustrative defaults; real values should be configured per tenant

### Business Step Instrumentation

- [ ] Add milestone logging to `app/tools/query_payroll.py`:
  - On successful data return: `logger.info("M2.achieved: payroll data retrieved successfully")`
  - On API error / empty result: `logger.warning("M2.missed: OData call failed or returned no results")`
- [ ] Add milestone logging to `app/tools/detect_anomalies.py`:
  - On completion: `logger.info("M3.achieved: anomaly detection completed", extra={"flagged_count": len(anomalies)})`
  - On exception: `logger.error("M3.missed: anomaly detection skipped or failed")`
- [ ] Add milestone logging to `app/tools/validate_tax_compliance.py`:
  - On completion: `logger.info("M4.achieved: tax compliance validation completed")`
  - On exception: `logger.error("M4.missed: tax compliance validation skipped or failed")`
- [ ] Add milestone logging to `app/tools/manage_compensation.py`:
  - On completion: `logger.info("M5.achieved: compensation action completed")`
  - On failure/decline: `logger.warning("M5.missed: compensation action failed or user declined")`
- [ ] Add M1 milestone in `app/agent.py` stream/invoke — after LLM intent classification selects a tool:
  - `logger.info("M1.achieved: intent classified and tool selected", extra={"tool": tool_name})`
  - On classification failure: `logger.warning("M1.missed: intent classification failed or ambiguous")`
- [ ] Add OpenTelemetry custom spans for each tool using decorator form `@tracer.start_as_current_span("tool_name")` on tool functions (NOT inside async generators)
- [ ] Extract all business logic from `stream()` into `_run_agent()` helper; instrument `_run_agent()` with OpenTelemetry
- [ ] Verify `auto_instrument()` is called at top of `main.py` before any AI framework imports

### MCP Integration (Path A — OData specs available)

- [ ] Verify `specification/sf-payroll-agent/api-specs/` contains:
  - `ECEmployeeCentralPayroll.edmx` (ORD ID: `sap.sf:apiResource:ECEmployeeCentralPayroll:v1`)
  - `ECIncomeTaxDeclaration.edmx` (ORD ID: `sap.sf:apiResource:ECIncomeTaxDeclaration:v1`)
  - `ECCompensationInformation.edmx` (ORD ID: `sap.sf:apiResource:ECCompensationInformation:v1`)
- [ ] Invoke `mcp-translation-file` skill for each EDMX spec file to generate `translation.json` artifacts. If `mcp-translation-file` is unavailable, log `[MCP-SKILL] mcp-translation-file unavailable — skipping` and proceed to testing with mocked tools
- [ ] Invoke `setup-solution` skill to create MCP server assets for each generated translation file
- [ ] Wire MCP tool loading in `app/agent.py` using `get_mcp_tools()` from `mcp_tools.py` (canonical pattern from guidelines)
- [ ] Add MCP server entries to `assets/sf-payroll-agent/asset.yaml` under `requires`:
  - One entry per generated MCP server (ECP, Tax, Compensation)
- [ ] Generate `mcp-mock.json` using `mcp-mock-config` skill (required before tests run)

### Cleanup

- [ ] Delete the template runtime skill: `rm -rf assets/sf-payroll-agent/app/skills/template-skill/`

## Testing

- [ ] `conftest.py` only sets `IBD_TESTING=true`
- [ ] Write unit test `tests/test_query_payroll.py` — mock MCP tool returning sample `EmployeePayrollRunResults`; verify structured output; run immediately after writing
- [ ] Write unit test `tests/test_detect_anomalies.py` — mock payroll data with one z-score outlier and one duplicate; verify both are flagged; run immediately after writing
- [ ] Write unit test `tests/test_validate_tax_compliance.py` — mock `ItDeclaration` data with one valid and one over-threshold declaration; verify violation is reported; run immediately after writing
- [ ] Write unit test `tests/test_manage_compensation.py` — test read path returns salary data; test update path returns confirmation request without writing; run immediately after writing
- [ ] Write integration test `tests/test_agent_integration.py` — mock LLM + MCP tools; call agent `invoke()` with "show payroll for employee 12345 for June 2025"; verify M1 and M2 milestone logs are emitted
- [ ] Run `pytest` from `assets/sf-payroll-agent/` (no args)
- [ ] Verify `grep -c "^@agent_model\|^@agent_config\|^@prompt_section" assets/sf-payroll-agent/app/agent.py` returns 3
- [ ] Run `pytest` again from `assets/sf-payroll-agent/` to generate final `test_report.json`
- [ ] Verify `test_report.json` exists in `assets/sf-payroll-agent/`
