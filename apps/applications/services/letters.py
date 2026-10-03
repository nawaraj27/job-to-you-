"""
Cover letters (FR-6.1, SRS §9.4 `cover_letter.draft`).

Its own module rather than more of `apply.py`, which is about submitting an
application. A letter outlives the application it was written for, is reused
across applications, and is edited long after — none of which is applying.

**The rule this feature exists under: a draft is a draft.** The model writes a
first version into a letter the candidate then owns and edits; nothing is sent
until they submit an application carrying it. That is the same shape as INV-3
for resumes, reached differently — a resume needs a `ResumeSuggestion` because a
draft already exists and must not be overwritten, while a cover letter *is* the
new thing being made, so the letter itself is the proposal.

Two things the AI half must never do, both enforced here rather than asked for
in a prompt:

  * **It never claims experience the candidate does not have.** The prompt is
    given their skills and roles as data and told to use nothing else, and the
    letter is rendered beside a note saying to check it. A model that invents a
    job is the single worst failure this feature can have, because the
    candidate is the one who gets caught with it.
  * **It never sends anything.** There is no code path from drafting to
    submission. Applying is a separate, explicit act (FR-6.1).

Everything degrades: with AI off, out of budget or the provider down, the
editor still works and the candidate writes their own letter, which is what
they did before this feature existed (SRS §9.3.6).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from django.db import transaction

from apps.ai.models import AIRun, RunStatus
from apps.ai.services import runner
from apps.audit import services as audit
from apps.common.errors import InvalidState, NotFound
from apps.common.ids import as_uuid

from ..models import CoverLetter, LetterSource, LetterTone

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.candidates.models import CandidateProfile
    from apps.jobs.models import Job

logger = logging.getLogger(__name__)

TASK_KEY = "cover_letter.draft"

#: Hard ceiling on stored letter length. Generous — some markets expect a full
#: page — but bounded, because an unbounded text column reached by a model is a
#: cost and a rendering problem at the same time.
MAX_CHARS = 20_000

#: How many letters one candidate may keep. A library, not an archive: past a
#: couple of dozen nobody finds the one they want, and the count is what stops
#: "generate" becoming an accidental denial-of-service on their own list.
MAX_LETTERS = 40

#: How much of a job description the model is given. Enough to know what the
#: role is; short enough that a posting padded with boilerplate cannot spend
#: the whole budget.
MAX_JOB_CHARS = 4_000


@transaction.atomic
def save(
    *,
    profile: CandidateProfile,
    actor: User,
    content: str,
    title: str = "",
    job: Job | None = None,
    source: str = LetterSource.WRITTEN,
    tone: str = "",
    ai_run: AIRun | None = None,
) -> CoverLetter:
    """Store a new letter.

    Was `apply.save_cover_letter`; moved here in P14 with the other letter
    operations, because a module named for submitting applications was not
    where anybody looked for letter CRUD.
    """
    text = (content or "").strip()
    if not text:
        raise InvalidState("A cover letter needs something in it.")

    _guard_count(profile)

    letter = CoverLetter.objects.create(
        profile=profile,
        job=job,
        title=(title or "").strip()[:120] or _default_title(job),
        content=text[:MAX_CHARS],
        source=source if source in LetterSource.values else LetterSource.WRITTEN,
        tone=tone if tone in LetterTone.values else "",
        ai_run=ai_run,
    )
    audit.record(
        verb="cover_letter.saved",
        actor=actor,
        target=letter,
        metadata={"source": letter.source, "job": str(job.public_id) if job else ""},
    )
    return letter


@transaction.atomic
def update(
    *, letter: CoverLetter, actor: User, content: str, title: str | None = None
) -> CoverLetter:
    """Edit a letter the candidate has not sent yet.

    A sent letter is refused rather than copied silently. The candidate asked to
    change *this* letter, and quietly editing a duplicate instead would leave
    them believing they had changed what the employer will read.
    """
    if letter.is_sent:
        raise InvalidState(
            "This letter has already been sent with an application, so it cannot be changed. "
            "Duplicate it and edit the copy."
        )

    text = (content or "").strip()
    if not text:
        raise InvalidState("A cover letter needs something in it.")

    fields = ["content", "updated_at"]
    letter.content = text[:MAX_CHARS]

    # A model drafted it and a person changed it: that is `ai_edited`, and it is
    # the honest state for most of what this feature produces.
    if letter.source == LetterSource.AI_DRAFT:
        letter.source = LetterSource.AI_EDITED
        fields.append("source")

    if title is not None:
        letter.title = title.strip()[:120] or letter.title
        fields.append("title")

    letter.save(update_fields=fields)
    audit.record(verb="cover_letter.updated", actor=actor, target=letter)
    return letter


@transaction.atomic
def duplicate(*, letter: CoverLetter, actor: User) -> CoverLetter:
    """Copy a letter, so a sent one can be the basis of the next.

    The copy carries the original's `source` and `ai_run`: it is the same words,
    and pretending a copy of an AI draft was hand-written would make the field
    useless the first time anybody duplicated anything.
    """
    _guard_count(letter.profile)

    copy = CoverLetter.objects.create(
        profile=letter.profile,
        job=letter.job,
        title=f"{letter.title} (copy)"[:120],
        content=letter.content,
        source=letter.source,
        tone=letter.tone,
        ai_run=letter.ai_run,
    )
    audit.record(verb="cover_letter.duplicated", actor=actor, target=copy)
    return copy


@transaction.atomic
def delete(*, letter: CoverLetter, actor: User) -> None:
    """Remove a letter the candidate has not sent.

    A sent letter is `PROTECT`-shaped by intent even though the column is
    `SET_NULL`: deleting it would blank what an employer read on an application
    they are still working through.
    """
    if letter.is_sent:
        raise InvalidState(
            "This letter was sent with an application, so it stays on the record. "
            "You can delete the application instead."
        )

    audit.record(verb="cover_letter.deleted", actor=actor, target=letter.profile)
    letter.delete()


def draft(
    *,
    profile: CandidateProfile,
    actor: User,
    job: Job,
    tone: str = LetterTone.DIRECT,
    notes: str = "",
) -> tuple[CoverLetter | None, AIRun]:
    """Ask for a first draft (SRS §9.4).

    Returns `(letter, run)`. **`letter` is `None` when the model could not be
    reached** — switched off, out of budget, provider down, output that failed
    its schema — and the caller shows the empty editor with a sentence saying
    the draft is unavailable. The candidate writes their own letter, which is
    what they did before this existed.

    The run is returned either way, because a failed call is a fact worth
    reporting to the person who is now typing instead (ADR-A11).
    """
    if tone not in LetterTone.values:
        tone = LetterTone.DIRECT

    # One cover letter draft from the plan's allowance (ADR-A59), refunded
    # when no usable draft comes back. Writing a letter by hand is never metered.
    from apps.billing.services import entitlements

    entitlements.claim(profile, "cover_letters")
    run = runner.run(
        task_key=TASK_KEY,
        inputs=_inputs(profile=profile, job=job, tone=tone, notes=notes),
        user=actor,
        input_ref=job,
    )
    if run.status != RunStatus.OK:
        logger.info("cover letter draft unavailable: %s", run.status)
        entitlements.release(profile, "cover_letters")
        return None, run

    body = _body_from(run.output, profile=profile, job=job)
    if not body:
        entitlements.release(profile, "cover_letters")
        return None, run

    body = _as_a_letter(body, profile=profile)

    letter = save(
        profile=profile,
        actor=actor,
        content=body,
        title=_default_title(job),
        job=job,
        source=LetterSource.AI_DRAFT,
        tone=tone,
        ai_run=run,
    )
    return letter, run


def find(*, profile: CandidateProfile, public_id: str) -> CoverLetter:
    """One of this candidate's letters, by public id.

    `NotFound` for somebody else's, never `NotAllowed`: a 403 would confirm that
    a letter with that id exists, and what a named person wrote to a named
    employer is exactly what should not be confirmable.
    """
    if as_uuid(public_id) is None:
        raise NotFound("That cover letter does not exist.")
    letter = (
        CoverLetter.objects.filter(profile=profile, public_id=public_id)
        .select_related("job", "ai_run")
        .first()
    )
    if letter is None:
        raise NotFound("That cover letter does not exist.")
    return letter


# --- internals ------------------------------------------------------------


def _inputs(*, profile: CandidateProfile, job: Job, tone: str, notes: str) -> dict[str, Any]:
    """What the model is given, and nothing more.

    Skills and roles are passed as **data the model may draw on**, never as
    prose it can extend. The distinction matters: given a summary it will
    embellish, given a list it tends to use what is in the list. Neither is a
    guarantee — which is why the screen tells the candidate to check it.

    No contact details, no email, no phone (SRS §9.3.7). A letter needs the
    candidate's name and nothing else identifying, and the name is theirs to
    put on it.
    """
    skills = [row.skill.name for row in profile.skills.select_related("skill")[:20]]
    roles = [f"{row.title} at {row.org_name}" for row in profile.experiences.all()[:6] if row.title]
    missing = missing_skills(profile=profile, job=job)

    return {
        "job_title": job.title,
        "company_name": job.company.name,
        "job_description": (job.description or "")[:MAX_JOB_CHARS],
        "job_skills": ", ".join(row.skill.name for row in job.skills.select_related("skill")[:15]),
        "candidate_name": profile.user.full_name or "the candidate",
        "candidate_headline": profile.headline or "",
        "candidate_skills": ", ".join(skills) or "none listed",
        "candidate_roles": "; ".join(roles) or "none listed",
        "tone": tone,
        "notes": (notes or "").strip()[:500],
        "missing_skills": ", ".join(missing) or "none",
    }


#: The sentences every generated cover letter writes, and every reader has
#: learned to skip. Each is a claim about the candidate's own suitability or
#: enthusiasm — the employer's conclusion to reach, not the applicant's to
#: assert — or an announcement that they are applying, which the envelope
#: already said.
CLICHES: tuple[tuple[str, str], ...] = (
    ("i am writing to", "The letter itself says you are writing. Start with what you do."),
    ("i am reaching out", "Same: say what you do and why this role."),
    ("i would like to express", "Say the thing instead of expressing interest in it."),
    ("i am applying for", "They know. Open with something only you could write."),
    ("passionate about", "Show it with something you did, or cut it."),
    ("i am excited", "Excitement is not evidence. What did you build?"),
    ("i am eager", "Eager to do what, and what have you already done?"),
    ("great fit", "Whether you fit is their conclusion. Give them the facts for it."),
    ("strong fit", "Whether you fit is their conclusion. Give them the facts for it."),
    ("perfect fit", "Whether you fit is their conclusion. Give them the facts for it."),
    ("well-equipped", "Equipped by what? Name it."),
    ("well equipped", "Equipped by what? Name it."),
    ("proven track record", "A record of what? One number beats the phrase."),
    ("i am confident that", "Confidence is not a qualification. Cut it."),
    ("i believe my", "Cut the belief and leave the fact."),
    ("bring to the table", "Say what you bring."),
    ("hit the ground running", "Everybody says this. Say what you would do first."),
    ("team player", "Nobody writes 'poor team player'. Cut it."),
    ("valuable asset", "Cut it."),
)


def cliches_in(text: str) -> list[dict[str, str]]:
    """The stock phrases in a letter, with what to do about each.

    A letter is edited, not regenerated, so this points rather than deletes: the
    sentence carrying "passionate about the tax engine" is also the sentence
    carrying the tax engine, and cutting it for the candidate would take the
    one specific thing with it.

    Deterministic and shown as advice, never as a score. It is the same thing a
    person who reads applications for a living says about the draft, and it is
    the difference between a letter that reads as written and one that reads as
    generated.
    """
    lowered = " ".join((text or "").lower().replace("’", "'").split())
    return [{"phrase": phrase, "why": why} for phrase, why in CLICHES if phrase in lowered]


def missing_skills(*, profile: CandidateProfile, job: Job) -> list[str]:
    """Skills this posting asks for that this candidate has not listed.

    Set arithmetic over taxonomy ids, which is what both sides already store —
    so this is the same answer the matcher gives, and it is an answer rather
    than an opinion.

    It used to be the model's job: the prompt asked for `gaps` and the editor
    printed whatever came back. On a profile listing Django and Kubernetes the
    model reported both as gaps, so the screen told the candidate she lacked two
    skills the match page scored 4-of-4, and the letter itself volunteered a
    sentence about not having them. A number a product computes must not be
    contradicted by a model it also ships.
    """
    theirs = set(profile.skills.values_list("skill_id", flat=True))
    return [
        row.skill.name for row in job.skills.select_related("skill") if row.skill_id not in theirs
    ]


#: What a letter opens with when nobody at the company is named. The same
#: words the "write it myself" path starts with, so the two look like one
#: product.
SALUTATION = "Dear hiring team,"
SIGN_OFF = "Yours sincerely,"


def _as_a_letter(body: str, *, profile: CandidateProfile) -> str:
    """Put a greeting and a sign-off around the drafted paragraphs.

    The system prompt tells the model not to write them because "the platform
    adds those" — and the platform did not. An AI draft therefore began with
    its first sentence and ended with its last, so a candidate who accepted it
    attached a letter with no greeting and no name at the bottom.

    Deterministic on purpose: a salutation is not a language problem, and
    spending model tokens on two fixed lines invites a model to vary them.
    """
    name = (profile.user.full_name or "").strip()
    parts = [SALUTATION, body.strip(), SIGN_OFF]
    if name:
        parts.append(name)
    return "\n\n".join(parts)


def _body_from(
    output: dict[str, Any],
    *,
    profile: CandidateProfile | None = None,
    job: Job | None = None,
) -> str:
    """The letter text out of a validated output.

    The schema asks for paragraphs rather than one blob, because a model given
    "write a letter" returns a wall, and because the candidate edits it
    paragraph by paragraph. Joined here so the stored letter is plain text:
    what gets sent is text, and storing a structure would mean every reader of
    a `CoverLetter` had to know the shape.

    Paragraphs are also the unit this can drop one of. A draft came back
    reading "My role at Himalaya Ledger Systems has given me a deep
    understanding of..." — addressed to Himalaya Ledger Systems, from somebody
    who has never worked there. The model had the company name in front of it
    and wrote the candidate into it. An invented employer in a letter sent to
    that employer is the worst single output this product can produce, so the
    paragraph carrying it does not survive.
    """
    paragraphs = [
        str(item).strip() for item in output.get("paragraphs", []) or [] if str(item).strip()
    ]
    if not paragraphs:
        return str(output.get("letter", "") or "").strip()

    if profile is not None and job is not None:
        absent = missing_skills(profile=profile, job=job)
        kept = [
            paragraph
            for paragraph in paragraphs
            if not _claims_working_there(paragraph, profile=profile, job=job)
            and not _claims_a_skill_they_lack(paragraph, absent)
        ]
        if len(kept) != len(paragraphs):
            logger.info(
                "dropped %s unfounded cover-letter paragraph(s) for %s",
                len(paragraphs) - len(kept),
                job.company.name,
            )
        paragraphs = kept

    return "\n\n".join(paragraphs)


#: How a letter says somebody worked somewhere. Checked against the hiring
#: company's own name, so it only trips when the candidate has no role there.
_WORKED_AT = (
    "my role at {name}",
    "my time at {name}",
    "my work at {name}",
    "my experience at {name}",
    "my years at {name}",
    "at {name} i",
    "while at {name}",
    "during my time at {name}",
    "working at {name}",
    "worked at {name}",
    "i work at {name}",
    "i have worked at {name}",
    "here at {name}",
    "my tenure at {name}",
    "my position at {name}",
)


def _claims_a_skill_they_lack(paragraph: str, absent: list[str]) -> bool:
    """Whether the paragraph claims a skill the candidate has not listed.

    The prompt is given this list and told not to use it. A draft came back
    saying "In my role at Fonepay, I developed REST APIs" for a candidate whose
    profile does not list REST APIs, on a posting whose one missing skill is
    REST APIs — the model reading the requirement and writing it in.

    Named skills only, and only where the posting asks for them, so this cannot
    quietly delete a paragraph about work the candidate really did: the list is
    exactly what the matcher already tells them they are missing, and the
    screen shows it beside the draft.
    """
    if not absent:
        return False

    text = _flatten(paragraph)
    return any(_flatten(skill) and _flatten(skill) in text for skill in absent)


def _claims_working_there(paragraph: str, *, profile: CandidateProfile, job: Job) -> bool:
    """Whether this paragraph says the candidate worked at the hiring company.

    Only a problem when it is not true. Plenty of people apply to a company
    they already work for — an internal move is an ordinary thing — so the
    candidate's own roles are checked first and the paragraph stands if one of
    them names this employer.
    """
    name = (job.company.name or "").strip()
    if not name:
        return False

    employers = {
        _flatten(row.org_name) for row in profile.experiences.all() if (row.org_name or "").strip()
    }
    if _flatten(name) in employers:
        return False

    text = _flatten(paragraph)
    target = _flatten(name)
    return any(phrase.format(name=target) in text for phrase in _WORKED_AT)


def _flatten(text: str) -> str:
    """Lowercased, punctuation-free, single-spaced, for comparing names."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _default_title(job: Job | None) -> str:
    return f"For {job.title}"[:120] if job is not None else "Cover letter"


def check_room(profile: CandidateProfile) -> None:
    """Refuse before any work when the candidate's letter list is full."""
    _guard_count(profile)


def _guard_count(profile: CandidateProfile) -> None:
    if profile.cover_letters.count() >= MAX_LETTERS:
        raise InvalidState(
            f"You have {MAX_LETTERS} saved letters, which is the limit. "
            "Delete one you no longer need."
        )
