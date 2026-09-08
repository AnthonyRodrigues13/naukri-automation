# Project Analysis: naukri-automation

## Summary
A local, single-user Python automation tool for naukri.com: it scrapes job
listings via Playwright, scores fit against a local resume using a
self-hosted Ollama LLM, and (optionally, behind multiple explicit safety
gates) auto-applies and auto-answers screening-question chatbots. No cloud
LLM APIs or paid services are used. Per its own README, Phase 1
(login/search/scrape) and Phase 2 (scoring) are live-verified, the apply
flow is live-tested (one real application has gone out), and Phase 3
(inbox/reply drafting) has not been started. This is a actively-developed,
single-developer WIP project with unusually disciplined process (decision
log, flow doc, safety gates) but zero deployment/CI infrastructure — it is
a personal local tool, not a service.

## Tech Stack
| Category | Technology | Version (if pinned) |
|---|---|---|
| Language(s) | Python | 3.13.3 (venv), unpinned in project |
| Framework | None (plain scripts/CLI via argparse) | N/A |
| Frontend | N/A | N/A |
| Backend | Local CLI (`orchestrator.py`), browser automation via Playwright | unpinned |
| Database | SQLite (`jobs.db`, file-based, single user) | stdlib `sqlite3` |
| ORM / Data layer | None — raw SQL via stdlib `sqlite3` (deliberate, see DECISIONS.md) | N/A |
| Auth | Manual human login (email/OTP) into naukri.com via persistent Chrome profile; no credentials stored/automated | N/A |
| Infra / Hosting | None — runs locally only; local Ollama server for LLM calls (`localhost:11434`) | N/A |
| CI/CD | None found (no `.github/workflows`, no other CI config) | N/A |
| Testing | pytest (179 tests, all passing at analysis time); not declared in `requirements.txt` | pytest 9.1.1 (locally installed) |
| Key third-party deps | `playwright`, `requests`, `openpyxl` (all unpinned in `requirements.txt`) | unpinned |

## Ratings (score /10 with 1-2 line justification each)
| Dimension | Score | Notes |
|---|---|---|
| Future-proof | 7/10 | Stack (Playwright, requests, SQLite, Ollama) is all actively maintained and not deprecated. Tight coupling to naukri.com's current DOM (CSS selectors) is the main fragility — a site redesign would require rework. |
| Maintainability | 8/10 | Clear module boundaries (`orchestrator`/`naukri_client`/`scoring`/`storage`/`excel_log`), typed return contracts (`ApplyResult`), functions decomposed into read/decide/act, and an unusually thorough `DECISIONS.md`/`FLOW.md` pair that documents rationale and call paths. Files are large (`naukri_client.py` ~1000 lines, `scoring.py` ~650) but internally organized. |
| Scalability | 4/10 | Not designed to scale — single SQLite file, single browser profile, single user, sequential Playwright automation, daily application cap of 10. This is intentional for a personal tool, not a defect, but it would need a rearchitecture (multi-tenant DB, queueing, credential management) for any multi-user/10x-100x scenario. |
| Security posture | 7/10 | No hardcoded secrets or API keys found; no cloud credentials in code; login is manual/human (OTP never automated); resume PII (`resume.md`, `resume.pdf`, `JOB_SEARCH_STRATEGY.md`) is explicitly gitignored per a documented decision. Weaknesses: dependencies are unpinned (supply-chain/reproducibility risk), and the highest-risk feature (auto-submitting chatbot answers to a real recruiter) exists, though it is off by default and multiply gated. |
| Test coverage & reliability | 7/10 | 179 tests across all four core modules pass locally (verified via `pytest -q`); decision-logic (chatbot read/decide/act) was explicitly refactored to be unit-testable. No CI enforces this on push/PR — tests only run if a developer remembers to. |
| Documentation | 9/10 | README covers setup, safety notes, and CLI usage in detail; `DECISIONS.md` (~1900 lines, ~40 dated entries) and `FLOW.md` (~630 lines, with a "Currently in flight" convention) are exceptionally thorough for a personal project and clearly actively maintained alongside code changes. |
| Dependency health | 6/10 | Only 3 runtime deps, all mainstream and maintained (Playwright, requests, openpyxl) — low surface area. Docked because none are version-pinned in `requirements.txt`, and the test-only dependency (pytest) isn't declared there at all, so a fresh clone can't reproduce the exact tested environment. |
| **Overall** | **7/10** | Strong engineering discipline (decision log, flow tracing, layered safety gates, decomposed/tested logic) for what is explicitly a personal, local, single-user tool. Main gaps are infra hygiene (no CI, unpinned deps) rather than code quality — appropriate for its scope, but would need work before being anything but a personal tool. |

## Key Risks / Tech Debt
- No CI/CD: 179 passing tests are only enforced by a developer manually running `pytest`; nothing blocks a regression from being committed.
- Dependencies unpinned in `requirements.txt` (`playwright`, `requests`, `openpyxl`), and `pytest` (used by all 4 test files) isn't listed there at all — environment isn't reproducible from a fresh clone.
- Highest-risk capability in the codebase — `AUTO_ANSWER_SCREENING_QUESTIONS`, which submits LLM-drafted answers to a real recruiter with no human review step — is implemented and only gated by config flags/CLI args, not a stronger structural safeguard; README itself calls this out as the highest-risk switch.
- Tight coupling to naukri.com's live DOM structure (CSS selectors, modal detection) means the scraping/apply flow is inherently brittle to site changes; `DECISIONS.md` already documents several live-discovered breakages that had to be patched reactively.
- Single-user, file-based persistence (SQLite file, local browser profile) with no backup/migration strategy — acceptable for personal use, but a real risk of silent data loss (`jobs.db`, `applications_log.xlsx` are both gitignored, so they aren't backed up anywhere in version control).
- Real applications and messages sent on the user's behalf are a fundamentally high-consequence action class; the safety net (dry-run default, `--live` flag, circuit breaker, daily cap, pre-flight confirmation) is well-designed and documented but still entirely software-enforced, with no independent/external check.

## Recommendations
1. Add a minimal CI workflow (e.g. GitHub Actions) that runs `pytest` on every push/PR — the test suite already exists and passes; it just isn't enforced automatically.
2. Pin dependency versions in `requirements.txt` (including `pytest` as a dev/test dependency, ideally split into a `requirements-dev.txt` or `pyproject.toml` with optional groups) for reproducible environments.
3. Consider adding a lightweight smoke/contract test (or periodic manual check) for the naukri.com selectors used in `naukri_client.py`, since DOM drift is the most likely real-world failure mode and has already caused live incidents per `DECISIONS.md`.
4. Since `AUTO_ANSWER_SCREENING_QUESTIONS` submits content to real recruiters with zero human review, consider adding a secondary safeguard beyond config (e.g. a mandatory review queue for the first N auto-answers after any resume change) rather than relying solely on the flag plus cached verification.
5. Formalize backup/export for `jobs.db` and `applications_log.xlsx` (both gitignored and locally-only) so accumulated application history isn't a single-machine point of failure.
