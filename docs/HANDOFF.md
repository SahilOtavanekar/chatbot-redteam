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

- **Spec approved by Sahil (2026-10-23)** — `docs/specs/2026-10-23-v0.1-design.md`. Planning then
  found 4 revisions, logged in its **§16** (engine calls PyRIT targets directly; own `evidence.db`;
  Gandalf judged by a deterministic password check because `GandalfScorer` needs an LLM; per-prompt
  rules in plain Python). Sahil reviews §16 together with the plan.
- **Implementation plan written, awaiting Sahil's approval + execution choice:**
  `docs/superpowers/plans/2026-10-23-chatbot-redteam-v0.1.md` (18 tasks, TDD).
- **No code, no repo init yet** — by design. Task 1 of the plan does `git init` + scaffold.
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

1. Sahil reviews the plan + spec §16 and chooses execution (subagent-driven or native).
2. Build v0.1 task by task with TDD (Task 1 = `git init` + scaffold on branch `feat/v0.1`),
   validating mock → lab bot → Gandalf. Tasks 17–18 need prompt text from Sahil.

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
