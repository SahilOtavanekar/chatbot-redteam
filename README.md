# chatbot-redteam

A framework that runs a predefined suite of security test prompts against any chatbot API
**in parallel**, judges each reply deterministically (fixed rules + human review for the rest),
and produces an OWASP-aligned report. A bot is described by one YAML file, so the same engine
tests a local bot today and an organisation chatbot later.

> **Status: v0.1 built and validated on a mock**
> ([`docs/validation/v0.1-mock.md`](docs/validation/v0.1-mock.md)); lab-bot and Gandalf validation
> pending. Start at [`docs/HANDOFF.md`](docs/HANDOFF.md), then [`CLAUDE.md`](CLAUDE.md) and the spec at
> [`docs/specs/2026-10-23-v0.1-design.md`](docs/specs/2026-10-23-v0.1-design.md).

## Why

Point the framework at a chatbot's endpoint, run 100+ prompts automatically and in parallel, and
get a report with Pass / Fail / Inconclusive per prompt and per OWASP LLM category, with evidence.
New prompts are added as files, no code change. Built so testing an organisation's own chatbots
later is a config change, not a rewrite.

## How it works (v0.1)

```
endpoint YAML + prompt packs → validate (dry run) → run in parallel (capped)
  → extract reply → judge (rules → hints → human review) → evidence (SQLite) → report (HTML/MD/CSV)
  → retest & compare
```

- **Own thin async engine**; PyRIT 1.1.0 used as a library for specific pieces (HTTP & Gandalf
  targets, a JSON-escaping converter, regex-based output detectors) behind one adapter package.
- **Our own evidence store**, `evidence.db` (SQLite, one per run), not PyRIT's memory: reports,
  review and compare read only our files (spec §16.2).
- **Deterministic judging only.** Inconclusive (errors/timeouts/empty replies) is never a Pass.
- Validated against a mock server ([`docs/validation/v0.1-mock.md`](docs/validation/v0.1-mock.md)).
  Validation against the `ai-red-teaming` lab bot and Gandalf is pending (it needs Sahil's prompt
  text).

## Quickstart

```bash
uv sync
# start the local mock bot in another terminal (check that port 8199 is free first):
uv run python tests/mockbot.py --port 8199

uv run suite validate --endpoint endpoints/mock.yaml --pack prompts/smoke   # dry run, sends nothing
uv run suite run      --endpoint endpoints/mock.yaml --pack prompts/smoke   # run folder under results/
uv run suite finalize --run results/<run-id>                                # after filling review.csv
uv run suite compare  --baseline results/<run-1> --run results/<run-2>      # fixed / regressed / unchanged
```

- `prompts/smoke` is the shipped smoke pack: 11 prompts, 106 attempts, written for the mock bot.
- `validate` prints the plan (prompts, attempts, limits, `authorized:`) and refuses a plan that has
  no `authorized:` reference or that exceeds `max_requests`. `run` repeats that check first.
- `run` ends with `fail N · pass N · inconclusive N · needs_review N` (attempt counts) and the run
  folder. Open `review.csv` there, type `pass` or `fail` in the `verdict` column for each row, then
  run `finalize`.
- `results/` is git-ignored: evidence may contain real data. It holds `run.json`, `evidence.db`,
  `report.html`, `report.md`, `matrix.csv` and `review.csv` for each run.
- Exit codes: 0 ok, 2 for a bad input file or a refused plan, 1 for anything unexpected, 130 for
  Ctrl+C (the partial run folder is kept). Errors print as one `error: ...` line, never a traceback.
- A run that **aborts** (too many consecutive errors, `abort_after_errors`) still exits 0, with a
  `warning:` line on stderr; its unsent attempts show as Inconclusive in the report, never as Pass.

### Private org-bot files

Endpoint files and prompt packs for an organisation's chatbot may hold internal names, URLs or
planted test records. Keep them in `endpoints/private/` and `prompts/private/`; both folders are
git-ignored. The CLI accepts any path, so they work like the shipped files. Every endpoint file needs
an `authorized:` line naming the bot owner's go-ahead (a ticket or an email reference); `suite run`
refuses without it.

## Docs

- [`docs/HANDOFF.md`](docs/HANDOFF.md) — resume point: state, decisions, next step.
- [`docs/specs/2026-10-23-v0.1-design.md`](docs/specs/2026-10-23-v0.1-design.md) — v0.1 design spec.
- [`docs/pyrit-analysis.md`](docs/pyrit-analysis.md) — detailed PyRIT 1.1.0 analysis (incl. its GUI).
- [`docs/pyrit-spike-findings.md`](docs/pyrit-spike-findings.md) — verified PyRIT patterns + pitfalls.

## Related

Split out of the `ai-red-teaming` lab (`C:\dev\ai-red-teaming`), whose deliberately vulnerable bot
is this framework's first owned test target.
