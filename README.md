# chatbot-redteam

A framework that runs a predefined suite of security test prompts against any chatbot API
**in parallel**, judges each reply deterministically (fixed rules + human review for the rest),
and produces an OWASP-aligned report. A bot is described by one YAML file, so the same engine
tests a local bot today and an organisation chatbot later.

> **Status: planning complete, not yet built.** Start at [`docs/HANDOFF.md`](docs/HANDOFF.md),
> then [`CLAUDE.md`](CLAUDE.md) and the spec at
> [`docs/specs/2026-10-23-v0.1-design.md`](docs/specs/2026-10-23-v0.1-design.md).

## Why

Point the framework at a chatbot's endpoint, run 100+ prompts automatically and in parallel, and
get a report with Pass / Fail / Inconclusive per prompt and per OWASP LLM category, with evidence.
New prompts are added as files, no code change. Built so testing an organisation's own chatbots
later is a config change, not a rewrite.

## How it will work (v0.1)

```
endpoint YAML + prompt packs → validate (dry run) → run in parallel (capped)
  → extract reply → judge (rules → hints → human review) → evidence (SQLite) → report (HTML/MD/CSV)
  → retest & compare
```

- **Own thin async engine**; PyRIT 1.1.0 used as a library for specific pieces (HTTP & Gandalf
  targets, encoding converters, rule-based scorers, evidence store) behind one adapter.
- **Deterministic judging only.** Inconclusive (errors/timeouts/empty replies) is never a Pass.
- Validated against a mock server, the `ai-red-teaming` lab bot, and Gandalf.

## Docs

- [`docs/HANDOFF.md`](docs/HANDOFF.md) — resume point: state, decisions, next step.
- [`docs/specs/2026-10-23-v0.1-design.md`](docs/specs/2026-10-23-v0.1-design.md) — v0.1 design spec.
- [`docs/pyrit-analysis.md`](docs/pyrit-analysis.md) — detailed PyRIT 1.1.0 analysis (incl. its GUI).
- [`docs/pyrit-spike-findings.md`](docs/pyrit-spike-findings.md) — verified PyRIT patterns + pitfalls.

## Related

Split out of the `ai-red-teaming` lab (`C:\dev\ai-red-teaming`), whose deliberately vulnerable bot
is this framework's first owned test target.
