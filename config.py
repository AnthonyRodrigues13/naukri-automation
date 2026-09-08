"""Central configuration. Every safety-relevant knob lives here — nothing else
should hardcode caps, thresholds, or the dry-run/pause switches.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Persistent Chrome profile dir — log in manually once (handle OTP in the
# browser), session persists across runs via launch_persistent_context().
USER_DATA_DIR = str(BASE_DIR / ".browser_profile")

DB_PATH = str(BASE_DIR / "jobs.db")

RESUME_PATH = str(BASE_DIR / "resume.md")

# The actual resume file (PDF) uploaded when Naukri's apply-flow chatbot asks
# for one. Separate from RESUME_PATH (the .md used for scoring/embedding) —
# a chatbot upload step wants a real document, not markdown.
#
# Moved into the project directory 2026-09-05 (was a hardcoded absolute
# Desktop path with a personal filename) ahead of this repo's first git
# commit — see DECISIONS.md. resume.pdf is gitignored, same as resume.md,
# so this path existing doesn't mean the file itself is tracked.
RESUME_PDF_PATH = str(BASE_DIR / "resume.pdf")

# --- Safety-relevant: changes to anything below must be called out explicitly ---

# Max applications submitted per day. Enforced in code (storage.count_applications_today),
# not just as an LLM instruction.
DAILY_APPLICATION_CAP = 10

# Circuit breaker: after this many CONSECUTIVE real applications go through
# with nothing skipped/failed in between, run_apply_cycle() stops and asks
# for a fresh typed confirmation before continuing to the rest of the
# candidates that cycle -- regardless of whether DAILY_APPLICATION_CAP has
# been reached yet. DAILY_APPLICATION_CAP bounds total volume for the day;
# this bounds an unbroken RUN of applies within one cycle, which is the
# shape both real incidents already on record took (see DECISIONS.md) --
# a bug or a misdetected page state applying to job after job unattended,
# not merely "too many applications today." Deliberately well below
# DAILY_APPLICATION_CAP so it can actually trip before the cap silently
# ends the cycle on its own.
CIRCUIT_BREAKER_CONSECUTIVE_APPLIES = 3

# While True, apply_to_job() only logs "would apply" and never clicks.
# Must be flipped to False explicitly and deliberately to go live.
DRY_RUN = True

# Global kill switch. While True, no write actions (applying, sending replies)
# may execute, regardless of DRY_RUN.
PAUSED = False

# Jitter bounds (seconds) between browser actions — no rapid-fire clicking.
MIN_DELAY_SECONDS = 2
MAX_DELAY_SECONDS = 8

# Minimum fit_score (0-100) for a job to be considered for auto-apply.
# recommend_apply from the LLM is a signal, not the sole gate — this threshold
# and the daily cap are the actual gate, enforced in orchestrator.py.
FIT_SCORE_THRESHOLD = 70

# A single LLM-judged fit_score within this many points of
# FIT_SCORE_THRESHOLD is not trusted on one pass: research found LLM-judge
# scores cluster at multiples of 5 near common thresholds (a reproducible
# precision artifact, not real fine-grained discrimination) -- see
# DECISIONS.md and JOB_SEARCH_STRATEGY.md. Jobs landing in this band get
# re-scored FIT_SCORE_REVERIFY_PASSES times total and the AVERAGE decides
# fit_score/recommend_apply -- see scoring.score_job_with_reverification().
# Scores clearly outside the band are trusted on the first pass, so most
# jobs never pay the extra LLM-call cost.
FIT_SCORE_REVERIFY_MARGIN = 10

# Total scoring passes (including the first) for a job that lands in the
# gray zone above. 3 means 2 EXTRA LLM calls beyond the normal one, only
# for borderline jobs.
FIT_SCORE_REVERIFY_PASSES = 3

# While False (default), apply_to_job() stops and flags for manual review
# the moment Naukri's apply-flow chatbot asks a screening question — it
# never fills in or clicks anything in that chatbot. Only when explicitly
# True does it draft answers (via scoring.draft_screening_answer, grounded
# only in resume.md, never fabricating ungrounded facts like salary
# expectations) and submit them automatically, no human review step.
# A second deliberate switch on top of DRY_RUN=False, on purpose — this is
# the riskiest capability in the codebase (real content to a real recruiter,
# zero review) and earns its own explicit opt-in.
AUTO_ANSWER_SCREENING_QUESTIONS = False

# While False (default), an external-apply-only job ("Apply on company
# site") is detected and skipped without being clicked. Only when explicitly
# True does apply_to_job() click that button to capture the destination URL
# (for jobs.db / applications_log.xlsx) before closing the tab it opens.
# Live-verified 2026-09-01: that click marks the job "Applied" in your own
# Naukri account/history — same visible effect as a real Naukri-native
# apply — even though nothing is actually submitted to the external site.
# Off by default because that side effect isn't obviously worth it just to
# capture a link; flip it deliberately once you've weighed that tradeoff.
CAPTURE_EXTERNAL_APPLY_URLS = False

# A newly-scraped job is flagged as a repost/duplicate of an existing job
# (jobs.duplicate_of set, excluded from scoring/apply -- see
# scoring.find_duplicate_job(), storage.get_original_job_embeddings()) when
# its description embedding's cosine similarity to an existing job's is at
# or above this. Calibrated 2026-09-06 against real scraped postings, not
# guessed -- see DECISIONS.md: genuine reposts of the same underlying
# posting measured 0.9920-0.9982; two DIFFERENT jobs that merely share
# Naukri's own auto-generated disclaimer boilerplate measured as high as
# 0.9228, a real false-positive risk. 0.97 sits with margin above that
# boilerplate-collision ceiling and below the genuine-repost floor. Set
# deliberately conservative (biased toward NOT flagging): missing a real
# repost just costs one extra scoring pass, but wrongly flagging a
# genuinely different job as a duplicate would silently remove it from
# scoring/apply consideration entirely -- a worse failure mode, so this
# leans toward the safer direction when in doubt.
DUPLICATE_SIMILARITY_THRESHOLD = 0.97

# --- Ollama models — no runtime code should hardcode a model name elsewhere ---

OLLAMA_MODELS = {
    "score": "qwen3:8b",
    "draft": "qwen3:8b",
    "embed": "nomic-embed-text",
    "classify": "phi4-mini",
}

OLLAMA_BASE_URL = "http://localhost:11434"

# --- Search targeting — `python orchestrator.py search --auto` rotates through
# these instead of keywords/location being typed manually every time ---

# Each entry is one role/city combo to search. `search --auto` picks
# whichever entry's (keywords, location) pair was searched least recently
# (or never) — see orchestrator._pick_least_recently_run_query() and
# storage.get_search_run_times(). Added 2026-09-06, JOB_SEARCH_STRATEGY.md
# roadmap item 4. Edit this list to match your actual target roles/cities —
# these are a starting point, not a fixed set.
SEARCH_QUERIES = [
    {"keywords": "AI Engineer", "location": "Bangalore"},
    {"keywords": "AI Engineer", "location": "Pune"},
    {"keywords": "AI Engineer", "location": "Hyderabad"},
    {"keywords": "AI Engineer", "location": "Goa"},
    {"keywords": "WordPress Developer", "location": "Bangalore"},
    {"keywords": "WordPress Developer", "location": "Pune"},
    {"keywords": "WordPress Developer", "location": "Hyderabad"},
    {"keywords": "WordPress Developer", "location": "Goa"},
]
