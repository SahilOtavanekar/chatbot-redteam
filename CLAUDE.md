# CLAUDE.md — chatbot-redteam

An endpoint-agnostic framework that runs a predefined suite of test prompts against any
chatbot API **in parallel**, judges each reply deterministically, queues the rest for human
review, and generates an OWASP-aligned report. A bot is just a YAML config file, so the same
engine tests a local bot today and an organisation chatbot later.

**Status (2026-10-26): v0.1 built (Tasks 1–16 done, final review fixes applied); Tasks 17–18
wait for Sahil's prompt text; branch `feat/v0.1` pushed, CI green.**
Read `docs/HANDOFF.md` first — it is the resume point. Spec: `docs/specs/2026-10-23-v0.1-design.md`
(see §16). Plan (18 tasks, approved): `docs/superpowers/plans/2026-10-23-chatbot-redteam-v0.1.md`.
Mock validation: `docs/validation/v0.1-mock.md`.

## How to resume the build (next session)

1. `git switch feat/v0.1` (should already be checked out); `uv sync`; `uv run pytest -q` → expect
   655 passed, 1 deselected (the live Gandalf test) on Python 3.13 (`.python-version` pins it).
2. The build runs with **superpowers:subagent-driven-development** (Sahil's choice). Its ledger is
   `.superpowers/sdd/2026-10-23-chatbot-redteam-v0.1/progress.md` — **git-ignored, local only**.
   Read it first: it has every ruling and deferred minor. Trust the ledger + `git log` over memory;
   never re-dispatch a task marked `complete`.
3. Next action: Tasks 17–18, once Sahil supplies the prompt text (Claude transcribes it verbatim;
   re-check the Gandalf API before Task 18). Then superpowers:finishing-a-development-branch: open
   a PR from `feat/v0.1` to `main`, merge after CI is green.
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

## Layout (✔ = built; full map in the plan's "File map")

```
src/suite/   ✔ models, config, prompts, patterns, planner, extract, engine, judge,
             adapters/ (only PyRIT importer), evidence, report, run, review, compare, cli
endpoints/   ✔ one YAML per bot (mock, lab-bot, gandalf-baseline/-mid/-hardest);
             private/ subdir git-ignored
prompts/     ✔ prompt packs (YAML/CSV): smoke (for the mock); lab-bot and Gandalf packs wait for
             Sahil's text (Tasks 17–18); private/ subdir git-ignored
results/     git-ignored run folders (run.json, evidence.db, matrix.csv, report.*, review.csv)
tests/       ✔ pytest, one suite per component (+ mockbot.py, factories.py, fakes.py)
docs/        specs/, superpowers/plans/, HANDOFF.md, pyrit-*.md, validation/ (v0.1-mock.md)
```

Decided 2026-10-23: v0.2 UI = **FastAPI + HTMX + Jinja2** with a JSON API (`/api/v1`) underneath, so
a FastAPI + React frontend can replace the views later (spec §16.6). Not Streamlit, not CoPyRIT.

## Commands

```bash
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy
# CLI against the local mock bot. Check port 8199 is free first, then in another terminal:
uv run python tests/mockbot.py --port 8199
uv run suite validate --endpoint endpoints/mock.yaml --pack prompts/smoke   # sends nothing
uv run suite run      --endpoint endpoints/mock.yaml --pack prompts/smoke
uv run suite finalize --run results/<run-id>
uv run suite compare  --baseline results/<r1> --run results/<r2>
```

## Next step

Tasks 17–18 once Sahil supplies the prompt text, then finishing-a-development-branch (PR) — see
"How to resume" above.
