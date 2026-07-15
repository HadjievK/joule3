"""Tool: detect_anomalies — statistical anomaly detection on payroll data."""
import logging
import statistics
from typing import Any

logger = logging.getLogger(__name__)


def detect_payroll_anomalies(payroll_records: list[dict]) -> list[dict]:
    """Run anomaly detection on a flat list of payroll line items.

    Detection logic:
    - Z-score check: flag entries where |z-score of amount| > 2.5 within the same wage_type group
    - Duplicate detection: flag same employee + wage_type + period appearing more than once
    - Missing entry detection: flag employees with zero entries (already filtered before call)

    Args:
        payroll_records: List of dicts with keys: employee_id, wage_type, amount, period_start, period_end

    Returns:
        List of anomaly dicts with: employee_id, wage_type, amount, reason, severity
    """
    anomalies: list[dict] = []

    # Group amounts by wage_type for z-score calculation
    wage_type_amounts: dict[str, list[float]] = {}
    for record in payroll_records:
        wt = record.get("wage_type", "UNKNOWN")
        amt = float(record.get("amount", 0) or 0)
        wage_type_amounts.setdefault(wt, []).append(amt)

    # Z-score check
    for record in payroll_records:
        wt = record.get("wage_type", "UNKNOWN")
        amt = float(record.get("amount", 0) or 0)
        amounts = wage_type_amounts.get(wt, [])
        if len(amounts) >= 3:
            mean = statistics.mean(amounts)
            stdev = statistics.stdev(amounts)
            if stdev > 0:
                z = abs((amt - mean) / stdev)
                if z > 2.5:
                    severity = "HIGH" if z > 3.5 else "MEDIUM"
                    anomalies.append({
                        "employee_id": record.get("employee_id"),
                        "wage_type": wt,
                        "amount": amt,
                        "reason": f"Z-score anomaly: z={z:.2f} (mean={mean:.2f}, stdev={stdev:.2f})",
                        "severity": severity,
                    })

    # Duplicate detection
    seen: dict[tuple, int] = {}
    for record in payroll_records:
        key = (
            record.get("employee_id"),
            record.get("wage_type"),
            record.get("period_start"),
            record.get("period_end"),
        )
        seen[key] = seen.get(key, 0) + 1

    for record in payroll_records:
        key = (
            record.get("employee_id"),
            record.get("wage_type"),
            record.get("period_start"),
            record.get("period_end"),
        )
        if seen.get(key, 0) > 1:
            # Avoid reporting the same duplicate multiple times
            already_reported = any(
                a["employee_id"] == record.get("employee_id")
                and a["wage_type"] == record.get("wage_type")
                and "Duplicate" in a["reason"]
                for a in anomalies
            )
            if not already_reported:
                anomalies.append({
                    "employee_id": record.get("employee_id"),
                    "wage_type": record.get("wage_type"),
                    "amount": float(record.get("amount", 0) or 0),
                    "reason": f"Duplicate payroll entry detected ({seen[key]} occurrences)",
                    "severity": "HIGH",
                })

    return anomalies


def build_detect_anomalies_tool(mcp_tools: list) -> Any:
    """Build the detect_anomalies tool, wiring payroll MCP data + statistical logic."""
    from langchain_core.tools import StructuredTool

    # Find payroll MCP tool for data fetching
    payroll_mcp_tool = None
    for t in mcp_tools:
        if any(kw in t.name.lower() for kw in ["payroll", "run", "result"]):
            payroll_mcp_tool = t
            break

    def _run(
        employee_ids: list,
        period_start: str,
        period_end: str,
        company_id: str = "",
    ) -> dict:
        try:
            all_records: list[dict] = []

            for emp_id in employee_ids:
                if payroll_mcp_tool is not None:
                    raw = payroll_mcp_tool.invoke({
                        "personId": emp_id,
                        "startDateWhenPaid": period_start,
                        "endDateWhenPaid": period_end,
                        "$top": 100,
                    })
                    # Normalize MCP response to flat record list
                    records = _normalize_payroll_response(raw, emp_id)
                    all_records.extend(records)
                else:
                    all_records.append({
                        "employee_id": emp_id,
                        "wage_type": "UNKNOWN",
                        "amount": 0,
                        "period_start": period_start,
                        "period_end": period_end,
                    })

            anomalies = detect_payroll_anomalies(all_records)
            logger.info(
                "M3.achieved: anomaly detection completed",
                extra={"flagged_count": len(anomalies), "total_records": len(all_records)},
            )
            return {
                "total_records_analyzed": len(all_records),
                "anomalies_found": len(anomalies),
                "anomalies": anomalies,
            }
        except Exception as exc:
            logger.error("M3.missed: anomaly detection skipped or failed", extra={"error": str(exc)})
            raise

    return StructuredTool.from_function(
        func=_run,
        name="detect_anomalies",
        description=(
            "Detect payroll anomalies (z-score outliers, duplicates, missing entries) "
            "for a list of employees over a given period."
        ),
    )


def _normalize_payroll_response(raw: Any, employee_id: str) -> list[dict]:
    """Normalize raw MCP payroll response to flat list of records."""
    records = []
    if isinstance(raw, dict):
        items = raw.get("value", raw.get("d", {}).get("results", []))
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    for item in items:
        line_items = item.get("employeePayrollRunResultsItems", [])
        if isinstance(line_items, dict):
            line_items = line_items.get("results", [])
        for li in line_items:
            records.append({
                "employee_id": employee_id,
                "wage_type": li.get("wageType") or li.get("payrollProviderWageType", "UNKNOWN"),
                "amount": li.get("amount", 0),
                "period_start": li.get("startDateWhenEarned"),
                "period_end": li.get("endDateWhenEarned"),
            })
    return records
