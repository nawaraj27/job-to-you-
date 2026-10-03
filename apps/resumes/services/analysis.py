"""
Resume analysis (FR-4.1 to FR-4.9, SRS §5.5.2).

The shape of the whole feature, and the reason it is this shape:

    finalize a version (reusing one if nothing changed)
        -> deterministic score, ATS checks, facts, findings   [always]
        -> ai enrichment: verdict, headline, judgement findings [when it works]
        -> one ResumeAnalysis row with everything on it

**The deterministic half runs first and never depends on the second.** A report
is produced when AI is switched off, when the provider is down, when the budget
is spent and when the model returns nonsense — degraded to its deterministic
parts rather than to an error, which is what SRS §9.3.6 asks of every AI
feature. The model adds prose and judgement; it cannot move the score (ADR-A6).

Three invariants this module is responsible for:

  * **It never mutates a resume.** Analysis reads a frozen version and writes
    its own rows. A fix reaches the draft only as a `ResumeSuggestion` the
    candidate accepts (INV-3).
  * **Every AI call goes through `ai.services.runner.run`** (P11). No provider,
    no model, no retry, no cost lives here.
  * **Contact details are stripped before dispatch** (SRS §9.3.7). A model
    analysing writing has no need of a phone number, and the cheapest way to
    keep it out of a provider's logs is not to send it.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.ai.models import AIRun, RunStatus
from apps.ai.services import runner
from apps.audit import services as audit
from apps.common.errors import InvalidState, NotFound
from apps.notifications import events

from .. import analysis as engine
from ..analysis import target as target_rules
from ..models import (
    CheckStatus,
    EnrichmentState,
    FindingSeverity,
    ResumeAnalysis,
    ResumeAtsCheck,
    ResumeDocument,
    ResumeFinding,
    ResumeSuggestion,
    ResumeVersion,
    VersionReason,
)
from . import versions as version_service

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.jobs.models import Job

#: How many judgement findings to keep from one model call. A report with
#: thirty items is a report nobody acts on, and the deterministic findings are
#: already on the page.
MAX_AI_FINDINGS = 6

logger = logging.getLogger(__name__)

#: How much CV text to send. Long enough for any real CV, short enough that a
#: pathological document cannot run the output token bill up on its own.
MAX_INPUT_CHARS = 12_000


@transaction.atomic
def analyse(
    *,
    document: ResumeDocument,
    actor: User,
    target_role: str = "",
    advert: str = "",
    job: Job | None = None,
    use_ai: bool = True,
    defer_ai: bool = False,
) -> ResumeAnalysis:
    """Analyse a CV, freezing a version first (FR-4.1).

    The version comes from `versions.finalize`, which reuses the current one
    when the content has not changed — so ten clicks on Analyse produce ten
    reports against one version rather than ten identical immutable rows.

    The plan's monthly allowance is **claimed atomically before** the work and
    released if the work fails. Checking then consuming afterwards was the
    original shape and lost a race that a double-click wins; claiming first
    and refunding on failure keeps both properties — nobody gets two analyses
    from one allowance, and nobody pays for the platform's own outage.
    """
    from apps.billing.services import entitlements

    profile = document.profile

    # Claimed atomically *before* the work, released if the work fails.
    # `require` then `consume` was the obvious shape and the wrong one: two
    # requests arriving together — a double-click is enough — both passed the
    # read, and a free account got two analyses. The claim is one locked
    # increment; the release is what keeps a provider outage from costing the
    # candidate their monthly allowance.
    entitlements.claim(profile, "resume_analyses")
    try:
        version = version_service.finalize(
            document=document, actor=actor, reason=VersionReason.ANALYSED
        )
        return analyse_version(
            version=version,
            actor=actor,
            target_role=target_role,
            advert=advert,
            job=job,
            use_ai=use_ai,
            defer_ai=defer_ai,
        )
    except Exception:
        entitlements.release(profile, "resume_analyses")
        raise


@transaction.atomic
def analyse_version(
    *,
    version: ResumeVersion,
    actor: User | None,
    target_role: str = "",
    advert: str = "",
    job: Job | None = None,
    use_ai: bool = True,
    defer_ai: bool = False,
) -> ResumeAnalysis:
    """Produce one report against one frozen version.

    `defer_ai` writes the deterministic report now and leaves the model's half
    to `enrich`, queued by the caller. That is what every screen does: the
    score and the checks are instant, and a ninety-second wait for the prose
    is a wait nobody sits through.

    Split from `analyse` so a re-run against an existing version — a retry after
    a provider outage, or a second opinion against a different job — cannot
    create a version as a side effect.
    """
    if version.is_redacted:
        raise InvalidState("That version was erased and can no longer be analysed.")

    # The erasure gate (SRS §19.4). The AI half is stopped by the runner, but
    # the deterministic half writes findings that quote resume lines verbatim,
    # so it needs its own check: a report produced after an erasure has walked
    # past this table would recreate exactly what was removed.
    from apps.accounts.services import erasure

    if erasure.is_erasing(version.document.profile):
        raise InvalidState("This account is being erased.")

    content = version.content
    today = timezone.localdate()
    upload = version.document.upload

    parse_warnings, filename = _upload_context(upload)
    checks = engine.ats_checks(
        content, parse_warnings=parse_warnings, filename=filename, today=today
    )
    # What this CV is being measured against, and how it does against it.
    # Deterministic: the advert is read by the taxonomy, not by a model, so the
    # matched and missing lists are reproducible and explicable term by term.
    aim = _target(job=job, advert=advert, target_role=target_role)
    covered = target_rules.coverage(aim, _text_of(content))

    scored = engine.score_version(
        content,
        ats_results=checks,
        target_skills=aim.skills,
        parse_warnings=parse_warnings,
        filename=filename,
        today=today,
    )
    findings = engine.deterministic_findings(content, ats_results=checks, today=today)

    run_row: AIRun | None = None
    if use_ai and defer_ai:
        # Deterministic now, prose later. `enrich` fills in the rest.
        prose: dict[str, str] = {}
        enrichment = EnrichmentState.PENDING
    elif use_ai:
        enrichment = EnrichmentState.DONE
        run_row, ai_findings, prose = _enrich(
            content=content,
            actor=actor,
            target_role=target_role or aim.role,
            job=job,
            missing=covered["missing"] + covered["missing_keywords"],
        )
        findings.extend(ai_findings)
        if run_row is None or run_row.status != RunStatus.OK:
            enrichment = EnrichmentState.FAILED
    else:
        prose = {}
        enrichment = EnrichmentState.SKIPPED

    analysis = ResumeAnalysis.objects.create(
        version=version,
        target_role=target_role.strip()[:120],
        job=job,
        score=scored["score"],
        breakdown={"components": scored["components"], "weights": engine.WEIGHTS},
        verdict=prose.get("verdict", "") or _fallback_verdict(scored["score"]),
        headline=prose.get("headline", "")[:200],
        priority=[finding.code for finding in findings if finding.severity != "strength"][:5],
        experience=engine.experience_summary(content, today=today),
        education=engine.education_summary(content),
        readability=engine.readability(content),
        skill_evidence=engine.skill_evidence(content),
        enrichment=enrichment,
        target=covered if aim.source != "none" else {},
        advert=(advert or "")[: target_rules.MAX_ADVERT_CHARS],
        ai_run=run_row,
        algorithm_version=scored["algorithm_version"],
    )

    ResumeFinding.objects.bulk_create(
        [
            ResumeFinding(
                analysis=analysis,
                severity=finding.severity,
                code=finding.code[:60],
                title=finding.title[:160],
                text=finding.text,
                evidence=finding.evidence,
                action=finding.action[:300],
                section_ref=finding.section_ref[:200],
                order=order,
                from_ai=finding.from_ai,
            )
            for order, finding in enumerate(findings)
        ]
    )
    ResumeAtsCheck.objects.bulk_create(
        [
            ResumeAtsCheck(
                analysis=analysis,
                key=check.key[:60],
                label=check.label[:160],
                status=check.status,
                note=check.note[:300],
                order=check.order,
            )
            for check in checks
        ]
    )

    events.emit(
        "resume.analysed",
        analysis,
        payload={
            "document_public_id": str(version.document.public_id),
            "version_no": version.version_no,
            "score": analysis.score,
            "algorithm_version": analysis.algorithm_version,
            # Counts only. Findings quote the CV, and the outbox has a
            # different retention from the analysis (DEVELOPER.md §2.13).
            "findings": len(findings),
            "used_ai": run_row is not None and run_row.status == RunStatus.OK,
        },
    )
    audit.record(
        verb="resume.analysed",
        actor=actor,
        target=analysis,
        metadata={
            "document": str(version.document.public_id),
            "version_no": version.version_no,
            "score": analysis.score,
            "job": str(job.public_id) if job is not None else "",
        },
    )
    return analysis


@transaction.atomic
def retry_enrichment(*, analysis: ResumeAnalysis, actor: User) -> ResumeAnalysis:
    """Ask for the written review again, on a report that already exists.

    A failed review is a provider outage, not a decision the candidate made,
    and it must not cost them anything. The only way to try again used to be
    "Analyse this CV", which claims a monthly allowance — so on a free plan,
    one timeout on the platform's side spent the candidate's single analysis
    for the month and they had to wait until the next one to see any prose at
    all. The score and the checks were never affected, which made it worse: the
    thing they lost the allowance for was the half that had already worked.

    No version is frozen and no allowance is claimed. `enrich` is idempotent by
    state, so re-opening the report here is the whole of the retry, and a
    report that has since succeeded is returned untouched rather than run a
    second time.
    """
    if analysis.enrichment == EnrichmentState.DONE:
        return analysis
    if analysis.enrichment == EnrichmentState.PENDING:
        raise InvalidState("That review is still being written.")

    analysis.enrichment = EnrichmentState.PENDING
    analysis.save(update_fields=["enrichment", "updated_at"])
    return enrich(analysis=analysis, actor=actor)


def enrich(*, analysis: ResumeAnalysis, actor: User | None = None) -> ResumeAnalysis:
    """Add the model's half to a report that was written without it.

    The second half of the split `defer_ai` starts: the verdict, the headline
    and the judgement findings, appended to a report whose score, checks and
    measurable findings are already on the candidate's screen.

    **The number never moves** (ADR-A6). This writes prose and findings and
    nothing else; the score was final the moment the deterministic pass ran,
    which is what makes it reproducible and what makes this safe to run late.

    Idempotent by state: a report already enriched is returned untouched, so a
    retried task cannot double the findings.
    """
    if analysis.enrichment != EnrichmentState.PENDING:
        return analysis

    version = analysis.version
    if version.is_redacted:
        analysis.enrichment = EnrichmentState.FAILED
        analysis.save(update_fields=["enrichment", "updated_at"])
        return analysis

    from apps.accounts.services import erasure

    if erasure.is_erasing(version.document.profile):
        analysis.enrichment = EnrichmentState.FAILED
        analysis.save(update_fields=["enrichment", "updated_at"])
        return analysis

    covered = analysis.target or {}
    run_row, ai_findings, prose = _enrich(
        content=version.content,
        actor=actor,
        target_role=analysis.target_role or covered.get("role", ""),
        job=analysis.job,
        missing=list(covered.get("missing") or []) + list(covered.get("missing_keywords") or []),
    )

    if run_row is None or run_row.status != RunStatus.OK:
        # Only a report still waiting is marked failed: a duplicate task that
        # failed must not turn a review another task already wrote into
        # "could not be produced".
        ResumeAnalysis.objects.filter(pk=analysis.pk, enrichment=EnrichmentState.PENDING).update(
            enrichment=EnrichmentState.FAILED, updated_at=timezone.now()
        )
        analysis.refresh_from_db()
        return analysis

    with transaction.atomic():
        # Re-read under a row lock before writing (ENR-01). The state check at
        # the top ran before a model call that takes minutes; a second task
        # for the same report — a broker redelivery, the free "try again"
        # pressed while the first was still running — passed the same check,
        # and both appended their findings. The later one now finds the
        # report already written and leaves it alone.
        locked = ResumeAnalysis.objects.select_for_update().get(pk=analysis.pk)
        if locked.enrichment != EnrichmentState.PENDING:
            analysis.refresh_from_db()
            return analysis
        _write_enrichment(analysis, run_row, ai_findings, prose, actor)
    return analysis


def _write_enrichment(
    analysis: ResumeAnalysis,
    run_row: AIRun,
    ai_findings: list[Any],
    prose: dict[str, str],
    actor: User | None,
) -> None:
    # Appended after the deterministic findings, keeping their order stable:
    # the measured problems are the ones to fix first, and a model's opinion
    # arriving late must not push them down the page.
    highest = analysis.findings.aggregate(top=Max("order"))["top"] or 0
    ResumeFinding.objects.bulk_create(
        [
            ResumeFinding(
                analysis=analysis,
                severity=finding.severity,
                code=finding.code[:60],
                title=finding.title[:160],
                text=finding.text,
                evidence=finding.evidence,
                action=finding.action[:300],
                section_ref=finding.section_ref[:200],
                order=highest + position + 1,
                from_ai=True,
            )
            for position, finding in enumerate(ai_findings)
        ]
    )

    analysis.verdict = prose.get("verdict", "") or analysis.verdict
    analysis.headline = prose.get("headline", "")[:200] or analysis.headline
    analysis.ai_run = run_row
    analysis.enrichment = EnrichmentState.DONE
    analysis.save(update_fields=["verdict", "headline", "ai_run", "enrichment", "updated_at"])

    audit.record(
        verb="resume.analysis_enriched",
        actor=actor,
        target=analysis,
        metadata={"findings": len(ai_findings)},
    )


def apply_finding(*, finding: ResumeFinding, actor: User, use_ai: bool = True) -> ResumeSuggestion:
    """Turn a finding into a suggestion the candidate can accept (FR-4.9).

    Never writes the draft. It writes a `ResumeSuggestion`, which is the only
    route into a document (INV-3) — the same route the reconcile protocol uses,
    and the reason "AI edited my CV" cannot happen here.

    Only for the two shapes where a model can honestly help: rewriting an
    entry's bullets and drafting a summary. `ResumeFinding.is_fixable` is the
    same rule, so the button and this service agree about what can be drafted.

    There used to be a fallback: anything else became a suggestion carrying the
    finding's own action text, described as "a prompt the candidate writes
    over". It was not one. Accepting it wrote the advice into the document at
    the finding's target — a skills list of ten entries replaced by the
    sentence "Name one of them in a bullet where you used it, or drop it."
    """
    if not finding.is_fixable:
        raise InvalidState("There is nothing to apply for that finding.")

    document = finding.analysis.version.document
    if use_ai:
        # One CV improvement draft from the plan's allowance (ADR-A59),
        # refunded if nothing usable comes back.
        from apps.billing.services import entitlements

        entitlements.claim(document.profile, "cv_improvements")
    try:
        return draft_rewrite(
            document=document,
            section_ref=finding.section_ref,
            problem=finding.title,
            actor=actor,
            target_role=finding.analysis.target_role or "",
            finding=finding,
            use_ai=use_ai,
        )
    except Exception:
        if use_ai:
            entitlements.release(document.profile, "cv_improvements")
        raise


def draft_rewrite(
    *,
    document: ResumeDocument,
    section_ref: str,
    problem: str,
    actor: User | None,
    target_role: str = "",
    finding: ResumeFinding | None = None,
    use_ai: bool = True,
) -> ResumeSuggestion:
    """Draft a rewording of words already on the CV, as a suggestion.

    The one drafting path, shared by "Draft a fix" on the report and by an
    improvement plan's safe rewordings (`services.improvements`). Only the two
    shapes where a model can honestly help — an entry's bullets, one bullet at
    a time, and a summary drawn from the rest of the CV — and never a write:
    the result is a `ResumeSuggestion` the candidate accepts or rejects.
    """
    from . import suggestions as suggestion_service

    draft = document.draft
    path = section_ref

    original, proposed, run_row = "", "", None
    if use_ai and path.startswith("entry.") and path.endswith(".bullets"):
        original, proposed, run_row = _rewrite_bullets(
            content=draft.content, path=path, actor=actor, problem=problem
        )
    elif use_ai and path == "summary":
        original, proposed, run_row = _draft_summary(
            content=draft.content, actor=actor, target_role=target_role
        )

    if not proposed.strip():
        # Reached when the model declined or returned nothing. Saying so is the
        # whole of the honesty here: the alternative is a suggestion that looks
        # like a rewrite and is not one.
        raise InvalidState(
            "Nothing could be drafted for that one. The note above is the "
            "change to make, and it is yours to write."
        )

    return suggestion_service.offer(
        draft=draft,
        target_path=path,
        proposed=proposed,
        original=original,
        source="finding",
        rationale=problem,
        ai_run=run_row,
        finding=finding,
    )


def history_for(document: ResumeDocument, *, limit: int = 20) -> list[dict[str, Any]]:
    """Score history across analyses (FR-4.8).

    Oldest first, because it is drawn as a line and a line that runs backwards
    tells the opposite story. Analyses from a different `algorithm_version` are
    marked rather than dropped: hiding them would silently break the history,
    and comparing them without saying so would be worse.
    """
    rows = (
        ResumeAnalysis.objects.filter(version__document=document)
        .select_related("version")
        .order_by("created_at")[:limit]
    )
    current = engine.ALGORITHM_VERSION
    return [
        {
            "public_id": str(row.public_id),
            "at": row.created_at,
            "version_no": row.version.version_no,
            "score": row.score,
            "comparable": row.algorithm_version == current,
        }
        for row in rows
    ]


# --- AI, all of it through the P11 runner ---------------------------------


def _enrich(
    *,
    content: dict[str, Any],
    actor: User | None,
    target_role: str,
    job: Job | None,
    missing: list[str] | None = None,
) -> tuple[AIRun | None, list[engine.Finding], dict[str, str]]:
    """Ask the model for judgement findings and prose.

    Returns what it got, or nothing at all. Every failure path — switched off,
    out of budget, provider down, schema error — lands here as "no findings",
    because the deterministic report is already complete and a half-written
    report is worse than an honest one.
    """
    cv_text = _redacted_text(content)
    run_row = runner.run(
        task_key="resume.analysis",
        inputs={
            "cv_text": cv_text,
            "target_role": target_role or (job.title if job is not None else ""),
            # The measured gaps, so the model reviews *against the advert*
            # rather than in the abstract. Capped: a hundred missing terms is a
            # prompt that costs more than the answer is worth.
            "gaps": ", ".join((missing or [])[:20]) or "not stated",
        },
        user=actor,
        input_ref=None,
    )
    if run_row.status != RunStatus.OK:
        return run_row, [], {}

    output = run_row.output or {}
    findings = []
    for item in (output.get("findings") or [])[:MAX_AI_FINDINGS]:
        severity = _severity(str(item.get("severity", "")))
        # Model-quoted evidence is candidate text coming back out of a
        # provider. Capped here and escaped on render (SRS §9.7).
        evidence = str(item.get("evidence", ""))[:300]
        if evidence and not _quotes_the_cv(evidence, cv_text):
            # The finding says "your CV says X" about words the CV does not
            # contain. The prompt asks for a quote and a small model sometimes
            # writes a plausible one instead, which is the one thing a review
            # of somebody's own document must never do — they read it, do not
            # recognise it, and stop believing the rest of the report.
            logger.info("dropping an AI finding whose quote is not on the CV")
            continue
        findings.append(
            engine.Finding(
                severity=severity,
                code=str(item.get("code") or "ai_observation")[:60],
                title=str(item.get("title", ""))[:160],
                text=str(item.get("detail", "")),
                evidence=evidence,
                action=str(item.get("action", ""))[:300],
                section_ref="",
                from_ai=True,
            )
        )

    prose = {
        "verdict": str(output.get("verdict", ""))[:2_000],
        "headline": str(output.get("headline", ""))[:200],
    }
    return run_row, [finding for finding in findings if finding.title], prose


def _rewrite_bullets(
    *, content: dict[str, Any], path: str, actor: User | None, problem: str
) -> tuple[str, str, AIRun | None]:
    """Ask for tighter wording for one entry's bullets, one bullet at a time.

    One call per bullet rather than one call carrying all of them. The prompt
    asked for "one line per original line" and a small local model returned one
    line for three: a rewrite that read well, quietly dropped two of the
    candidate's bullets, and gave no sign it had. Losing a role's content is the
    one outcome this feature must not have.

    Asking per bullet makes that shape impossible rather than detectable — the
    unit the task is named for is one bullet, and the answer to "one bullet in"
    is either one bullet out or nothing. Anything that comes back as several
    lines is discarded, and any bullet the model fails or mangles keeps its
    original wording, so a partial answer is still worth showing.

    Returns `("", "", run)` when no line changed, and the caller says so rather
    than proposing something else.
    """
    from .. import content as content_rules

    entry_id = path.split(".")[1]
    located = content_rules.entry_by_id(content, entry_id)
    if located is None:
        return "", "", None

    _, entry = located
    bullets = [line for line in (entry.get("bullets") or []) if line.strip()]
    if not bullets:
        return "", "", None

    role = f"{entry.get('title', '')} at {entry.get('org', '')}".strip()
    rewritten: list[str] = []
    last_run: AIRun | None = None
    changed = False

    for bullet in bullets:
        run_row = runner.run(
            task_key="resume.bullet_rewrite",
            inputs={"bullet": bullet, "role": role, "problem": problem},
            user=actor,
        )
        last_run = run_row or last_run
        line = _one_line_option(run_row)
        rewritten.append(line or bullet)
        changed = changed or bool(line and line != bullet)

    if not changed:
        return "", "", last_run

    return "\n".join(bullets), "\n".join(rewritten), last_run


def _one_line_option(run_row: AIRun | None) -> str:
    """The first option that is one bullet, or nothing.

    A multi-line answer to a one-bullet question is the model doing something
    other than what was asked, and a bullet list is not the place to find out
    what.
    """
    if run_row is None or run_row.status != RunStatus.OK:
        return ""

    for option in (run_row.output or {}).get("options") or []:
        text = str(option.get("text", "")).strip()
        lines = _lines_of(text)
        if len(lines) == 1:
            return lines[0].lstrip("-•* ").strip()
    return ""


def _lines_of(text: str) -> list[str]:
    """Non-empty lines, which is what a bullet list is made of."""
    return [line for line in (text or "").splitlines() if line.strip()]


def _draft_summary(
    *, content: dict[str, Any], actor: User | None, target_role: str = ""
) -> tuple[str, str, AIRun | None]:
    """Draft a summary from the rest of the CV.

    The one place a model writes a paragraph from scratch, and it is still a
    suggestion: it lands in `ResumeSuggestion` at `summary`, and the candidate
    accepts it or does not. The input is the redacted CV — a summary written
    from someone's own experience, never from their phone number.
    """
    original = _summary_of(content)
    run_row = runner.run(
        task_key="resume.summary",
        inputs={
            "cv_text": _redacted_text(content),
            "target_role": target_role,
        },
        user=actor,
    )
    if run_row.status != RunStatus.OK:
        return "", "", run_row

    options = (run_row.output or {}).get("options") or []
    proposed = str(options[0].get("text", "")).strip() if options else ""
    return (original, proposed, run_row) if proposed else ("", "", run_row)


def _summary_of(content: dict[str, Any]) -> str:
    from ..analysis import text as reader

    return reader.summary_text(content)


#: How much of a quote has to be on the page for it to be a quote.
#:
#: Not an exact match. A model normalises as it quotes — it drops a bullet
#: marker, straightens a dash, trims a trailing full stop — and refusing those
#: would throw away findings that are perfectly well grounded. It is a quote
#: when the CV contains it, or when nearly all of its substantial words are
#: there.
QUOTE_OVERLAP = 0.8
_QUOTE_NOISE = re.compile(r"[^\w\s]+")
_QUOTE_SPACE = re.compile(r"\s+")


def _quotes_the_cv(evidence: str, cv_text: str) -> bool:
    """Whether these words are actually on the candidate's CV."""
    quote = _QUOTE_SPACE.sub(" ", _QUOTE_NOISE.sub(" ", evidence)).strip().casefold()
    body = _QUOTE_SPACE.sub(" ", _QUOTE_NOISE.sub(" ", cv_text)).strip().casefold()
    if not quote:
        return True
    if quote in body:
        return True

    words = [word for word in quote.split() if len(word) > 3]
    if not words:
        # Too short to check, and too short to mislead anybody either.
        return True
    present = sum(1 for word in words if word in body)
    return present / len(words) >= QUOTE_OVERLAP


