# PyRIT 1.1.0 — detailed analysis

Date: 2026-10-23. Grounded in the installed `pyrit==1.1.0` source, not from memory. Informs the
decision to use PyRIT as a **library for specific pieces** behind one adapter, not as our engine,
CLI or GUI.

## How PyRIT is shipped: three entry points

| Command | What it is |
|---|---|
| `pyrit_backend` | FastAPI server that **is** the GUI (CoPyRIT) + the REST API |
| `pyrit_scan` | CLI that is a thin REST client to `pyrit_backend` over HTTP |
| `pyrit_shell` | Interactive shell |

In 1.1.0 the backend server is the hub; the CLI drives the same API the GUI uses. The library
(`import pyrit`) sits underneath — that is how our spike used it.

## The GUI (CoPyRIT)

- FastAPI backend serving a **pre-built React SPA** (Microsoft Fluent UI), ~4.6 MB, bundled in the
  package (Vite/Rolldown). Runs out of the box, no `npm build`.
- Routes/screens: targets, converters, attacks, scenarios, datasets, configuration, initializers,
  labels, media, health, version.
- Auth: **Microsoft Entra ID (Azure AD), PKCE — optional.** The middleware disables itself when
  `ENTRA_TENANT_ID`/`ENTRA_CLIENT_ID` are unset, and `PYRIT_ALLOW_UNAUTHENTICATED_ADMIN=true`
  allows local use. So it runs fully locally with **no Azure required**.
- Security headers (CSP/HSTS), request IDs, RFC 7807 errors, configurable CORS (default
  localhost:3000/5173). Swagger/ReDoc only in DEV_MODE. Defaults to localhost (README's `0.0.0.0`
  example would expose it on the LAN — don't copy it).

**Assessment:** polished and real, but built around PyRIT's own concepts (scenarios, techniques,
its target catalog). It is **not** a report/review UI: no per-OWASP pass rates, no human-review
queue, no Pass/Fail/Inconclusive. Using it means adopting PyRIT's whole model and still building
reporting on top. It also reads PyRIT's central memory, so it cannot display runs from our own
engine and evidence store. For our needs a thin web UI over our own engine is the better fit
(decided 2026-10-23: FastAPI + HTMX + Jinja2 in v0.2; maybe FastAPI + React later). The GUI is
worth showing in a demo as "the upstream tool".

## Component inventory (counts from source)

| Layer | Count | Local on our setup (no GPU, Ollama 3B only)? |
|---|---|---|
| Targets | 12 | HTTP, HTTPXApi, OpenAI-compatible, HuggingFace, Playwright, WebSocket, **Gandalf**, Azure ML, A2A, text. Azure ones need Azure. |
| Converters | 93 | Encoding/obfuscation (base64, rot13, leetspeak, look-alike Unicode, zero-width, Morse…) local. Multimodal + LLM-based need models. |
| Scorers — deterministic | ~41 (incl. 18 regex) | ✅ substring, regex, credential-leak, XSS, SQLi, SSRF, path-traversal, markdown-injection… |
| Scorers — LLM/classifier | ~14 float-scale + self-ask | ⚠️ need a judge LLM, a guard model (LlamaGuard/ShieldGemma via Ollama), or Azure Content Safety |
| Attacks — single-turn | 4 | ✅ `PromptSendingAttack` (spike-tested) |
| Attacks — multi-turn | 8 | ⚠️ Crescendo/TAP/PAIR/RedTeaming need an attacker LLM; `MultiPromptSendingAttack` (scripted) does not |
| Datasets | 60 remote loaders + local prompt files + 94 jailbreak templates | ✅ as an offline importer; many are harmful-content sets |
| Scenarios (ready-made suites) | 19 | mostly LLM-dependent — see below |
| Analytics | 4 | ✅ outcome/paired/technique stats |
| Memory | SQLite / InMemory / Azure SQL | ✅ SQLite (spike-tested) |

## Architecture

```
  pyrit_backend (GUI + REST API)  ·  pyrit_scan (CLI)
  Scenarios  = named suites: (attack technique × dataset) + scoring, run as a batch
  Attacks/Executors  (single & multi-turn; parallel via AttackExecutor)
  Targets ─► Converters ─► [bot] ─► Scorers ─► Memory (SQLite)
```

Clean plugin model: targets, converters, scorers and memory are independent; attacks orchestrate
them; scenarios bundle them into named runnable suites; backend/CLI/GUI sit on top.

## Scenarios — closest to our goal

A scenario is PyRIT's "predefined suite run against a target". The 19 that ship: **airt/** (cyber,
jailbreak, leakage, multilingual, psychosocial, rapid_response, scam), **garak/** (encoding,
figstep, doctor, web_injection, system_prompt_extraction, package_hallucination, audio),
**foundry/** red_team_agent, **benchmark/** adversarial, **adaptive/**. `pyrit_scan --scenario …`
runs one end to end. But most lean on attacker/judge LLMs and LLM scorers, so on our hardware they
are weak and their verdicts are not the deterministic rules we want.

## Strengths

Large, maintained (Microsoft) library; deterministic scorers + encoding converters reusable today
and locally; native Gandalf; real parallelism, SQLite evidence, a working local GUI; MIT, Py
3.11–3.14.

## Weaknesses / costs for us

Heavy (~55 deps + 4.6 MB frontend → own environment); best features need a capable model we can't
run well; fast-moving API (pin `==1.1.0`); spike pitfalls (no JSON escaping, no status check,
empty/error → "safe"); no report/review/Inconclusive in our shape; GUI built around PyRIT's model.

## Decision for chatbot-redteam

Use PyRIT as a **library** for the deterministic, local-friendly pieces (HTTP + Gandalf targets,
encoding converters, rule-based scorers, SQLite memory), behind one `adapters/` module; build our
own thin engine, judging, human review and OWASP report on top. Skip CoPyRIT as our product UI.
