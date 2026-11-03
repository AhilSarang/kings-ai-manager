# AI Manager

A multi-agent AI operations platform for a wholesale distribution business. The owner messages it on WhatsApp like an employee: it logs tasks, delegates to specialist agents, asks for approval before doing anything consequential, executes, and reports back — 24/7.

```
WhatsApp / console
        │
        ▼
   Supervisor ──────────── task system (lifecycle, follow-ups, reminders)
        │
        ├── Email agent      (Gmail triage, drafts, send-on-approval)
        ├── Amazon agent     (listings, inventory, FBA workflows)
        └── PPC agent        (advertising experiments — see below)
        │
        ▼
   Action layer (validated JSON → permission check → execute)
        │
        ▼
   Append-only audit log · PostgreSQL
```

**Scale:** 160+ Python modules · 33,000+ lines · 1,100+ automated tests · FastAPI · PostgreSQL

## Design principles

These are the rules the whole codebase is built around:

1. **The LLM never touches the database or the send path.** It returns JSON validated against a strict schema; a permission-checked action layer decides whether that action may run, and only the action layer executes. A malformed or over-reaching proposal fails closed.
2. **Consequential actions require explicit approval.** Approval is PIN-gated over the messaging channel. The approved payload is hash-frozen at proposal time: what executes is byte-for-byte what was approved, or nothing.
3. **Every decision is audited.** Allowed or refused, every action lands in an append-only audit log.
4. **Deterministic work is plain code.** Status checks, lookups, summaries, and math are code paths, not model calls. The model is used only where judgment is actually needed.
5. **No code depends on which LLM backend is active.** Providers sit behind one interface and are swappable with a config change; the test suite runs against a fake backend, so the entire system is testable offline.

## The PPC agent

The flagship subsystem: an Amazon advertising agent that runs real experiments instead of blind bid automation.

- **Hypothesis → experiment → evidence.** It forms hypotheses from per-keyword, per-hour data, tests them as controlled experiments with hard spend limits and control groups, and promotes what works into a written playbook of rules, each tagged with its evidence level.
- **Context-aware attribution.** A sales dip during a stockout is not the fault of a bid change. The attribution logic checks confounders before crediting or blaming an action.
- **Knows what it can't know.** A keyword with 40 clicks a month can't prove anything in a week. Under-powered results are reported as "not enough data," never as findings.
- **Profit-aware.** Break-even ACOS is computed per ASIN from real FBA fees, not account-wide averages.
- **Self-scoring.** Every prediction is scored against what actually happened, so calibration is measured, not assumed.

## Testing

```bash
uv run pytest
```

The suite runs against an embedded PostgreSQL instance and a fake LLM backend — no network, no API keys, no mocks of our own code. Coverage is organized per milestone; every feature ships with its tests.

## What it doesn't do

- It doesn't self-retrain. Learning speed is bounded by real click volume, and the playbook grows at the pace the data justifies.
- It doesn't act without approval on anything consequential. That's a feature.
- This public mirror excludes internal operations docs and deployment configuration.