def _redacted_text(content: dict[str, Any]) -> str:
    """The CV as text, with contact details removed (SRS §9.3.7).

    A model judging writing has no use for a phone number, and the cheapest way
    to keep one out of a provider's logs is not to send it.
    """
    from ..analysis import text as reader

    parts: list[str] = []
    summary = reader.summary_text(content)
    if summary:
        parts.append(f"SUMMARY\n{summary}")

    for section in sorted(content.get("sections", []), key=lambda s: s.get("order", 0)):
        if not section.get("visible", True) or section.get("key") == "summary":
            continue
        body: list[str] = []
        for entry in section.get("entries", []) or []:
            head = " — ".join(
                part for part in (entry.get("title"), entry.get("org"), entry.get("dates")) if part
            )
            body.append(head)
            body.extend(f"  - {bullet}" for bullet in entry.get("bullets", []) or [])
        body.extend(f"  - {item}" for item in section.get("items", []) or [])
        if section.get("text"):
            body.append(str(section["text"]))
        if body:
            parts.append(str(section.get("title", "")).upper() + "\n" + "\n".join(body))

    return "\n\n".join(parts)[:MAX_INPUT_CHARS]


def _severity(value: str) -> str:
    """Map whatever the model said onto the vocabulary the report renders.

    The prompt asks for these three words. This exists because a schema can
    require an enum and a model can still be given a new prompt version by
    somebody who forgets — and an unmapped severity would break the report
    rather than one finding.
    """
    lowered = value.strip().lower()
    if lowered in {FindingSeverity.CRITICAL, "high", "critical"}:
        return FindingSeverity.CRITICAL
    if lowered in {FindingSeverity.STRENGTH, "strength", "good"}:
        return FindingSeverity.STRENGTH
    return FindingSeverity.IMPROVE


