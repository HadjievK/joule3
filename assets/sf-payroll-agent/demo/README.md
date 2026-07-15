# AGT Governance Demo — for leadership review

**Purpose:** show, in one command, why the SF Payroll agent should run behind the
**Microsoft Agent Governance Toolkit (AGT)** — and let you accept or reject the
toolkit on the strength of what you see.

It runs the **same set of risky payroll operations twice** — once with AGT
enforcing policy, once with no governance at all — and shows the difference.

```
cd assets/sf-payroll-agent
python demo/agt_demo.py
```

That prints a side-by-side table to the terminal and writes
**`demo/agt_demo_report.html`** (open it in any browser, or drop it on a slide).

---

## What you'll see

| Scenario (what the agent was asked to do) | AGT **OFF** | AGT **ON** |
|---|---|---|
| Set a salary to **$5,000,000** | ✅ executes | ⛔ **denied** (over $1M cap) |
| Grant a **500%** target bonus | ✅ executes | ⛔ **denied** (over 100% cap) |
| **"Give ALL employees a 10% raise"** | ✅ executes | ⏸ **approval required** (2 humans) |
| **Prompt injection** — "ignore previous instructions…" | ✅ executes | ⛔ **denied** |
| **PII** (an SSN) in the request | ✅ executes | ⛔ **denied** |
| A **legitimate** raise to $92,000 | ✅ executes | ✅ **executes** |

The headline: with governance off, **every** dangerous operation goes through
silently. With governance on, all five are stopped or sent for human approval —
and the one legitimate change still succeeds. **AGT is a gate, not a wall.**

---

## Why this is credible (not a mock-up)

- The **ON** column is enforced by the *real, installed* Microsoft toolkit
  (`agent-governance-toolkit`, shown in the report header with its version).
  Nothing about the governance decision is faked.
- The policy is a small, readable YAML file — **`demo/policy-demo.yaml`** — in
  the exact grammar the toolkit evaluates. You can read every rule in a minute.
- The payroll "writes" themselves are harmless local stubs, so the demo is safe
  to run on any laptop with no SAP connection, no LLM, and no network. Only the
  *governance decision* is real.
- The script exits `0` only when AGT behaves as designed on all scenarios, so it
  can also run in CI as a guardrail.

---

## How it works (30-second version)

1. For each scenario, the agent code computes plain-English risk **signals**
   (is there PII? a prompt-injection pattern? a bulk request?) and the money
   amounts involved.
2. Those signals + the requested action are handed to AGT's `govern()` wrapper,
   which checks them against `policy-demo.yaml` **before** the write is allowed
   to run.
3. AGT returns **allow**, **deny**, or **require approval** — and only on
   *allow* does the write actually execute.

This is the intended division of labour: **the application supplies facts, the
toolkit is the single, auditable decision point** for every sensitive action.

---

## Where this fits the SAP AI Security Framework

From the framework research (`SecurityAIF/SAP-AI-Security-Framework-Research.md`),
the recommended stack is three layers:

| Layer | Framework | Role |
|---|---|---|
| Risk taxonomy | Google SAIF 2.0 | *which* risks exist |
| Lifecycle spine | NCSC/CISA Guidelines | *when* to address them |
| **Enforcement runtime** | **Microsoft AGT** | ***how* to stop them at runtime** |

This demo exercises the third layer. AGT answers the three questions traditional
IAM/RBAC cannot, enforced per tool call:

1. **Is this action allowed?** — the deny/approval decisions above.
2. **Which agent did it?** — per-agent attribution (agent identity in the policy).
3. **Can you prove what happened?** — a tamper-evident audit trail of every decision.

Each scenario is tagged with the OWASP Agentic-AI (ASI) risk it illustrates
(ASI-01 Goal Hijack, ASI-02 Tool Misuse, ASI-03/06 Privilege & Blast Radius,
ASI-04 Data Exfiltration, ASI-05 Prompt Injection).

---

## Files in this folder

| File | What it is |
|---|---|
| `agt_demo.py` | The runnable demo (terminal table + HTML report). |
| `policy-demo.yaml` | The governance policy AGT enforces — read this to see the rules. |
| `agt_demo_report.html` | Generated report for sharing (created on each run). |
| `README.md` | This file. |

> The **production** agent already ships a fuller policy at
> `governance/policy.yaml` and a pytest suite proving the same enabled-vs-disabled
> behaviour (`tests/test_governance_enabled.py`, `tests/test_governance_disabled.py`).
> This `demo/` folder is the *leadership-facing* distillation of that work.

---

## Relationship to the production wiring

In production the agent wraps **every** tool through `app/governance.py`'s
`apply_governance()` before the LangGraph agent can call it (see
`app/agent_executor.py`). If the toolkit is ever *not* installed, that layer
degrades gracefully to a no-op and logs a warning — which is exactly the
"AGT OFF" column you see here. Adopting AGT means that column never happens in
production.
