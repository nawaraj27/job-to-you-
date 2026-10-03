"""
Writing a posting: create, edit, skills, questions, assignment, cloning.

Everything here is company-scoped by argument rather than by lookup — the
company comes from the caller's resolved membership (ADR-A18), never from a
field in the form, so a request cannot name a company it does not belong to.

Salary is normalised at write time with its FX provenance (ADR-A39), exactly as
the candidate side does it: the figures the employer typed are what the posting
shows, and the annualised base is what comparison will use in P13.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils.text import slugify

from apps.audit import services as audit
from apps.common.errors import Conflict, InvalidState, NotFound, StaleWrite
from apps.notifications import events
from apps.taxonomy.models import JobCategory, Location, TermSource
from apps.taxonomy.services import money
from apps.taxonomy.services import skills as skill_resolver
from apps.taxonomy.services import terms as term_ledger

from ..models import (
    ApplyMode,
    EmploymentType,
    ExperienceLevel,
    Job,
    JobAssignment,
    JobScreeningQuestion,
    JobSkill,
    JobStatus,
    QuestionKind,
    RemoteScope,
    Requirement,
    Workplace,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import Company, CompanyMembership

logger = logging.getLogger(__name__)

#: Fields a form may write. Anything else in a request is dropped rather than
#: stored: status is the state machine's, counters are the platform's.
EDITABLE = (
    "title",
    "description",
    "responsibilities",
    "requirements",
    "benefits",
    "category_id",
    "location_id",
    "workplace",
    "remote_scope",
    "remote_countries",
    "employment_type",
    "experience_level",
    "salary_min",
    "salary_max",
    "currency",
    "period",
    "salary_visible",
    "apply_mode",
    "external_url",
    "application_cooldown_days",
    "closes_at",
)

MAX_SKILLS = 20
MAX_QUESTIONS = 15
MAX_LIST_ITEMS = 25
_SLUG_MAX_ATTEMPTS = 50


@transaction.atomic
def create_job(*, company: Company, actor: User, title: str, **fields: Any) -> Job:
    """Start a posting. Always a draft — publishing is its own decision."""
    cleaned = _clean(dict(fields), title=title)

    job = Job.objects.create(
        company=company,
        slug=_unique_slug(company, cleaned.get("title") or title),
        created_by=actor,
        status=JobStatus.DRAFT,
        **cleaned,
    )
    _normalise_salary(job)
    job.save(
        update_fields=[
            "salary_min_base",
            "salary_max_base",
            "fx_rate_id",
            "fx_rate_value",
            "fx_as_of",
            "fx_source",
            "updated_at",
        ]
    )

    _assign_author(job, actor)

    events.emit(
        "job.created",
        job,
        payload={
            "job_public_id": str(job.public_id),
            "company_id": company.pk,
            "title": job.title,
        },
    )
    audit.record(
        verb="job.created", actor=actor, target=job, company=company, metadata={"title": job.title}
    )
    return job


@transaction.atomic
def update_job(*, job: Job, actor: User, row_version: int | None = None, **fields: Any) -> Job:
    """Edit a posting.

    A published job stays published while it is edited: withdrawing a live role
    because somebody fixed a typo would lose it its place in every candidate's
    saved list.
    """
    if not job.is_editable:
        raise InvalidState("A closed or archived posting cannot be edited. Reopen it first.")
    if row_version is not None and row_version != job.row_version:
        raise StaleWrite(
            "Someone else changed this posting while you were editing it. "
            "Reload to see their version."
        )

    cleaned = _clean(dict(fields))
    if not cleaned:
        raise InvalidState("There was nothing to save.")

    for key, value in cleaned.items():
        setattr(job, key, value)
    _normalise_salary(job)

    job.row_version += 1
    job.save()

    events.emit(
        "job.updated",
        job,
        payload={
            "job_public_id": str(job.public_id),
            "company_id": job.company_id,
            "fields": sorted(cleaned),
        },
    )
    audit.record(
        verb="job.updated",
        actor=actor,
        target=job,
        company=job.company,
        metadata={"fields": sorted(cleaned)},
    )
    return job


@transaction.atomic
def _assign_author(job: Job, actor: User | None) -> None:
    """ "Create and edit own jobs" (SRS §2.3, D-13).

    Write access flows through assignment, and nothing assigned the author: a
    Recruiter saved a draft — or duplicated a posting — and was told on the next
    click that they could only edit postings they were assigned to.
    """
    from apps.companies.models import CompanyMembership as Membership

    author = (
        Membership.objects.active().filter(company=job.company, user=actor).first()
        if actor is not None
        else None
    )
    if author is not None:
        JobAssignment.objects.get_or_create(
            job=job, membership=author, kind=JobAssignment.Kind.RECRUITER
        )


def clone_job(*, job: Job, actor: User, title: str = "") -> Job:
    """Copy a posting, including its skills and questions.

    Hiring the same role twice is the normal case, and retyping fifteen fields
    is how the second posting ends up subtly different from the first.
    """
    copy = Job.objects.create(
        company=job.company,
        slug=_unique_slug(job.company, title or f"{job.title} (copy)"),
        title=(title or f"{job.title} (copy)")[:160],
        category=job.category,
        description=job.description,
        responsibilities=list(job.responsibilities),
        requirements=list(job.requirements),
        benefits=list(job.benefits),
        location=job.location,
        workplace=job.workplace,
        remote_scope=job.remote_scope,
        remote_countries=list(job.remote_countries),
        employment_type=job.employment_type,
        experience_level=job.experience_level,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        currency=job.currency,
        period=job.period,
        salary_min_base=job.salary_min_base,
        salary_max_base=job.salary_max_base,
        fx_rate_id=job.fx_rate_id,
        fx_rate_value=job.fx_rate_value,
        fx_as_of=job.fx_as_of,
        fx_source=job.fx_source,
        salary_visible=job.salary_visible,
        apply_mode=job.apply_mode,
        external_url=job.external_url,
        application_cooldown_days=job.application_cooldown_days,
        created_by=actor,
        status=JobStatus.DRAFT,
    )

    JobSkill.objects.bulk_create(
        [
            JobSkill(
                job=copy,
                skill_id=row.skill_id,
                requirement=row.requirement,
                weight=row.weight,
                min_years=row.min_years,
                order=row.order,
            )
            for row in job.skills.all()
        ]
    )
    JobScreeningQuestion.objects.bulk_create(
        [
            JobScreeningQuestion(
                job=copy,
                order=row.order,
                kind=row.kind,
                text=row.text,
                options=list(row.options),
                is_required=row.is_required,
                knockout=row.knockout,
            )
            for row in job.questions.all()
        ]
    )

    _assign_author(copy, actor)

    audit.record(
        verb="job.cloned",
        actor=actor,
        target=copy,
        company=job.company,
        metadata={"source": str(job.public_id)},
    )
    return copy


@transaction.atomic
def delete_job(*, job: Job, actor: User) -> Job:
    """Soft delete (ADR-A12).

    Soft because P8's applications point at the posting, and an applicant whose
    job vanished has no way to find out what they applied to.
    """
    from django.utils import timezone

    job.deleted_at = timezone.now()
    job.status = JobStatus.ARCHIVED
    job.row_version += 1
    job.save(update_fields=["deleted_at", "status", "row_version", "updated_at"])

    audit.record(verb="job.deleted", actor=actor, target=job, company=job.company)
    return job


# --- skills ---------------------------------------------------------------


def add_skill(
    *,
    job: Job,
    actor: User,
    text: str,
    requirement: str = Requirement.REQUIRED,
    weight: Any = None,
    min_years: Any = None,
) -> JobSkill:
    """Attach a requirement, resolved through the taxonomy.

    Free text is refused for the same reason it is on a profile: the matcher
    compares `JobSkill.skill` with `CandidateSkill.skill`, and two spellings of
    one skill make that comparison silently wrong. The unresolved term goes to
    the curation queue (ADR-A46) rather than being discarded.

    **Resolution happens before the transaction opens.** The ledger write and
    the refusal it accompanies are the same moment, so a single atomic block
    would roll the ledger row back along with the error — losing exactly the
    evidence that a term is missing, every time it is missing.
    """
    text = (text or "").strip()
    if not text:
        raise InvalidState("Type a skill first.")
    if requirement not in Requirement.values:
        raise InvalidState("A skill is either required or preferred.")
    if job.skills.count() >= MAX_SKILLS:
        raise InvalidState(
            f"A posting lists up to {MAX_SKILLS} skills. A longer list stops meaning anything."
        )

    match = skill_resolver.resolve(text)
    if match is None:
        term_ledger.record(text, source=TermSource.JOB_IMPORT)
        from apps.taxonomy import selectors as taxonomy_selectors

        # Only offer alternatives when there are some. With no near match this
        # read "We don't have 'Kafka' in our skill list yet. Did you mean: ?",
        # which is a broken sentence and tells the recruiter nothing about what
        # to do next. The term is recorded either way, so the answer is that
        # somebody will look at it.
        near = [skill.name for skill in taxonomy_selectors.search_skills(text, limit=5)]
        message = f"We don't have {text!r} in our skill list yet."
        message += (
            f" Did you mean: {', '.join(near)}?"
            if near
            else " It has been noted for review. List the closest skill you can for now."
        )
        raise InvalidState(message)

    return _attach_skill(
        job=job,
        actor=actor,
        match=match,
        requirement=requirement,
        weight=weight,
        min_years=min_years,
    )


@transaction.atomic
def _attach_skill(
    *,
    job: Job,
    actor: User,
    match: Any,
    requirement: str,
    weight: Any,
    min_years: Any,
) -> JobSkill:
    if job.skills.filter(skill=match.skill).exists():
        raise Conflict(f"{match.skill.name} is already on this posting.")

    record = JobSkill.objects.create(
        job=job,
        skill=match.skill,
        requirement=requirement,
        weight=_clean_weight(weight),
        min_years=_clean_years(min_years),
        order=job.skills.count(),
    )
    audit.record(
        verb="job.skill_added",
        actor=actor,
        target=job,
        company=job.company,
        metadata={"skill": match.skill.slug, "requirement": requirement},
    )
    return record


@transaction.atomic
def remove_skill(*, job: Job, actor: User, skill_id: int) -> None:
    record = job.skills.select_related("skill").filter(skill_id=skill_id).first()
    if record is None:
        raise NotFound("That skill is not on this posting.")

    slug = record.skill.slug
    record.delete()
    audit.record(
        verb="job.skill_removed",
        actor=actor,
        target=job,
        company=job.company,
        metadata={"skill": slug},
    )


# --- screening questions ---------------------------------------------------


@transaction.atomic
def add_question(
    *,
    job: Job,
    actor: User,
    text: str,
    kind: str = QuestionKind.SHORT_TEXT,
    options: list[str] | None = None,
    is_required: bool = True,
    knockout: dict | None = None,
) -> JobScreeningQuestion:
    if kind not in QuestionKind.values:
        raise InvalidState("That is not a kind of question.")
    if not (text or "").strip():
        raise InvalidState("Write the question first.")
    if job.questions.count() >= MAX_QUESTIONS:
        raise InvalidState(f"A posting asks up to {MAX_QUESTIONS} questions.")

    cleaned_options = [
        str(option).strip()[:120] for option in (options or []) if str(option).strip()
    ]
    if kind in {QuestionKind.SINGLE_CHOICE, QuestionKind.MULTI_CHOICE} and len(cleaned_options) < 2:
        raise InvalidState("A choice question needs at least two options.")

    question = JobScreeningQuestion.objects.create(
        job=job,
        order=job.questions.count(),
        kind=kind,
        text=text.strip()[:300],
        options=cleaned_options,
        is_required=bool(is_required),
        knockout=knockout or None,
    )
    audit.record(verb="job.question_added", actor=actor, target=job, company=job.company)
    return question


@transaction.atomic
def remove_question(*, job: Job, actor: User, question_id: int) -> None:
    question = job.questions.filter(pk=question_id).first()
    if question is None:
        raise NotFound("That question is not on this posting.")

    question.delete()
    audit.record(verb="job.question_removed", actor=actor, target=job, company=job.company)


# --- assignment -----------------------------------------------------------


@transaction.atomic
def assign(
    *,
    job: Job,
    actor: User,
    membership: CompanyMembership,
    kind: str = JobAssignment.Kind.RECRUITER,
) -> JobAssignment:
    """Put someone on a posting (SRS §2.3).

    Assignment is what gives a recruiter write access to a job when they do not
    hold `job.write_any` — which is the default, so that a five-person company
    is usable without granting everybody everything.
    """
    if membership.company_id != job.company_id:
        # Cross-tenant assignment would hand another company's member write
        # access to this posting.
        raise NotFound("That person is not on this company's team.")
    if not membership.is_active:
        raise InvalidState("That team member is no longer active.")
    if kind not in JobAssignment.Kind.values:
        raise InvalidState("That is not a kind of assignment.")

    record, created = JobAssignment.objects.get_or_create(job=job, membership=membership, kind=kind)
    if created:
        audit.record(
            verb="job.assigned",
            actor=actor,
            target=job,
            company=job.company,
            metadata={"member": membership.user_id, "kind": kind},
        )
    return record


@transaction.atomic
def unassign(*, job: Job, actor: User, membership: CompanyMembership, kind: str) -> None:
    removed, _ = job.assignments.filter(membership=membership, kind=kind).delete()
    if removed:
        audit.record(
            verb="job.unassigned",
            actor=actor,
            target=job,
            company=job.company,
            metadata={"member": membership.user_id, "kind": kind},
        )


# --- internals ------------------------------------------------------------


def _clean(fields: dict[str, Any], *, title: str | None = None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if title is not None:
        clean["title"] = _require_text(title, "a title", limit=160)

    for key, value in fields.items():
        if key not in EDITABLE:
            continue

        if key == "title":
            clean[key] = _require_text(value, "a title", limit=160)
        elif key in {"responsibilities", "requirements", "benefits", "remote_countries"}:
            clean[key] = _clean_list(value, key)
        elif key in {"salary_min", "salary_max"}:
            clean[key] = _clean_amount(value)
        elif key in {"salary_visible"}:
            clean[key] = value in (True, "on", "true", "True", "1", "yes")
        elif key == "application_cooldown_days":
            clean[key] = _clean_cooldown(value)
        elif key in {"category_id", "location_id"}:
            clean[key] = _clean_reference(key, value)
        elif isinstance(value, str):
            clean[key] = value.strip()
        else:
            clean[key] = value

    _validate(clean)
    return clean


def _validate(clean: dict[str, Any]) -> None:
    checks = {
        "workplace": Workplace.values,
        "remote_scope": RemoteScope.values,
        "employment_type": EmploymentType.values,
        "experience_level": ExperienceLevel.values,
        "apply_mode": ApplyMode.values,
    }
    for field, allowed in checks.items():
        if field in clean and clean[field] not in allowed:
            raise InvalidState(f"{clean[field]!r} is not a valid {field.replace('_', ' ')}.")

    low, high = clean.get("salary_min"), clean.get("salary_max")
    if low is not None and high is not None and high < low:
        raise InvalidState("The top of the salary range cannot be below the bottom.")

    if clean.get("currency"):
        clean["currency"] = clean["currency"].upper()[:3]

    # The check constraint would reject this anyway; saying so in words is what
    # the employer can act on.
    if clean.get("workplace") == Workplace.REMOTE and clean.get("remote_scope") in (
        None,
        RemoteScope.NONE,
    ):
        raise InvalidState("Say where a remote hire may work from.")


def _require_text(value: Any, label: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidState(f"Give the posting {label}.")
    return text[:limit]


def _clean_list(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        # A textarea arrives as one string; the model stores a list of lines.
        value = value.split("\n")
    items = [str(item).strip()[:300] for item in (value or []) if str(item).strip()]
    if len(items) > MAX_LIST_ITEMS:
        raise InvalidState(f"{field.capitalize()} holds up to {MAX_LIST_ITEMS} lines.")
    return items


def _clean_amount(value: Any) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise InvalidState("Type a salary as a number, without symbols.") from None
    if amount < 0:
        raise InvalidState("A salary cannot be negative.")
    if amount >= Decimal("10000000000"):
        raise InvalidState("That salary is larger than this field can hold.")
    return amount.quantize(Decimal("0.01"))


def _clean_cooldown(value: Any) -> int:
    try:
        days = int(str(value or 0).strip() or 0)
    except ValueError:
        raise InvalidState("A cooldown is a number of days.") from None
    if days < 0 or days > 365:
        raise InvalidState("A cooldown is between 0 and 365 days.")
    return days


def _clean_weight(value: Any) -> Decimal:
    if value in (None, "", "None"):
        return Decimal("1.00")
    try:
        weight = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InvalidState("A weight is a number between 0 and 1.") from None
    if weight < 0 or weight > 1:
        raise InvalidState("A weight is between 0 and 1.")
    return weight.quantize(Decimal("0.01"))


def _clean_years(value: Any) -> Decimal | None:
    if value in (None, "", "None"):
        return None
    try:
        years = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InvalidState("Minimum years is a number.") from None
    if years < 0 or years > 60:
        raise InvalidState("Minimum years is between 0 and 60.")
    return years.quantize(Decimal("0.1"))


def _clean_reference(key: str, value: Any) -> int | None:
    if value in (None, "", "None", 0, "0"):
        return None

    model = JobCategory if key == "category_id" else Location
    try:
        identifier = int(value)
    except (TypeError, ValueError):
        raise InvalidState("That is not something we know.") from None

    if not model.objects.filter(pk=identifier).exists():
        raise InvalidState(
            "That category is not one we know."
            if key == "category_id"
            else "That location is not one we know."
        )
    return identifier


def _normalise_salary(job: Job) -> None:
    """Annualise into the base currency, keeping the rate that did it (ADR-A39).

    A missing FX rate does not refuse the posting — the employer's own figures
    are what candidates read — but comparison is cleared rather than left
    holding a number produced by some earlier rate.
    """
    if not job.currency or not job.period or (job.salary_min is None and job.salary_max is None):
        _clear_base(job)
        return

    try:
        low = money.normalise_salary(
            amount=job.salary_min, currency=job.currency, period=job.period
        )
        high = money.normalise_salary(
            amount=job.salary_max, currency=job.currency, period=job.period
        )
    except InvalidState as exc:
        logger.warning("salary not normalised for job %s: %s", job.public_id, exc.message)
        _clear_base(job)
        return

    reference = low or high
    if reference is None:  # pragma: no cover - both amounts were None
        _clear_base(job)
        return

    job.salary_min_base = low.amount if low else None
    job.salary_max_base = high.amount if high else None
    job.fx_rate_id = reference.fx_rate_id
    job.fx_rate_value = reference.fx_rate_value
    job.fx_as_of = reference.fx_as_of
    job.fx_source = reference.fx_source


def _clear_base(job: Job) -> None:
    job.salary_min_base = None
    job.salary_max_base = None
    job.fx_rate_id = None
    job.fx_rate_value = None
    job.fx_as_of = None
    job.fx_source = ""


def _unique_slug(company: Company, title: str) -> str:
    """A stable public identifier, prefixed by the company.

    Two companies hiring a "Backend Engineer" must not fight over one URL, and
    the prefix makes the link readable rather than a hash.
    """
    base = f"{company.slug}-{slugify(title)}"[:110].strip("-") or f"{company.slug}-role"
    slug, suffix = base, 2

    while Job.objects.filter(slug=slug).exists():
        tail = f"-{suffix}"
        slug = f"{base[: 120 - len(tail)]}{tail}"
        suffix += 1
        if suffix > _SLUG_MAX_ATTEMPTS:
            from apps.common.uuid7 import uuid7

            return f"{base[:100]}-{str(uuid7())[:8]}"
    return slug