# --- helpers --------------------------------------------------------------


def _upload_context(upload: Any) -> tuple[list[str], str]:
    """Parse warnings and the original file name, when the CV came from a file.

    A CV written in the builder has neither, and the checks that read them say
    so rather than inventing a problem.
    """
    if upload is None:
        return [], ""
    parse = getattr(upload, "parse", None)
    warnings = list(parse.warnings) if parse is not None else []
    return [str(warning) for warning in warnings], upload.original_filename or ""


def _target(*, job: Job | None, advert: str, target_role: str) -> target_rules.Target:
    """What the candidate said they are aiming at, in order of specificity.

    A posting on the platform beats a pasted advert, because its skills are
    already curated rather than read off prose; a pasted advert beats a typed
    role name, because a role name carries no skills at all.
    """
    if job is not None:
        return target_rules.from_job(job)
    return target_rules.from_advert(advert, role=target_role)


def _text_of(content: dict) -> str:
    """The whole CV as one lower-cased string, for phrase matching (TGT-01)."""
    from ..analysis import text as text_rules

    return text_rules.all_text(content).lower()


def _fallback_verdict(score: int) -> str:
    """What the report says when no model spoke.

    Written from the score, which is a fact. Not "analysis unavailable": the
    analysis is right there, and only the prose is missing.
    """
    if score >= 80:
        return "This CV is in good shape. The findings below are refinements rather than repairs."
    if score >= 60:
        return "This CV works, and a few specific changes would make it noticeably stronger."
    if score >= 40:
        return "There is a real CV here, but a recruiter has to work to find what you have done."
    return "This CV is not yet doing its job. Start with the items marked “Fix this”."


def find_analysis(*, document: ResumeDocument, public_id: str) -> ResumeAnalysis:
    analysis = (
        ResumeAnalysis.objects.filter(version__document=document, public_id=public_id)
        .select_related("version", "ai_run")
        .first()
    )
    if analysis is None:
        raise NotFound("That report does not exist, or you cannot see it.")
    return analysis


def latest_for(document: ResumeDocument) -> ResumeAnalysis | None:
    return (
        ResumeAnalysis.objects.filter(version__document=document)
        .select_related("version", "ai_run")
        .prefetch_related("findings", "checks")
        .first()
    )


__all__ = [
    "CheckStatus",
    "analyse",
    "analyse_version",
    "apply_finding",
    "find_analysis",
    "history_for",
    "latest_for",
]
