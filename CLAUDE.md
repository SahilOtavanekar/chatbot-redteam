# CLAUDE.md — chatbot-redteam

An endpoint-agnostic framework that runs a predefined suite of test prompts against any
chatbot API **in parallel**, judges each reply deterministically, queues the rest for human
review, and generates an OWASP-aligned report. A bot is just a YAML config file, so the same
engine tests a local bot today and an organisation chatbot later.

**Status (paused 2026-10-25): v0.1 build in progress on branch `feat/v0.1` — Tasks 1–14 done
and reviewed (599 tests passing at dc9e212); Task 15 (CLI) implementer was still running at the
pause — its review has NOT started.**
Read `docs/HANDOFF.md` first — it is the resume point. Spec: `docs/specs/2026-10-23-v0.1-design.md`
(see §16). Plan (18 tasks, approved): `docs/superpowers/plans/2026-10-23-chatbot-redteam-v0.1.md`.

## How to resume the build (next session)

1. `git switch feat/v0.1` (should already be checked out); `uv sync`; `uv run pytest -q` → expect
   599+ passed on Python 3.13 (`.python-version` pins it; more if Task 15's commit landed).
2. The build runs with **superpowers:subagent-driven-development** (Sahil's choice). Its ledger is
   `.superpowers/sdd/2026-10-23-chatbot-redteam-v0.1/progress.md` — **git-ignored, local only**.
   Read it first: it has every ruling, deferred minor, and a `RESUME HERE` line. Trust the ledger
   + `git log` over memory; never re-dispatch a task marked `complete`.
3. Next action: check `git log` for a Task 15 commit after `dc9e212` (and `task-15-report.md` in
   the ledger folder). If present → review it (package `dc9e212..HEAD`); if absent or the tree is
   dirty → inspect, then re-dispatch Task 15. Then Task 16 (mock validation). Stop before Tasks
   17–18 — they need prompt text from Sahil. Then the final whole-branch review.
4. Sahil may first want to review progress; summarise from the ledger, don't re-run tasks.

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

## Layout (✔ = built so far; full map in the plan's "File map")

```
src/suite/   ✔ models, config, prompts, patterns, planner, extract, engine, judge,
             adapters/ (only PyRIT importer), evidence, report, run, review, compare · cli (Task 15)
endpoints/   one YAML per bot (mock, lab-bot, gandalf-*); private/ subdir git-ignored (Task 15)
prompts/     prompt packs (YAML/CSV); Sahil authors the text; private/ subdir git-ignored (Task 15)
results/     git-ignored run folders (run.json, evidence.db, matrix.csv, report.*, review.csv)
tests/       pytest, one suite per component (+ mockbot.py, factories.py, fakes.py)
docs/        specs/, superpowers/plans/, HANDOFF.md, pyrit-*.md, validation/ (later)
```

Decided 2026-10-23: v0.2 UI = **FastAPI + HTMX + Jinja2** with a JSON API (`/api/v1`) underneath, so
a FastAPI + React frontend can replace the views later (spec §16.6). Not Streamlit, not CoPyRIT.

## Commands

```bash
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy
# CLI (built in Task 15):
uv run suite validate --endpoint endpoints/lab-bot.yaml --pack prompts/core
uv run suite run      --endpoint endpoints/lab-bot.yaml --pack prompts/core
uv run suite finalize --run results/<run-id>
uv run suite compare  --baseline results/<r1> --run results/<r2>
```

## Next step

Resume the subagent-driven build at Task 15's review (see "How to resume" above).
