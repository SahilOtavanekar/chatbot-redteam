# HANDOFF — chatbot-redteam (resume here)

Last updated: 2026-10-23. Written so a fresh session in `C:\dev\chatbot-redteam` can continue
with no prior conversation. Read this, then `CLAUDE.md`, then the spec.

## What this project is

A framework that takes any chatbot's API endpoint, runs a predefined prompt suite against it
**in parallel** (100+ prompts), judges each reply by fixed rules (+ human review for the rest),
and produces an OWASP-aligned report. Endpoint-agnostic: a bot is a YAML file, so the lab bot now
and an org chatbot later are the same code path. Goal stated by Sahil's manager: eventually test
the organisation's own chatbots with this.

Split out of the `ai-red-teaming` lab (`C:\dev\ai-red-teaming`), whose vulnerable bot is the first
owned test target.

## Where we are

- **Spec approved (2026-10-23)** — `docs/specs/2026-10-23-v0.1-design.md`; §16 logs the
  revisions found while planning, plus the UI decision (§16.6: FastAPI + HTMX + Jinja2 for v0.2).
- **Plan approved; execution = subagent-driven** (fresh implementer + reviewer per task):
  `docs/superpowers/plans/2026-10-23-chatbot-redteam-v0.1.md` (18 tasks, TDD). Sahil also approved
  git-ignored `endpoints/private/` + `prompts/private/` (added to Task 15).
- **Build paused 2026-10-23 at Sahil's request, on branch `feat/v0.1`** (git initialised; `main`
  holds only the planning docs):
  | Task | State | Commits |
  |---|---|---|
  | 1 Scaffold, shared types, PyRIT boundary test | complete, review clean | ab54a0a |
  | 2 Endpoint config (+ fix: secrets masked everywhere, bad `${...}` rejected) | complete, review clean | 32ac2a9, 4645e42 |
  | 3 Prompt packs + pattern library | built; review = **needs fixes** (2 Important) | ebd8673 |
  Test suite: 112 passed on Python 3.13 (Executed by the Task 3 implementer). Task 3's open
  findings: the card-number redaction regex destroys run ids/dates (spec N4), and the email regex
  is quadratic on very long replies — exact findings + rulings in
  `.superpowers/sdd/2026-10-23-chatbot-redteam-v0.1/task-3-findings-round1.md`.
- The SDD **ledger** `.superpowers/sdd/2026-10-23-chatbot-redteam-v0.1/progress.md` (git-ignored,
  this machine only) holds every ruling and deferred minor finding. Read it before resuming.
- A **PyRIT 1.1.0 spike was run** (Executed, throwaway) and proved the core approach. Findings and
  working code patterns: `docs/pyrit-spike-findings.md`. Full tool analysis: `docs/pyrit-analysis.md`.

## Decisions settled (don't re-litigate)

1. **Own thin engine, PyRIT as a library for specific pieces** (not PyRIT's engine/CLI/GUI). The
   spike showed PyRIT's engine is 6–8× slower than a plain async loop and scores broken requests as
   passes.
2. **Judging = rules → hints → human review.** No LLM verdict (local 3B is too weak; proven in the
   lab's TL-04). Inconclusive is never a Pass.
3. **Endpoint-agnostic via YAML config files.** Auth pluggable. Per-bot concurrency + rate + budget
   caps. Redaction on by default.
4. **PyRIT pinned `==1.1.0`, isolated behind one `adapters/` module.**
5. **GUI (revised 2026-10-23 by Sahil):** not CoPyRIT (it reads PyRIT's memory, so it can't show
   our runs). **FastAPI + HTMX + Jinja2** over our own modules, added in **v0.2**, bound to
   127.0.0.1. Keep a JSON API (`/api/v1`) under the HTMX views so a **FastAPI + React** frontend
   (CoPyRIT's style) can replace the views later without touching logic. Replaces the earlier
   Streamlit plan. v0.1 ships CLI + static HTML report. (Pull the review screen
   into v0.1 only if the demo needs it.)
6. **Validation targets:** mock server, the lab bot, and **Gandalf** (via PyRIT's `GandalfTarget` +
   `GandalfScorer`). Org bots are designed-for but not demoed in v0.1.
7. Repo name chosen by Sahil: **chatbot-redteam**.

## PyRIT pieces we use in v0.1 (all behind `adapters/`)

`HTTPTarget` (shared httpx client + strict status callback), `JsonStringConverter`,
`CredentialLeakScorer`, `XSSOutputScorer`, `MarkdownInjectionScorer`, `GandalfTarget`, in-memory
central memory. **Not** `PromptSendingAttack`/`AttackExecutor` (too slow), **not** `GandalfScorer`
(needs an LLM). The two spike wrappers (JSON escaping, HTTP-status check) are mandatory.

## Open questions — resolved 2026-10-23 (spec §15)

HTML + Markdown + CSV reports; `pass_if` stays free text for humans; Gandalf demo = 3-level
subset (baseline, gpt-is-password-encoded, gandalf-the-white).

## Next step (the gate)

1. Sahil reviews progress (ledger + `git log feat/v0.1`); answer from the ledger, don't re-run tasks.
2. Resume superpowers:subagent-driven-development: Task 3 fix round 1 (FIX_BASE `ebd8673`) →
   scoped re-review → Tasks 4–16 without pausing → stop before 17–18 (need Sahil's prompt text) →
   final whole-branch review → finishing-a-development-branch (ask before creating a GitHub remote).

## Environment notes

- Machine: Windows 11, i7-1365U, 32 GB RAM, **no NVIDIA GPU** (Intel Iris Xe). PowerShell primary,
  Git Bash available. Python 3.13 via uv.
- Ollama has `granite4.1:3b` + `granite-embedding:30m` — enough for fixed-rule runs only, not for an
  LLM judge/attacker.
- The lab bot (first test target) runs from `C:\dev\ai-red-teaming` with
  `uv run python -m target` (binds 127.0.0.1:8000; health at `/health`, chat at `/chat`). It is
  **stateless** (no conversation history), so multi-turn needs a stateful mock for now.
- The PyRIT spike lived in a temp scratchpad (now disposable); its findings are captured in
  `docs/pyrit-spike-findings.md`, so nothing is lost when that folder is cleaned.

## Pre-seeded memory

Auto-memory for this folder was pre-seeded at
`C:\Users\SahilOtavanekar\.claude\projects\c--dev-chatbot-redteam\memory\`. If a new session's
memory path differs, this HANDOFF plus CLAUDE.md are the reliable source.
