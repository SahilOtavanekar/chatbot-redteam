# CLAUDE.md — chatbot-redteam

An endpoint-agnostic framework that runs a predefined suite of test prompts against any
chatbot API **in parallel**, judges each reply deterministically, queues the rest for human
review, and generates an OWASP-aligned report. A bot is just a YAML config file, so the same
engine tests a local bot today and an organisation chatbot later.

**Status (2026-10-23): spec approved; implementation plan awaiting approval; no code yet.** Read
`docs/HANDOFF.md` first — it is the resume point. Spec: `docs/specs/2026-10-23-v0.1-design.md`
(see §16). Plan: `docs/superpowers/plans/2026-10-23-chatbot-redteam-v0.1.md`.

This project was split out of the `ai-red-teaming` lab (at `C:\dev\ai-red-teaming`). The lab's
deliberately vulnerable bot is this framework's first owned test target.

## Hard rules

- **Claude never writes attack prompts or planted injection text.** Sahil provides those; Claude
  transcribes them verbatim into prompt-pack files and builds / validates / runs / reports
  everything else. (Drafting attack wording trips the safety classifier and is out of scope.)
- **Judgement is deterministic, never an LLM.** Fixed rules decide Pass/Fail; a person reviews the
  rest. **Inconclusive is never counted as a Pass** (errors, timeouts, empty/unreadable replies).
- **Test only authorised targets:** targets we own, local targets, or purpose-built/authorised
  challenges (e.g. Gandalf). An organisation bot is tested only with the bot owner's go-ahead.
  Never point the framework at a third-party production chatbot without permission.
- **PyRIT is a library dependency, pinned `==1.1.0`, isolated behind one adapter module.** Only
  that module imports `pyrit`, so upstream renames touch one file. We do NOT adopt PyRIT's engine,
  CLI or GUI (CoPyRIT) — see `docs/pyrit-analysis.md`.
- **No secrets in the repo.** Values live in `.env` (git-ignored); names in `.env.example`.
- **Evidence may contain real data** (for org bots): it stays in the git-ignored `results/` folder;
  redaction on by default. Synthetic data only for our own test targets.
- **TDD for code under `src/`:** write the failing test, watch it fail, then implement.

## Working style

- Plan → get approval → implement → honest report. State the step's outcome, how it moves toward
  the goal, and what comes next.
- Label each test result **Executed / Design-only / Not Testable**; never invent results or runs.
- Explain the concept before building it (Sahil is learning); cite primary sources (OWASP LLM Top
  10, NIST AI 600-1).
- Work on a branch; open a PR; merge after CI is green. Check ports before starting a server.

## Planned layout (not built yet — see the spec)

```
src/suite/   config, adapters(PyRIT), prompts, engine, judge, evidence, report, review, compare, cli
endpoints/   one YAML per bot (lab-bot, gandalf, <org-bot> later)
prompts/     prompt packs (YAML/CSV); Sahil authors the prompt text
results/     git-ignored run folders (run.json, memory.db, matrix.csv, report.*, review.csv)
tests/       pytest, one suite per component
docs/        specs/, HANDOFF.md, pyrit-analysis.md, pyrit-spike-findings.md
```

## Commands (once scaffolded)

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
uv run mypy src
# planned CLI:
uv run suite validate --endpoint endpoints/lab-bot.yaml --pack prompts/core
uv run suite run      --endpoint endpoints/lab-bot.yaml --pack prompts/core
uv run suite finalize --run results/<run-id>
uv run suite compare  --baseline results/<r1> --run results/<r2>
```

## Next step

Sahil approves the plan (and spec §16 revisions) and picks an execution method → build v0.1 with
TDD, task by task, validating against mock + lab bot + Gandalf.
