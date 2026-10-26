# HANDOFF — chatbot-redteam (resume here)

Last updated: 2026-10-26. Written so a fresh session in `C:\dev\chatbot-redteam` can continue
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
- **v0.1 built, on branch `feat/v0.1`** (`main` holds only the planning docs). **Tasks 1–16
  complete and reviewed** (scaffold, config, prompt packs + patterns, planner, extractor, engine,
  HTTP adapter + mock + N1 benchmark, judge, PyRIT detectors + Gandalf, evidence store, reports,
  run orchestration, finalize, compare, CLI + shipped endpoints and smoke pack, and the mock
  validation in `docs/validation/v0.1-mock.md`). **The final whole-branch review is done and its
  fix wave applied** (2026-10-26: `${VAR}` secrets kept out of error text, evidence and reports;
  in-flight attempts labelled "no outcome recorded"; hardening; these docs). 655 tests passing
  (Executed 2026-10-26). N1 benchmark Executed: 100 prompts @50 concurrency with a 1 s mock →
  ~2.5–2.7 s, peak 50 (spike: PyRIT AttackExecutor 17.5 s, plain httpx 3.1 s).
- **Tasks 17–18 (lab-bot and Gandalf packs + validation) wait for Sahil's prompt text** — Claude
  never writes attack prompts; it transcribes Sahil's text verbatim.
- **Pushed 2026-10-26** to https://github.com/SahilOtavanekar/chatbot-redteam (`main` and
  `feat/v0.1`); CI (ubuntu + windows: ruff, format, mypy, pytest) green.
- **Open items for Sahil:** (1) prompt text for Tasks 17 (lab bot) and 18 (Gandalf); (2) Gandalf's
  API (`gandalf-api.lakera.ai/api/send-message`) returned **503** on 2026-10-25 (Executed probe) —
  re-check before Task 18; (3) before org-bot runs: should a guardrail's HTTP 400 "blocked" reply
  (e.g. Azure content filter) count as a refusal instead of Inconclusive?; (4) commit trailers are
  mixed (Opus/Sonnet) — tidy at merge if wanted.
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
6. **Validation targets:** mock server, the lab bot, and **Gandalf** (sent via PyRIT's
   `GandalfTarget`; judged by our own deterministic password check — candidate words from the
   reply, each confirmed by Gandalf's guess-password API — **not** `GandalfScorer`, which needs an
   LLM; spec §16.3). Org bots are designed-for but not demoed in v0.1.
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
2. Tasks 17–18, once Sahil supplies the prompt text: transcribe it verbatim into the lab-bot and
   Gandalf packs, then run and document those validations (re-check the Gandalf API first).
3. Then superpowers:finishing-a-development-branch: open a PR from `feat/v0.1` to `main`; merge
   after CI is green.

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
