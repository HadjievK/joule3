"""Risky write tools: update_salary and update_bonus_eligibility.

Both tools enforce a mandatory human-in-the-loop confirmation gate:

    1. Agent calls the tool with confirmed=False (default).
    2. Tool returns a structured CONFIRMATION_REQUIRED payload — never touches SAP.
    3. Agent surfaces the payload to the user and sets require_user_input=True.
    4. User replies "yes / confirm / approved".
    5. Agent calls the tool again with confirmed=True → write executes.

In local/test mode (IBD_TESTING=1) the tools return deterministic mock
responses without touching any MCP server.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

_TESTING = os.environ.get("IBD_TESTING") == "1"


def _find_mcp_tool(mcp_tools: list, *keywords: str) -> Any | None:
    for t in mcp_tools:
        name_lower = t.name.lower()
        if all(kw.lower() in name_lower for kw in keywords):
            return t
    for t in mcp_tools:
        name_lower = t.name.lower()
        if any(kw.lower() in name_lower for kw in keywords):
            return t
    return None


def _audit_log(operation: str, payload: dict) -> None:
    logger.warning(
        "AUDIT | operation=%s | ts=%s | payload=%s",
        operation,
        datetime.now(timezone.utc).isoformat(),
        json.dumps(payload, default=str),
    )


def _confirmation_required(operation: str, summary: dict) -> str:
    return json.dumps(
        {
            "status": "CONFIRMATION_REQUIRED",
            "operation": operation,
            "summary": summary,
            "instruction": (
                "Please review the changes above carefully. "
                "Reply 'confirm' or 'yes' to proceed, or 'cancel' to abort."
            ),
        },
        indent=2,
    )


def build_update_salary_tool(mcp_tools: list) -> StructuredTool:
    """Build the update_salary tool, wired to the compensation MCP tool.

    Parameters:
        userId              – employee userId (e.g. 'alice.johnson')
        new_annual_salary   – new annual salary as decimal string (e.g. '88000.00')
        currency            – ISO 4217 code (default 'USD')
        effective_date      – YYYY-MM-DD (defaults to today)
        change_reason       – free-text reason
        confirmed           – must be True to execute the write (default False)
    """
    write_tool = _find_mcp_tool(mcp_tools, "update", "compensation")

    async def _run(
        userId: str,
        new_annual_salary: str,
        currency: str = "USD",
        effective_date: str = "",
        change_reason: str = "Salary update",
        confirmed: bool = False,
    ) -> str:
        try:
            annual = float(new_annual_salary)
            monthly = round(annual / 12, 2)
        except ValueError:
            return json.dumps({"status": "ERROR", "message": f"Invalid salary value: {new_annual_salary}"})

        eff_date = effective_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not confirmed:
            return _confirmation_required(
                operation="update_salary",
                summary={
                    "employee_userId": userId,
                    "new_annual_salary": f"{annual:,.2f} {currency}",
                    "new_monthly_salary": f"{monthly:,.2f} {currency}",
                    "effective_date": eff_date,
                    "change_reason": change_reason,
                    "warning": (
                        "This is a WRITE operation that will modify payroll-relevant "
                        "compensation data in SAP SuccessFactors Employee Central."
                    ),
                },
            )

        _audit_log(
            "update_salary",
            {
                "userId": userId,
                "new_annual_salary": new_annual_salary,
                "currency": currency,
                "effective_date": eff_date,
                "change_reason": change_reason,
            },
        )

        if _TESTING or write_tool is None:
            if write_tool is None:
                logger.warning("update_salary: no compensation MCP write tool found — returning mock response")
            return json.dumps(
                {
                    "status": "Updated",
                    "userId": userId,
                    "annualSalary": f"{annual:.2f}",
                    "monthlySalary": f"{monthly:.2f}",
                    "currency": currency,
                    "effectiveDate": eff_date,
                    "changeReason": change_reason,
                    "updatedBy": "agent (mock)",
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )

        try:
            result = await write_tool.acall(
                {
                    "userId": userId,
                    "annualSalary": f"{annual:.2f}",
                    "currency": currency,
                    "effectiveDate": eff_date,
                    "reason": change_reason,
                }
            )
            logger.info("update_salary: MCP write completed for userId=%s", userId)
            return str(result)
        except Exception as exc:
            logger.exception("update_salary: MCP write failed for userId=%s", userId)
            return json.dumps({"status": "ERROR", "message": str(exc)})

    return StructuredTool.from_function(
        coroutine=_run,
        name="update_salary",
        description=(
            "⚠️ RISKY WRITE — Update an employee's annual salary in SAP SuccessFactors "
            "Employee Central. ALWAYS call first with confirmed=False to show the user a "
            "summary. Only call with confirmed=True after the user explicitly approves. "
            "Required: userId, new_annual_salary. Optional: currency (default USD), "
            "effective_date (default today), change_reason."
        ),
        handle_tool_error=True,
    )


def build_update_bonus_eligibility_tool(mcp_tools: list) -> StructuredTool:
    """Build the update_bonus_eligibility tool, wired to the compensation MCP tool.

    Parameters:
        userId                – employee userId (e.g. 'dave.wilson')
        eligible_for_bonus    – True to enable, False to remove bonus eligibility
        target_bonus_percent  – target bonus % of annual salary (e.g. '10.00')
        effective_date        – YYYY-MM-DD (defaults to today)
        change_reason         – free-text reason
        confirmed             – must be True to execute the write (default False)
    """
    write_tool = _find_mcp_tool(mcp_tools, "update", "compensation")

    async def _run(
        userId: str,
        eligible_for_bonus: bool,
        target_bonus_percent: str = "0.00",
        effective_date: str = "",
        change_reason: str = "Bonus eligibility update",
        confirmed: bool = False,
    ) -> str:
        try:
            bonus_pct = float(target_bonus_percent)
        except ValueError:
            return json.dumps({"status": "ERROR", "message": f"Invalid bonus percent: {target_bonus_percent}"})

        eff_date = effective_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not confirmed:
            return _confirmation_required(
                operation="update_bonus_eligibility",
                summary={
                    "employee_userId": userId,
                    "eligible_for_bonus": eligible_for_bonus,
                    "target_bonus_percent": f"{bonus_pct:.2f}%",
                    "effective_date": eff_date,
                    "change_reason": change_reason,
                    "warning": (
                        "This is a WRITE operation that will modify bonus eligibility "
                        "and target bonus percentage in SAP SuccessFactors Employee Central. "
                        "Changes affect future payroll runs immediately."
                    ),
                },
            )

        _audit_log(
            "update_bonus_eligibility",
            {
                "userId": userId,
                "eligible_for_bonus": eligible_for_bonus,
                "target_bonus_percent": target_bonus_percent,
                "effective_date": eff_date,
                "change_reason": change_reason,
            },
        )

        if _TESTING or write_tool is None:
            if write_tool is None:
                logger.warning("update_bonus_eligibility: no compensation MCP write tool found — returning mock response")
            return json.dumps(
                {
                    "status": "Updated",
                    "userId": userId,
                    "eligibleForBonus": eligible_for_bonus,
                    "targetBonusPercent": f"{bonus_pct:.2f}",
                    "effectiveDate": eff_date,
                    "changeReason": change_reason,
                    "updatedBy": "agent (mock)",
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )

        try:
            result = await write_tool.acall(
                {
                    "userId": userId,
                    "eligibleForBonus": eligible_for_bonus,
                    "targetBonusPercent": f"{bonus_pct:.2f}",
                    "effectiveDate": eff_date,
                    "reason": change_reason,
                }
            )
            logger.info("update_bonus_eligibility: MCP write completed for userId=%s", userId)
            return str(result)
        except Exception as exc:
            logger.exception("update_bonus_eligibility: MCP write failed for userId=%s", userId)
            return json.dumps({"status": "ERROR", "message": str(exc)})

    return StructuredTool.from_function(
        coroutine=_run,
        name="update_bonus_eligibility",
        description=(
            "⚠️ RISKY WRITE — Enable or disable bonus eligibility and set target bonus % "
            "for an employee in SAP SuccessFactors Employee Central. "
            "ALWAYS call first with confirmed=False to show the user a summary and wait "
            "for explicit approval. Only call with confirmed=True after the user says yes. "
            "Required: userId, eligible_for_bonus. "
            "Optional: target_bonus_percent (default '0.00'), effective_date, change_reason."
        ),
        handle_tool_error=True,
    )
