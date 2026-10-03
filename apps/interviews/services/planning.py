"""
The interview kit for one posting, built once and reused.

Planning an interview is the largest generation this product makes: three to
five competencies, five to eight questions each, all of them written for a
specific role. On a modest model that is tens of seconds, and it was being done
**while the candidate waited for their practice interview to open**. In
practice it timed out and every session quietly fell back to the four generic
competencies — the feature looked implemented and did nothing.

Three facts make a better shape obvious:

  * **A plan is a property of the job, not of the candidate.** Everybody
    interviewing for the same posting should face the same pool; that is what
    makes the scores comparable, which is the whole point of a structured
    interview.
  * **So it can be cached.** One generation per posting, reused by every
    practice run and every screening interview against it.
  * **And it is worth something to the employer.** Stored as a company-owned
    `InterviewTemplate`, the plan is an interview kit a recruiter can read and
    edit — which is a feature, not a cache artefact.

What the candidate waits for is therefore nothing: a session opens immediately
on whatever pool exists now, and the tailored kit is built off the request. The
first person to practise for a new posting gets general questions; everybody
after them gets the role's own.

**Personalisation is not in here.** A pool shared by every applicant must not
contain one applicant's CV — that is both a privacy leak and the wrong shape.
An interview responds to the individual through its follow-up questions, which
are written after reading the answer that was just given.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from apps.ai.models import RunStatus
from apps.ai.services import runner
from apps.common.hashing import stable_hash

from ..models import InterviewKind, InterviewTemplate
from . import relevance

if TYPE_CHECKING:  # pragma: no cover
    from apps.jobs.models import Job

logger = logging.getLogger(__name__)

#: Marks a template this module wrote, so a recruiter's own framework is never
#: overwritten by a regeneration.
AUTO_SOURCE = "ai_plan"

#: How long a kit stays fresh with the posting unchanged. A generated plan does
#: not rot, but a model that has improved is worth picking up eventually.
MAX_AGE_DAYS = 90


def slug_for(job: Job) -> str:
    return f"auto-{job.slug}"[:140]


def cached_pools(job: Job) -> dict[str, Any] | None:
    """This posting's kit, if one exists and still describes the posting.

    Keyed on a digest of the words the plan was written from — the title, the
    level, the description and the skills. An employer who rewrites the advert
    gets a new kit; an employer who fixes a typo in the benefits does not pay
    for one.
    """
    template = _existing(job)
    if template is None or not template.question_pools:
        return None

    config = template.config or {}
    if config.get("source") != AUTO_SOURCE:
        # A recruiter has taken this template over. Their edits win, always.
        return dict(template.question_pools)

    if config.get("source_sha256") != _digest(job):
        return None

    # Filtered on read as well as on write. Kits written before a rule was
    # tightened are still cached for ninety days, and measured across the 26
    # kits in the development database 14% of their questions were "how do you
    # ensure..." or "walk me through how you would..." — the rehearsed-answer
    # shape the prompt bans. A rule that only applies to kits written from now
    # on leaves every candidate this quarter answering the old ones.
    posting = relevance.Posting.from_job(job)
    others = other_posting_phrases(job)
    pools = _pools_from(
        {"competencies": [{"key": key, **pool} for key, pool in template.question_pools.items()]},
        junior=is_junior(job),
        posting=posting,
        other_phrases=others,
    )
    # Judged on the kit as stored, not as served: serving has already removed
    # borrowed questions, so a kit containing them looked clean and was never
    # rewritten.
    stored = {key: pool for key, pool in template.question_pools.items() if isinstance(pool, dict)}
    if pools and (not kit_is_relevant(stored, posting, others) or thin_pools(pools)):
        # Too few questions about this posting, questions borrowed from another
        # one, or a competency left with fewer than three servable questions —
        # where every candidate is asked the same pair. Served as filtered
        # below, and rewritten off the request (at most once a day) so the next
        # candidate gets a pool that rotates.
        _queue_replan(job)
    return pools or None


def thin_pools(pools: dict[str, Any], *, min_pool: int = relevance.MIN_POOL) -> list[str]:
    """Competencies serving fewer than `min_pool` questions.

    The Interview report measured served pools of 2 on some competencies:
    filtering (hypothetical, near-duplicate, borrowed) had left two, and a kit
    judged relevant was never rewritten, so two candidates always got the
    same pair there.
    """
    return sorted(
        key
        for key, pool in pools.items()
        if isinstance(pool, dict) and len(pool.get("questions") or []) < min_pool
    )


def plan_job(*, job: Job) -> InterviewTemplate | None:
    """Write this posting's interview kit. Safe to call twice.

    Returns the template, or `None` when the model could not be reached — which
    is not an error here: the caller already has a pool to run on, and this is
    the thing that makes the next interview better rather than this one
    possible.
    """
    existing = _existing(job)
    if existing is not None and (existing.config or {}).get("source") != AUTO_SOURCE:
        # Recruiter-owned. Never regenerate over somebody's own framework.
        return existing

    inputs = _plan_inputs(job)
    run = runner.run(task_key="interview.plan", inputs=inputs, company=job.company, input_ref=job)
    if run.status == RunStatus.SCHEMA_ERROR:
        # The local model sometimes writes one competency and stops: valid JSON,
        # 405 tokens, `minItems: 2` refused, two minutes of generation thrown
        # away (2 of the last 5 runs in development). The runner does not retry
        # schema errors because a deterministic model would repeat them; this
        # one is not deterministic, the work is off the request, and one more
        # attempt is cheaper than a posting with no kit.
        logger.info("interview kit for job %s failed its schema; asking once more", job.pk)
        run = runner.run(
            task_key="interview.plan", inputs=inputs, company=job.company, input_ref=job, force=True
        )
    if run.status != RunStatus.OK:
        logger.info("interview kit unavailable for job %s: %s", job.pk, run.status)
        return None

    pools = _pools_from(
        run.output or {},
        junior=is_junior(job),
        posting=relevance.Posting.from_job(job),
        other_phrases=other_posting_phrases(job),
    )
    if not pools:
        logger.info("interview kit for job %s came back empty", job.pk)
        return None

    return _store(job=job, pools=pools)


def _plan_inputs(job: Job) -> dict[str, Any]:
    """The structured posting, as the prompt reads it (INT-04).

    The posting's own statements of the job are numbered so the prompt can ask
    for every one to be covered — generated kits left 51% of their questions
    naming nothing from the posting, and a list to work through is harder to
    ignore than a paragraph. They now include the employer's structured
    responsibilities and requirements, and skills arrive split into required
    and preferred; before, only sentences cut out of the description and one
    flat skills list reached the model.
    """
    from . import job_context

    return job_context.from_job(job).as_prompt_inputs()


@transaction.atomic
def _store(*, job: Job, pools: dict[str, Any]) -> InterviewTemplate:
    template, _ = InterviewTemplate.objects.update_or_create(
        company=job.company,
        slug=slug_for(job),
        defaults={
            "name": f"{job.title} — interview kit",
            "kind": InterviewKind.AI_TEXT,
            "question_pools": pools,
            "is_active": True,
            "config": {
                "source": AUTO_SOURCE,
                "job_id": job.pk,
                "job_public_id": str(job.public_id),
                "source_sha256": _digest(job),
                "written_at": timezone.now().isoformat(),
            },
        },
    )
    return template


def _existing(job: Job) -> InterviewTemplate | None:
    if not job.company_id:
        return None
    return InterviewTemplate.objects.filter(company_id=job.company_id, slug=slug_for(job)).first()


def _digest(job: Job) -> str:
    """What the plan was written from, as one hash."""
    return stable_hash(
        {
            "title": job.title,
            "level": job.experience_level,
            "description": job.description or "",
            "skills": sorted(row.skill.name for row in job.skills.all() if row.skill_id),
            # Everything else the prompt now reads (INT-04). Without these, a
            # kit written before an employer edited the responsibilities — or
            # before they reached the prompt at all — was kept for ever.
            "responsibilities": list(job.responsibilities or []),
            "requirements": list(job.requirements or []),
            "required": sorted(
                row.skill.name
                for row in job.skills.all()
                if row.skill_id and row.requirement == "required"
            ),
        }
    )


def _pools_from(
    output: dict[str, Any],
    *,
    junior: bool = False,
    posting: relevance.Posting | None = None,
    other_phrases: set[tuple[str, str]] | None = None,
    semantic: dict[str, float] | None = None,
) -> dict[str, Any]:
    pools: dict[str, Any] = {}
    own = employer_things(posting) if posting is not None else set()
    for item in output.get("competencies", []):
        key = str(item.get("key", ""))[:40]
        questions = [
            text
            for question in item.get("questions", [])
            if (text := indefinite(str(question).strip()[:400], own))
            and not _is_hypothetical(text)
            and not (junior and _assumes_seniority(text))
        ]
        questions = _drop_near_duplicates(questions)
        if posting is not None:
            scored = relevance.score_questions(
                questions, posting, other_phrases=other_phrases, semantic_scores=semantic
            )
            questions = relevance.choose_served(scored)
        if not key or not questions:
            continue
        pools[key] = {
            "label": str(item.get("label", ""))[:80],
            "why": str(item.get("why", ""))[:200],
            "questions": questions,
        }
    return pools


#: Words that end a noun phrase lifted from the posting: "the Django service
#: that takes every reservation" names "Django service".
_PHRASE_END = frozenset(
    [
        "a",
        "an",
        "the",
        "our",
        "your",
        "their",
        "its",
        "of",
        "for",
        "from",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "and",
        "or",
        "but",
        "that",
        "which",
        "who",
        "whom",
        "whose",
        "we",
        "you",
        "they",
        "it",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "will",
        "can",
        "should",
        "must",
        "as",
        "so",
        "than",
        "into",
        "onto",
        "over",
        "under",
        "across",
        "through",
        "every",
        "each",
        "all",
    ]
)

#: Openings that make a two-word phrase a figure of speech, not a system.
_NOT_A_THING = frozenset(
    ["same", "whole", "right", "best", "first", "next", "last", "other", "entire", "following"]
)

#: A lookahead, so "the experience of our booking app" is read from "the" and
#: again from "our" rather than consumed whole by the first.
_OWN_THING = re.compile(r"\b(?:our|the)\s+(?=([A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*){0,4}))")


def employer_things(posting: relevance.Posting) -> set[str]:
    """The systems and products this employer names as its own.

    "Own the experience of **our guest-facing booking app**", "work on **the
    Django service** that takes every reservation", "maintain **the product
    design system**". Multi-word only: "the team" and "the role" are not
    things anybody could claim to have worked on.
    """
    things: set[str] = set()
    for text in (posting.description, *posting.duties):
        for match in _OWN_THING.finditer(text or ""):
            words: list[str] = []
            for word in match.group(1).split():
                if word.lower() in _PHRASE_END:
                    break
                words.append(word.lower())
            if len(words) >= 2 and words[0] not in _NOT_A_THING:
                things.add(" ".join(words))
    return things


def indefinite(question: str, things: set[str]) -> str:
    """Talk about "a" thing like the employer's, never "the" employer's own.

    The prompt has said this since INT-04 — "say 'a', not 'the', about anything
    on this posting" — and the local model ignores it: measured on the
    development kits, the UI/UX Designer kit asked candidates about the
    research they did "to improve the guest-facing booking app", which is this
    employer's product and which no candidate has ever worked on, and the
    Backend kit asked about "the Django service at your previous job". The
    only honest answer is "I haven't", and it scores 1.

    Rewritten rather than dropped: "a guest-facing booking app" is exactly the
    question the prompt asks for, and dropping every such question left pools
    too thin to rotate.
    """
    for thing in sorted(things, key=len, reverse=True):
        pattern = re.compile(rf"\b(the|our)\s+({re.escape(thing)})\b", re.IGNORECASE)

        def article(match: re.Match[str]) -> str:
            first = match.group(1)
            word = "an" if match.group(2)[:1].lower() in "aeiou" else "a"
            return (word.capitalize() if first[:1].isupper() else word) + " " + match.group(2)

        question = pattern.sub(article, question)
    return question


#: Questions that do not ask for something the candidate did. The prompt bans
#: them and the models write them anyway, in three shapes:
#:
#:   * **hypothetical** — "how would you debug a system that is not processing
#:     transactions?", including the disguised "walk me through how you would
#:     design...", which the first version of this list missed;
#:   * **definitional** — "what is your definition of idempotency?";
#:   * **habitual** — "how do you ensure data integrity?", "what steps do you
#:     take to...". Present tense invites the textbook answer rather than an
#:     account of a real piece of work, and it is the commonest of the three.
#:
#: A habitual opening followed by a request for a real example ("...? Give an
#: example of a time you did") is kept: the second half is a behavioural
#: question and is what the candidate will answer. A hypothetical is never
#: rescued that way — the candidate answers the hypothetical.
#:
#: Dropped here so the guarantee does not depend on which model wrote the kit.
_HYPOTHETICAL = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bhow (would|will|might|could|should) you\b",
        r"\bwhat (would|will|might|could|should) you\b",
        r"\bhow you (would|will|might|could|should)\b",
        r"\bwhat you (would|will|might|could|should)\b",
        r"\bhow do you think you would\b",
        r"\bif you (were|had|are|have to|needed to|joined)\b",
        r"\bimagine\b",
        r"\bsuppose\b",
        r"\bhypothetical",
    )
)
_DEFINITIONAL = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bwhat is your definition\b",
        r"\bwhat do you understand by\b",
        r"\bcan you define\b",
        r"^(what|which) (is|are) (a|an|the)\b",
        r"\bwhat is the difference between\b",
    )
)
_HABITUAL = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bhow do you (usually |typically |generally |normally )?"
        r"(ensure|handle|approach|manage|keep|stay|deal|go about|prioriti[sz]e|make sure|decide|"
        r"measure|balance|maintain|stay|organi[sz]e|track|avoid|prevent|test|verify|communicate)\b",
        r"\bwhat (steps|methods|strategies|techniques|tools|practices|processes|process|"
        r"approach|criteria) do you (use|take|follow|apply|employ|rely on)\b",
        r"\bwhat is your (approach|process|strategy|method)\b",
        # "Walk me through how you ensure..." is the same habitual question
        # without the "do". Past tense ("how you ensured") does not match.
        r"\bhow you (usually |typically |generally |normally )?"
        r"(ensure|handle|approach|manage|keep|deal|go about|prioriti[sz]e|make sure|decide|"
        r"measure|balance|maintain|organi[sz]e|track|avoid|prevent|test|verify|communicate)\b",
    )
)
#: What turns a habitual opening into a behavioural question.
_ASKS_FOR_EXAMPLE = re.compile(
    r"\b(tell me about a time|describe a time|a time (when|you)|give (me )?an example|"
    r"provide an example|share an example|an example of|walk me through a (time|situation|project|case|specific)|"
    r"describe a (situation|project|case|specific)|tell me about a (project|situation|case))\b"
)


def _is_hypothetical(question: str) -> bool:
    """Whether a question fails to ask for something the candidate did."""
    lowered = " ".join(question.lower().split())
    if any(pattern.search(lowered) for pattern in _HYPOTHETICAL):
        return True
    if any(pattern.search(lowered) for pattern in _DEFINITIONAL):
        return True
    if any(pattern.search(lowered) for pattern in _HABITUAL):
        return not _ASKS_FOR_EXAMPLE.search(lowered)
    return False


#: Levels at which somebody has not yet had people or budgets to answer for.
_JUNIOR_LEVELS = frozenset({"intern", "internship", "entry", "junior", "graduate", "trainee"})
_JUNIOR_TITLES = ("intern", "trainee", "graduate", "junior", "apprentice")

#: Experience a junior candidate has had no chance to have. Measured in the
#: development kits: a Design Intern was asked to "walk me through a situation
#: where you had to delegate tasks to a team member", and an entry-level HR
#: Officer to describe running a performance review. The only honest answer is
#: "I haven't", which scores 1 on a rubric that then measures their job title.
_SENIOR_ONLY = (
    "delegat",
    "mentor",
    "direct report",
    "performance review",
    "hiring decision",
    "managed a team",
    "led a team",
    "your team's budget",
    "line manag",
    "people you managed",
)


def is_junior(job: Job) -> bool:
    level = (job.experience_level or "").lower()
    title = (job.title or "").lower()
    return level in _JUNIOR_LEVELS or any(word in title for word in _JUNIOR_TITLES)


def _assumes_seniority(question: str) -> bool:
    lowered = question.lower()
    return any(phrase in lowered for phrase in _SENIOR_ONLY)


# --- relevance to the posting ---------------------------------------------

#: Share of served questions that must be specific to the posting before a kit
#: is left alone. Two thirds, the rule the plan prompt has always stated.
MIN_RELEVANT_SHARE = 2 / 3


def kit_is_relevant(
    pools: dict[str, Any],
    posting: relevance.Posting,
    other_phrases: set[tuple[str, str]] | None = None,
) -> bool:
    """Whether a kit is about this job: mostly specific, and nothing borrowed.

    Measured over the kit rather than per competency. A communication
    competency is allowed a generic question; a kit made mostly of them is not
    a kit for this role.
    """
    questions = [q for pool in pools.values() for q in pool.get("questions", [])]
    if not questions:
        return True
    scored = relevance.score_questions(questions, posting, other_phrases=other_phrases)
    if any(row.borrowed for row in scored):
        return False
    return sum(row.specific for row in scored) / len(scored) >= MIN_RELEVANT_SHARE


def other_posting_phrases(job: Job, *, limit: int = 1000) -> set[tuple[str, str]]:
    """Two-word phrases from other live postings, to catch questions borrowed from them.

    Cached for ten minutes: the set changes when postings do, and computing it
    on every practice start would re-read every posting.
    """
    from django.core.cache import cache

    from apps.jobs.models import Job as JobModel

    key = "interviews:posting-phrases:v1"
    table = cache.get(key)
    if table is None:
        table = {}
        rows = JobModel.objects.filter(deleted_at__isnull=True).prefetch_related("skills__skill")[
            :limit
        ]
        for row in rows:
            table[row.pk] = sorted(relevance.Posting.from_job(row).phrases())
        cache.set(key, table, 600)
    return {tuple(pair) for pk, pairs in table.items() if pk != job.pk for pair in pairs}


def _queue_replan(job: Job) -> None:
    from ..tasks import plan_job_interview

    try:
        plan_job_interview.delay(job.pk, replace=True)
    except Exception:  # noqa: BLE001 - a queue outage must not stop practice
        logger.warning("could not queue a replacement kit for job %s", job.pk, exc_info=True)


#: Share of significant words above which two questions in one competency are
#: the same question. After the kits were rewritten with the anchoring rule the
#: benchmark counted 10 such pairs, up from 3: "Tell me about a billing change
#: you shipped" beside "Describe a billing change you shipped and what happened".
#: A session that asks both has asked one question twice.
NEAR_DUPLICATE_SHARE = 0.6


def _drop_near_duplicates(questions: list[str]) -> list[str]:
    from .engine import _significant

    kept: list[str] = []
    seen: list[set[str]] = []
    for question in questions:
        words = _significant(question)
        if words and any(
            earlier and len(words & earlier) / min(len(words), len(earlier)) >= NEAR_DUPLICATE_SHARE
            for earlier in seen
        ):
            continue
        kept.append(question)
        seen.append(words)
    return kept
