---
name: tax-compliance-rules
description: Configurable tax compliance rules for income tax declaration validation in SAP SuccessFactors EC
---

# Tax Compliance Rules

These rules are applied during income tax declaration validation. All monetary thresholds are in the local currency configured in SuccessFactors. Update these values to match your country and company-specific tax regulations.

## Investment Deduction Limits (Section 80C / Equivalent)

| Category | Investment Type | Annual Limit | Notes |
|----------|----------------|--------------|-------|
| 80C | Life Insurance Premium | 150000 | Combined 80C limit |
| 80C | Public Provident Fund | 150000 | Combined 80C limit |
| 80C | ELSS Mutual Fund | 150000 | Combined 80C limit |
| 80C | NSC | 150000 | Combined 80C limit |
| 80D | Medical Insurance Premium (Self) | 25000 | Separate from 80C |
| 80D | Medical Insurance Premium (Senior Parent) | 50000 | Additional limit |
| HRA | House Rent Allowance | 0 | Calculated as min(actual HRA, 50% salary, actual rent - 10% salary) |

## Validation Rules

1. **Maximum 80C combined**: Sum of all 80C declarations must not exceed 150,000.
2. **Non-negative amounts**: All declaration amounts must be >= 0.
3. **Approved status required**: Only declarations with `approvalStatus = APPROVED` are considered for compliance.
4. **Fiscal year match**: Declaration `fiscalYear` must match the requested fiscal year.

## Severity Levels

- **HIGH**: Combined 80C exceeds annual limit by more than 20%
- **MEDIUM**: Any single declaration exceeds its individual limit
- **LOW**: Minor discrepancies (< 5% over limit)
