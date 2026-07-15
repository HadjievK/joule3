"""Tool: query_payroll — retrieves payroll run results from SAP SuccessFactors ECP via MCP tools."""
import logging
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
def query_payroll(
    employee_id: str,
    period_start: str,
    period_end: str,
    company_id: str = "",
) -> dict[str, Any]:
    """Query payroll run results for an employee from SAP SuccessFactors Employee Central Payroll.

    Args:
        employee_id: The employee's person ID or user ID in SuccessFactors.
        period_start: Start of the payroll period in ISO date format (YYYY-MM-DD).
        period_end: End of the payroll period in ISO date format (YYYY-MM-DD).
        company_id: Optional company ID to filter results.

    Returns:
        Dictionary with payroll run results and line items.
    """
    raise NotImplementedError(
        "query_payroll must be called through the MCP tool loader. "
        "Direct invocation is not supported — use get_mcp_tools() to obtain the live tool."
    )


def build_query_payroll_tool(mcp_tools: list) -> Any:
    """Build the query_payroll tool from available MCP tools.

    Searches for the MCP tool that exposes EmployeePayrollRunResults and wraps it
    with milestone logging.
    """
    payroll_tool = _find_mcp_tool(mcp_tools, keywords=["payroll", "run", "result"])
    if payroll_tool is None:
        logger.warning("M2.missed: no MCP tool found for payroll run results")
        return _make_stub_tool("query_payroll", "Query payroll run results (stub — no MCP tool available)")

    from langchain_core.tools import StructuredTool

    original_func = payroll_tool.func if hasattr(payroll_tool, "func") else None

    def _run(**kwargs: Any) -> Any:
        try:
            result = payroll_tool.invoke(kwargs) if original_func is None else original_func(**kwargs)
            logger.info("M2.achieved: payroll data retrieved successfully")
            return result
        except Exception as exc:
            logger.warning("M2.missed: OData call failed or returned no results", extra={"error": str(exc)})
            raise

    return StructuredTool.from_function(
        func=_run,
        name=payroll_tool.name,
        description=payroll_tool.description,
    )


def _find_mcp_tool(tools: list, keywords: list[str]) -> Any:
    """Find an MCP tool whose name matches any of the given keywords (case-insensitive)."""
    for tool in tools:
        tool_name_lower = tool.name.lower()
        if any(kw.lower() in tool_name_lower for kw in keywords):
            return tool
    return None


def _make_stub_tool(name: str, description: str) -> Any:
    """Return a stub LangChain tool that raises a clear error when invoked."""
    from langchain_core.tools import StructuredTool

    def _stub(**kwargs: Any) -> str:
        return f"Tool '{name}' is not available: no matching MCP server found."

    return StructuredTool.from_function(func=_stub, name=name, description=description)
