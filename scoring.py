"""Embedding similarity pre-filter + qwen3:8b fit scoring, both via a local
Ollama instance (no cloud APIs, no API keys).
"""

import json
import logging
import math
import re

import requests

import config

log = logging.getLogger("scoring")

# Jobs whose embedding similarity to the resume falls below this never reach
# the LLM call — a cheap filter for obviously-unrelated postings. Not a
# safety gate (that's config.FIT_SCORE_THRESHOLD), just an efficiency one.
#
# nomic-embed-text runs a high baseline cosine similarity even between
# unrelated professional text (measured ~0.49 for a chef resume vs. a Python
# developer posting, ~0.88 for a genuine match) — a naive low floor like 0.35
# would never trigger. 0.45 is calibrated to that measurement, not a guess.
EMBED_SIMILARITY_FLOOR = 0.45

SCORE_TEMPERATURE = 0.15

# Lower than SCORE_TEMPERATURE on purpose: a screening answer gets submitted
# to a real recruiter with no human review (when AUTO_ANSWER_SCREENING_
# QUESTIONS is on), so consistency matters more here than for fit scoring.
# Live-tested: at 0.15, an otherwise-correct grounded answer (relocation to
# a city explicitly listed in the resume) flipped to an incorrect SKIP on
# roughly 1 run in 6 — not frequent, but a wrong SKIP on a real application
# is exactly the failure mode this whole module exists to avoid.
SCREENING_ANSWER_TEMPERATURE = 0.05

SCORE_PROMPT_TEMPLATE = """You are an experienced technical recruiter judging whether this candidate is a genuinely good fit for this specific job — the way a thoughtful human reviewer would, not by counting keyword overlaps between the resume and the posting.

Resume:
{resume_profile}

Job description:
{job_description}

Judge it the way a human would:
- Skills overlap alone is not fit. Explicitly compare the job's stated experience-LEVEL requirement (years of experience, seniority title like "Junior"/"Senior"/"Lead") against the resume's actual experience level. A significant mismatch in either direction — the candidate has 2 years but the job wants 7-12, or the candidate is clearly senior applying to a fresher-only role — is a real fit problem, not something to wave away because some skills match. Weigh it accordingly in the score.
- Required/mandatory skills matter far more than nice-to-have ones. If the posting marks something as required or mandatory and the resume doesn't support it, that's a real gap, not a minor deduction — don't let strong nice-to-have overlap paper over a missing mandatory skill.
- Shared buzzwords are not the same as demonstrated substance — the resume mentioning "AI" doesn't mean it demonstrates what this specific posting asks for; check what was actually built, not just which words appear in both documents.
- A candidate can be a strong skills match but a poor level match, or vice versa. Let fit_score reflect whichever is the bigger real-world blocker to actually getting hired for this role, not an average that hides it.

Score guide (a rough anchor, not a rigid formula): 85-100 = strong match on both skills and experience level, no real gaps. 60-84 = good skills match but one real gap (e.g. experience level, or one required skill). 30-59 = partial overlap only, or a major level mismatch. 0-29 = fundamentally different role, or multiple major mismatches.

Respond with ONLY a JSON object in exactly this shape, no other text — "reason" should read like an actual human's judgment, naming both the strongest point of fit AND the biggest real concern if there is one, not just a one-sided positive summary:
{{"fit_score": <integer 0-100>, "reason": "<one sentence>", "recommend_apply": <true or false>}}
"""

_FALLBACK_RESULT = {
    "fit_score": 0,
    "reason": "invalid LLM output, needs manual review",
    "recommend_apply": False,
}


def load_resume_profile(path: str = config.RESUME_PATH) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _embed(text: str) -> list[float]:
    resp = requests.post(
        f"{config.OLLAMA_BASE_URL}/api/embeddings",
        json={"model": config.OLLAMA_MODELS["embed"], "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def embed_resume(resume_profile: str) -> list[float] | None:
    """Computes the resume's embedding once, for reuse across every job in
    one scoring cycle via score_job(..., resume_embedding=...) — added
    2026-09-06. The resume text doesn't change within a cycle (loaded once
    in run_scoring_cycle()), so re-embedding it per job, identically, N
    times for N jobs was pure waste. Returns None (doesn't raise) on
    failure so run_scoring_cycle() can skip the whole cycle cleanly rather
    than crash — matches this module's fallback-on-failure convention
    elsewhere (see score_job)."""
    try:
        return _embed(resume_profile)
    except (requests.RequestException, KeyError, TypeError) as e:
        log.error("Failed to compute resume embedding - cannot score this cycle: %s", e)
        return None


def embed_job_description(description: str) -> list[float] | None:
    """Computes a job description's embedding for repost/duplicate detection
    (see find_duplicate_job(), storage.get_original_job_embeddings()) --
    added 2026-09-06, JOB_SEARCH_STRATEGY.md roadmap item 7. Returns None
    (doesn't raise) on failure, mirroring embed_resume(): a transient
    embedding failure should skip duplicate detection for this one job,
    not crash the whole search cycle."""
    try:
        return _embed(description)
    except (requests.RequestException, KeyError, TypeError) as e:
        log.error("Failed to compute job description embedding - skipping duplicate check for this job: %s", e)
        return None


def find_duplicate_job(new_embedding: list[float], candidates: list[dict]) -> str | None:
    """Pure function, no I/O. `candidates` is [{"job_id", "embedding"}, ...]
    for previously-seen ORIGINAL jobs only (never a job that's itself
    already flagged as a repost of something else -- see
    storage.get_original_job_embeddings() -- so a chain of reposts always
    resolves back to one true original instead of drifting). Returns the
    job_id of the single most-similar candidate if its cosine similarity to
    new_embedding is >= config.DUPLICATE_SIMILARITY_THRESHOLD, else None.

    Threshold calibrated 2026-09-06 against real scraped postings (see
    DECISIONS.md and config.DUPLICATE_SIMILARITY_THRESHOLD): genuine
    reposts of the same underlying job measured 0.9920-0.9982 cosine
    similarity; two DIFFERENT jobs that merely share Naukri's own
    auto-generated disclaimer boilerplate measured as high as 0.9228 -- a
    real false-positive risk this threshold must sit above."""
    best_job_id = None
    best_similarity = -1.0
    for candidate in candidates:
        similarity = _cosine_similarity(new_embedding, candidate["embedding"])
        if similarity > best_similarity:
            best_similarity = similarity
            best_job_id = candidate["job_id"]
    if best_job_id is not None and best_similarity >= config.DUPLICATE_SIMILARITY_THRESHOLD:
        return best_job_id
    return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _call_scoring_llm(job_description: str, resume_profile: str) -> str:
    prompt = SCORE_PROMPT_TEMPLATE.format(
        resume_profile=resume_profile, job_description=job_description
    )
    resp = requests.post(
        f"{config.OLLAMA_BASE_URL}/api/generate",
        json={
            "model": config.OLLAMA_MODELS["score"],
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": SCORE_TEMPERATURE},
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["response"]


def _parse_score_response(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not {"fit_score", "reason", "recommend_apply"} <= data.keys():
        return None
    try:
        fit_score = max(0, min(100, int(data["fit_score"])))
    except (TypeError, ValueError):
        return None
    return {
        "fit_score": fit_score,
        "reason": str(data["reason"]),
        "recommend_apply": bool(data["recommend_apply"]),
    }


SCREENING_ANSWER_PROMPT_TEMPLATE = """You are answering a job application screening question on behalf of a candidate, using ONLY the resume below as ground truth. Never invent facts — numbers, dates, years of experience, availability, salary, or preferences — that aren't clearly supported by the resume.

This applies just as much to yes/no or multiple-choice questions as to open ones. A binary choice is not a coin flip you're allowed to guess: if the resume is silent on the topic, SKIP is the only correct answer, even though it isn't one of the listed options. Do not default to "No" (or any other option) just because it seems like a safe or common answer — an incorrect guess submitted on the candidate's behalf is worse than leaving the question unanswered.

Resume:
{resume_profile}
{job_context_block}
Screening question: {question}
{options_block}

Step 1 — check the resume FIRST, specifically, for this exact topic. Topics
like relocation, salary/CTC, notice period, travel/shift willingness, visa
status, and background-check consent are usually NOT covered by a resume —
but this specific one might explicitly state a position on this specific
topic (e.g. an "Availability & logistics" section listing named cities, a
CTC figure or range, a notice period). If it does, that stated position is
your answer — the "usually not covered" pattern does NOT override a resume
that actually covers it. Only fall through to SKIP when, after actually
checking, the resume is silent on this exact topic.

Rules:
- If options are listed, Naukri's widget only allows picking ONE of them, even if the question is phrased as "which of the following..." and several genuinely apply. Answer with EXACTLY one of the listed options, verbatim, nothing else — UNLESS the resume doesn't clearly support any of them, in which case answer SKIP instead of picking the closest-sounding option. When several options are all true but only one can be chosen, pick whichever single one is most central/prominent in the resume (e.g. mentioned first, or most repeated) — never invent a combined answer, never list more than one.
- The resume stating a fact about this topic does NOT mean one of the listed options is automatically your answer — the fact still has to accurately match one specific option. If the resume gives a fact that doesn't cleanly correspond to any listed option (e.g. the options are all "how soon can you join" categories — Immediate / 15 Days / 1 Month / Serving Notice Period — but the resume states a specific notice-period LENGTH, like "3 months," that isn't offered as a choice and doesn't mean the candidate is currently serving notice), the correct answer is SKIP. Do not pick the option whose wording merely overlaps with resume phrasing (e.g. the resume saying you "could serve a shorter notice" does NOT make "Serving Notice Period" correct — that option means "I am currently in my notice period at my current job right now," a different, specific claim the resume doesn't make). When genuinely unsure whether an option accurately matches, SKIP rather than pick the closest-sounding one.
- If no options are listed, answer the way the candidate would type it themselves into this chat — concise and natural, first-person where that reads naturally (e.g. "2+ years" or "Yes, I've built production RAG pipelines with LangChain" rather than "The candidate has 2 years of experience"). This is about phrasing only — it does not relax any rule above: still grounded only in the resume, still SKIP when the resume doesn't support an answer.
- If the resume states a RANGE for a numeric fact (e.g. "expected CTC: 10-20 LPA depending on role seniority") and a job description is given above, your answer must be ONE specific number picked from within that range — repeating the original range back is always wrong once a job description is given, for a junior job exactly as much as for a senior one. Worked examples for that exact "10-20 LPA depending on seniority" range:
  - Job description says "Junior Developer, 0-1 years, fresher welcome" -> answer "10 LPA" (near the low end) — NOT "10-20 LPA".
  - Job description says "Senior Staff Engineer, 8+ years, leading architecture" -> answer "18 LPA" (near the high end) — NOT "10-20 LPA".
- If you cannot point to the specific part of the resume that supports your answer, the answer is SKIP.

Respond with ONLY the answer text (or SKIP). No explanation, no punctuation around it.
"""

_SKIP_SENTINEL = "SKIP"


_ANSWER_LABEL_RE = re.compile(r"^\**\s*answer\s*\**\s*:?\s*", re.IGNORECASE)


def _clean_free_text_answer(raw: str, question: str) -> str:
    """Free-text answers sometimes echo the question back before the real
    answer, or prefix it with a markdown label like "**Answer:**", despite
    the existing "Respond with ONLY the answer text" instruction. Tried
    strengthening that instruction further (explicitly forbidding echoing
    the question and markdown formatting) — it fixed the label problem but
    NOT the echo, AND caused a real regression elsewhere: a previously-
    reliable options-based answer ("willing to relocate to Bangalore?" ->
    "Yes", 8/8 stable before) dropped to 0/8 with that stronger instruction
    in place, restored to 5/5 by reverting it (live-tested and isolated
    2026-09-02). Reverted; both problems are instead cleaned up here in
    code, which doesn't carry that side-effect risk since it never touches
    what's sent to the model for other question types."""
    stripped = raw.strip()
    q = question.strip()
    if stripped.lower().startswith(q.lower()):
        stripped = stripped[len(q):].lstrip(" \n\t.:-")
    stripped = _ANSWER_LABEL_RE.sub("", stripped.strip())
    stripped = stripped.replace("**", "")
    return stripped.strip()

# Expected-CTC questions require picking a specific number calibrated to a
# job's seniority from a range in the resume — tested extensively and found
# genuinely unreliable for this model in a single pass (correctly handles
# clear junior/senior cases, but flips on mid-level phrasing, and fixing one
# case broke another already-passing one — not converging, not a prompt bug
# to keep patching). Per explicit instruction: always skip these for manual
# review rather than risk a wrong number reaching a real recruiter
# unsupervised. This check runs BEFORE any LLM call — deterministic, not a
# judgment the model gets a chance to guess at.
#
# Widened 2026-09-05 after live phrasing checks found real false negatives —
# "expected take-home pay", "expected monthly pay", "annual package", and
# "budget expectation for this role" all failed to match the original list.
# Casting a wider net risks over-matching a question that isn't actually
# about salary (e.g. a stray mention of "package" in an unrelated sense) —
# accepted deliberately, since the only effect of a false-positive match here
# is routing to SKIP/manual-review, the same safe direction this whole gate
# exists to enforce. See DECISIONS.md.
_SALARY_KEYWORDS = (
    "ctc",
    "salary",
    "compensation",
    "remuneration",
    "pay",
    "package",
    "budget",
    "take-home",
    "take home",
)


def _mentions_salary(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in _SALARY_KEYWORDS)


def _call_screening_llm(
    question: str, options: list[str] | None, resume_profile: str, job_description: str = ""
) -> str:
    options_block = ""
    if options:
        options_block = "Options: " + " | ".join(options)
    job_context_block = ""
    if job_description:
        job_context_block = f"\nJob description (for calibrating range-based answers only):\n{job_description}\n"
    prompt = SCREENING_ANSWER_PROMPT_TEMPLATE.format(
        resume_profile=resume_profile,
        question=question,
        options_block=options_block,
        job_context_block=job_context_block,
    )
    resp = requests.post(
        f"{config.OLLAMA_BASE_URL}/api/generate",
        json={
            "model": config.OLLAMA_MODELS["draft"],
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {"temperature": SCREENING_ANSWER_TEMPERATURE},
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()


# Live-verified 2026-09-01: for a fixed-option question, the drafting call
# above sometimes picks an option that merely *sounds* plausible over one
# that's actually, specifically supported — reproduced 3/4 times with
# varied notice-period option lists (picked "Serving Notice Period" for a
# resume that says "3 months, negotiable," not "currently serving notice").
# More prompt instructions telling it not to do this made no measurable
# difference (word-for-word identical wrong answers before/after) — the
# same kind of forced-match failure as expected-CTC calibration, and the
# same lesson applies: don't keep patching the single-pass prompt, add a
# second, narrower, independently-scrutinizing pass instead. Only runs for
# fixed-option (chip/radio) answers, since that's the only case with a
# reproduced failure — free-text answers aren't re-verified.
VERIFY_ANSWER_PROMPT_TEMPLATE = """A candidate's resume was used to pick one answer to a job screening question from a fixed list of options. Verify this was accurate — be skeptical, not agreeable.

Resume:
{resume_profile}

Screening question: {question}
Chosen answer: {answer}

Does the resume specifically and accurately support this exact answer — not something loosely related, not an option that merely sounds plausible, but this precise claim? A resume mentioning a related word or concept is not enough if the specific claim in the chosen answer isn't actually what the resume states.

Respond with exactly YES or NO. Nothing else."""


def _verify_screening_answer(
    question: str,
    answer: str,
    resume_profile: str,
    cache: dict | None = None,
    durable_lookup=None,
    durable_save=None,
) -> bool:
    """`cache`, if given, is checked/populated for the exact (question,
    answer) pair before making a real LLM call. Naukri visibly reuses
    standard screening questions verbatim across postings (see
    DECISIONS.md's Yellowblock/Coffeebeans examples) — a cache scoped to
    one apply cycle (the caller creates it fresh per cycle, never persisted
    across process runs) skips only literal repeats of a question this
    cycle has already verified, never a new judgment. resume_profile is
    deliberately NOT part of the cache key: it's fixed for the whole cycle
    by the time a cache is passed in (loaded once in run_apply_cycle()), so
    a (question, answer) pair means the same verification for the cache's
    entire lifetime. This does not reopen the doubled-latency-per-call
    tradeoff recorded below — every distinct (question, answer) pair still
    gets verified at full rigor; only literal repeats are skipped.

    `durable_lookup`/`durable_save`, if given, extend this beyond one
    process's in-memory `cache` into a store that survives across apply
    cycles — added 2026-09-06, JOB_SEARCH_STRATEGY.md roadmap item 3, since
    the in-memory cache above only ever helps within a single cycle, and
    Naukri visibly repeats the same standard questions across separate
    cycles/days too. Both are plain callables, not a direct storage.py
    import: naukri_client.py/scoring.py/storage.py never import each other
    (see README.md's module boundary rule) — orchestrator.py wires
    storage.get_screening_answer_verification/save_screening_answer_verification
    in as these two hooks, closed over a hash of resume.md's current
    content computed once per apply cycle (so an edited resume never
    silently reuses a verdict it was never actually checked against — see
    storage's docstrings). `durable_lookup(question, answer) -> bool | None`
    is checked after the in-memory cache (a miss there but a hit here still
    skips the real LLM call, and back-fills the in-memory cache too).
    `durable_save(question, answer, verdict)` is called alongside
    populating the in-memory cache — never on a transient failure, same
    reasoning as the in-memory cache below."""
    cache_key = (question, answer)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    if durable_lookup is not None:
        durable_verdict = durable_lookup(question, answer)
        if durable_verdict is not None:
            if cache is not None:
                cache[cache_key] = durable_verdict
            return durable_verdict

    prompt = VERIFY_ANSWER_PROMPT_TEMPLATE.format(
        resume_profile=resume_profile, question=question, answer=answer
    )
    try:
        resp = requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={
                "model": config.OLLAMA_MODELS["draft"],
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": SCREENING_ANSWER_TEMPERATURE},
            },
            timeout=120,
        )
        resp.raise_for_status()
        verdict_text = resp.json()["response"].strip().upper()
    except (requests.RequestException, KeyError, TypeError) as e:
        # KeyError/TypeError: a 200 OK with an unexpected body (e.g.
        # {"error": "..."} instead of {"response": "..."}) shouldn't crash
        # this function — rejecting the answer is already this function's
        # safe default for any failure, so no new behavior needed here,
        # just a wider net.
        log.error("Screening-answer verification call failed: %s — rejecting answer to be safe.", e)
        return False  # deliberately not cached — a transient failure shouldn't poison a repeat of this question

    if verdict_text.startswith("YES"):
        verdict = True
    else:
        if not verdict_text.startswith("NO"):
            log.warning("Verification returned neither YES nor NO (%r) — rejecting answer to be safe.", verdict_text)
        verdict = False

    if cache is not None:
        cache[cache_key] = verdict
    if durable_save is not None:
        durable_save(question, answer, verdict)
    return verdict


def draft_screening_answer(
    question: str,
    options: list[str] | None,
    resume_profile: str,
    job_description: str = "",
    cache: dict | None = None,
    durable_lookup=None,
    durable_save=None,
) -> str | None:
    """Drafts an answer to a job-application screening question, grounded
    only in resume_profile. Returns None (never a guess) if the resume
    doesn't clearly support a confident answer — callers must treat None as
    "skip this question, needs manual review", never fill in a placeholder.

    `job_description`, if given, is used ONLY to calibrate a value the
    resume states as a range (e.g. "expected CTC: 10-20 LPA depending on
    seniority") to this specific job — it is never a substitute for the
    resume actually supporting an answer at all.

    If `options` is given (a chip/quick-reply question), the return value is
    guaranteed to be one of the exact strings in `options` (matched
    case-insensitively, returned in the original casing) — never a
    close-but-not-exact string a click handler can't locate, and never more
    than one option even for a "which of these apply" style question:
    live-verified 2026-09-01 that Naukri's widget for that exact question
    (rendered once as chips, once as radio buttons across two separate
    page loads of the same question) carries `id="singleselect_radiobutton_
    ..."` — it's single-select by the platform's own design regardless of
    phrasing or which widget renders it, so the prompt instructs picking
    one best answer rather than combining several.

    Always returns None for anything mentioning salary/CTC/compensation —
    see _mentions_salary — regardless of what resume_profile says, because
    calibrating a specific number is unreliable for this model; current-CTC
    (a flat fact, no calibration needed) is the one exception this skips
    unnecessarily, an accepted false-positive given the alternative.

    For a fixed-option (chip/radio) question, a matched answer goes through
    a second, independent verification call (_verify_screening_answer)
    before being returned — see the comment above that function for why:
    the drafting call alone was found to sometimes pick a plausible-
    sounding wrong option over an accurate one. Free-text answers are not
    re-verified (no reproduced failure there yet). This roughly doubles
    latency for fixed-option questions — an accepted tradeoff, not a
    default anyone should assume without checking config/instructions.

    `cache`, if given, is passed straight through to _verify_screening_answer
    (see its docstring) — a dict the caller creates fresh once per apply
    cycle (not this function's job to manage), so repeat questions across
    jobs in the same cycle skip a redundant verification call.

    `durable_lookup`/`durable_save`, if given, are also passed straight
    through to _verify_screening_answer (see its docstring) — added
    2026-09-06, roadmap item 3, so a repeat question skips a redundant
    verification call across DIFFERENT apply cycles too, not just within
    one.
    """
    if _mentions_salary(question):
        log.info("Screening question mentions salary/CTC — always skipping auto-answer: %r", question)
        return None

    try:
        raw = _call_screening_llm(question, options, resume_profile, job_description)
    except (requests.RequestException, KeyError, TypeError) as e:
        # KeyError/TypeError: a 200 OK with an unexpected body (missing
        # "response") shouldn't crash the apply cycle -- None already means
        # "can't confidently answer, needs manual review", the correct
        # outcome here regardless of which of these three raised.
        log.error("Screening-answer LLM call failed: %s", e)
        return None

    if raw.strip().upper() == _SKIP_SENTINEL:
        return None

    if options:
        for opt in options:
            if raw.strip().lower() == opt.strip().lower():
                if _verify_screening_answer(
                    question, opt, resume_profile, cache=cache, durable_lookup=durable_lookup, durable_save=durable_save
                ):
                    return opt
                log.warning(
                    "Screening answer %r for %r failed independent verification — skipping.", opt, question
                )
                return None
        log.warning("Screening answer %r didn't match any option in %r — skipping.", raw, options)
        return None

    return _clean_free_text_answer(raw, question)


def score_job(job_description: str, resume_profile: str, resume_embedding: list[float] | None = None) -> dict:
    """Must return {"fit_score": 0-100 or None, "reason": str, "recommend_apply": bool}.

    `resume_embedding`, if given, is used instead of re-computing
    _embed(resume_profile) — see embed_resume(), which run_scoring_cycle()
    calls once per cycle since resume_profile doesn't change across jobs
    within one cycle. Falls back to computing it here when not given, so
    this function stays usable standalone (tests, any future direct
    caller) without requiring a caller to precompute anything.

    recommend_apply from the LLM is a signal, not the sole gate — callers
    (orchestrator.run_apply_cycle) must still enforce config.FIT_SCORE_THRESHOLD
    and the daily application cap in code.

    fit_score is None specifically (and only) for the two transport-failure
    branches below (embedding call / scoring LLM call unreachable, e.g. a
    timeout or Ollama down) — NOT a real 0. storage.get_unscored_jobs()
    queries `WHERE fit_score IS NULL`, so returning None here leaves the job
    queryable and retried on the next `score` cycle instead of permanently
    stuck at a fake 0 that reads as "scored, doesn't match." A live scoring
    run (2026-09-05) hit exactly this: one job's LLM call read-timed-out and
    the old code (fit_score=0 via _FALLBACK_RESULT) would have silently
    dropped a real candidate for good. See DECISIONS.md. Every OTHER failure
    path (empty description, two rounds of unparseable JSON from the model)
    still returns a real 0 via _FALLBACK_RESULT — those aren't transport
    failures, so there's no reason to expect a retry would do better.

    Also catches KeyError/TypeError, not just requests.RequestException —
    a 200 OK response with an unexpected body (e.g. Ollama returning
    {"error": "model not found"} instead of {"embedding": [...]} because a
    configured model isn't pulled) doesn't trip raise_for_status() at all,
    so it would otherwise reach the ["embedding"]/["response"] lookup and
    crash with a KeyError, uncaught anywhere above this function. Treated
    the same as a transport failure (fit_score=None, retryable) since a
    misconfiguration like a missing model is exactly the kind of thing a
    later retry (after the user fixes it) should succeed at, unlike a
    genuinely-scored answer the model just failed to format as JSON.
    """
    if not job_description.strip():
        return {**_FALLBACK_RESULT, "reason": "empty job description, needs manual review"}

    try:
        resume_vec = resume_embedding if resume_embedding is not None else _embed(resume_profile)
        similarity = _cosine_similarity(_embed(job_description), resume_vec)
    except (requests.RequestException, KeyError, TypeError) as e:
        log.error("Embedding call failed: %s", e)
        return {"fit_score": None, "reason": f"embedding call failed: {e}", "recommend_apply": False}

    if similarity < EMBED_SIMILARITY_FLOOR:
        return {
            "fit_score": 0,
            "reason": f"embedding similarity {similarity:.2f} below floor, skipped LLM scoring",
            "recommend_apply": False,
        }

    for attempt in range(2):
        try:
            raw = _call_scoring_llm(job_description, resume_profile)
        except (requests.RequestException, KeyError, TypeError) as e:
            log.error("Scoring LLM call failed: %s", e)
            return {"fit_score": None, "reason": f"LLM call failed: {e}", "recommend_apply": False}

        parsed = _parse_score_response(raw)
        if parsed:
            return parsed
        log.warning("Attempt %d: invalid JSON from scoring model: %.200r", attempt + 1, raw)

    return _FALLBACK_RESULT


def score_job_with_reverification(
    job_description: str, resume_profile: str, resume_embedding: list[float] | None = None
) -> dict:
    """Wraps score_job() with re-verification for borderline scores —
    added 2026-09-06 (see DECISIONS.md and JOB_SEARCH_STRATEGY.md).

    Research found single-pass LLM-judge fit scores cluster at multiples
    of 5 near common thresholds — a reproducible precision artifact, not
    genuine fine-grained discrimination between, say, 65 and 75. When the
    FIRST pass's fit_score lands within config.FIT_SCORE_REVERIFY_MARGIN
    of config.FIT_SCORE_THRESHOLD, this re-scores the SAME job (same
    inputs) config.FIT_SCORE_REVERIFY_PASSES - 1 more times and returns
    the AVERAGE (rounded to the nearest int) as the final fit_score.
    recommend_apply is recomputed from that average against
    FIT_SCORE_THRESHOLD directly — not averaged or majority-voted on its
    own — so the pass/fail decision stays traceable to one number instead
    of two independently-wobbling ones. A score clearly outside the gray
    zone is trusted on the first pass and returned as-is: most jobs never
    pay the extra LLM-call cost, only genuinely borderline ones do.

    fit_score can still be None here: a failure on the FIRST pass is
    returned immediately, unchanged — nothing meaningful to re-verify yet
    (see score_job's own docstring for what None means). If the first pass
    succeeds and lands in the gray zone but SOME re-verify passes fail
    transiently, only the successful ones are averaged — a transient
    failure during re-verification degrades precision, it doesn't
    invalidate the attempt. If EVERY re-verify pass fails, the first
    pass's own real score is returned unchanged (nothing to average
    against), logged so a run of transient failures during re-verification
    is visible rather than silently swallowed.

    Deliberately re-runs the embedding + cosine-similarity check on every
    pass (via score_job()) rather than reusing the first pass's
    similarity value — those calls are fast/cheap relative to the LLM
    scoring call, and reusing state across calls here would couple this
    function to score_job()'s internals more than the simplicity is worth
    for a feature that only affects a minority of borderline jobs."""
    first = score_job(job_description, resume_profile, resume_embedding=resume_embedding)
    if first["fit_score"] is None:
        return first

    if abs(first["fit_score"] - config.FIT_SCORE_THRESHOLD) > config.FIT_SCORE_REVERIFY_MARGIN:
        return first

    scores = [first["fit_score"]]
    for _ in range(config.FIT_SCORE_REVERIFY_PASSES - 1):
        result = score_job(job_description, resume_profile, resume_embedding=resume_embedding)
        if result["fit_score"] is not None:
            scores.append(result["fit_score"])

    if len(scores) == 1:
        log.warning(
            "fit_score %d was in the gray zone (threshold %d +/- %d) but every re-verification "
            "pass failed transiently - using the single first-pass score.",
            first["fit_score"],
            config.FIT_SCORE_THRESHOLD,
            config.FIT_SCORE_REVERIFY_MARGIN,
        )
        return first

    average = round(sum(scores) / len(scores))
    return {
        "fit_score": average,
        "reason": f"{first['reason']} (re-verified across {len(scores)} passes: {scores}, averaged to {average})",
        "recommend_apply": average >= config.FIT_SCORE_THRESHOLD,
    }
