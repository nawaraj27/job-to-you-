"""
Talent search (FR-9.1, FR-9.2, SRS §10.6).

**A distinct subsystem, not a reversed recommendation.** The candidate feed
scores a person against a posting and has an application to learn from; this has
neither. An employer typing "Django, Kathmandu, available soon" is asking a
question about the corpus, not about one job, so what ranks here is skills,
experience, recency of activity and stated availability — the four things §10.6
names, and nothing else.

That is why this module does not import `apps.matching`. Reusing the match
scorer would mean either inventing a job to score against or scoring against
nothing, and both produce a number that looks like a match score and is not one.
The two subsystems share the taxonomy and the profile, which is the right amount
of sharing.

**Nothing here decides who may be seen.** The queryset this module filters is
handed to it by `services.talent`, which has already applied the visibility and
grant rules. Search that also enforced authorization would be a second place for
the rule to live, and the second place is where it drifts.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import (
    Case,
    Count,
    Exists,
    F,
    FloatField,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.expressions import Combinable
from django.db.models.functions import Coalesce, Greatest, Least
from django.utils import timezone

from .models import CandidateProfile, Seeking

#: How many results one page holds.
PAGE_SIZE = 20

#: Hard ceiling on a single search. An employer who needs more than this is
#: exporting, which is a different act with a different grant (FR-9.4).
MAX_RESULTS = 200

#: What a skill-name hit is worth on the text signal. `SearchRank` over a short
#: headline lands around 0.05–0.1, so this puts "has the skill you typed" above
#: "mentioned the word once in a sentence", which is the order a recruiter
#: means when they type one word into the box.
SKILL_NAME_RANK = 0.3

#: What each ranking signal is worth. They sum to 1.0 and a test says so.
#:
#: Skills dominate because it is the only signal the employer actually asked
#: about; the rest break ties in the direction that helps. Availability is
#: weighted above recency deliberately — someone who says they are looking is a
#: better lead than someone who merely logged in.
WEIGHTS: dict[str, float] = {
    "skills": 0.55,
    "text": 0.15,
    "experience": 0.12,
    "availability": 0.10,
    "recency": 0.08,
}

#: Seeking states, mapped to what they are worth. "Not looking" is not zero:
#: the candidate is still findable if their visibility permits, and a nought
#: would bury them below empty profiles.
AVAILABILITY: dict[str, float] = {
    Seeking.ACTIVE: 1.0,
    Seeking.OPEN: 0.7,
    Seeking.NOT_LOOKING: 0.2,
    "": 0.5,
}

#: Days after which a profile stops counting as recently maintained. Ninety,
#: because a quarter is roughly how long a job search runs.
RECENCY_DAYS = 90


@dataclass(frozen=True)
class TalentQuery:
    """What an employer asked for.

    Frozen and built from a mapping, so a saved search or a URL can be turned
    into one without the search service knowing about either.
    """

    text: str = ""
    skill_ids: tuple[int, ...] = ()
    #: Skills as typed in the form ("Python, Django"), and those of them the
    #: taxonomy could not resolve, so the screen can say which were ignored.
    skill_names: tuple[str, ...] = ()
    unknown_skills: tuple[str, ...] = ()
    location_id: int | None = None
    remote_ok: bool = False
    min_experience_months: int = 0
    seeking: str = ""
    max_salary_base: Decimal | None = None
    sort: str = "relevance"

    @classmethod
    def from_mapping(cls, data: Any) -> TalentQuery:
        ids = list(
            _as_ints(data.getlist("skill") if hasattr(data, "getlist") else data.get("skill"))
        )
        names, unknown = _skill_names(data.get("skills"))
        for skill_id in names.values():
            if skill_id not in ids:
                ids.append(skill_id)
        return cls(
            text=str(data.get("q", "") or "").strip()[:200],
            skill_ids=tuple(ids),
            skill_names=tuple(names),
            unknown_skills=tuple(unknown),
            location_id=_as_int(data.get("location")),
            remote_ok=str(data.get("remote", "")).lower() in {"1", "true", "on", "yes"},
            # Clamped: `min_months=-1` counted as "asked for something" and
            # returned the whole corpus past the empty-search refusal.
            min_experience_months=max(_as_int(data.get("min_months")) or 0, 0),
            seeking=_as_choice(data.get("seeking"), Seeking),
            max_salary_base=_as_decimal(data.get("max_salary")),
            sort=str(data.get("sort", "relevance") or "relevance"),
        )

    @property
    def is_empty(self) -> bool:
        """Whether this asks for anything at all.

        An empty query is refused rather than answered with the whole corpus:
        "show me every candidate" is not a search, it is a download, and it is
        the shape a scraper's first request takes.
        """
        return not any(
            (
                self.text,
                self.skill_ids,
                self.location_id,
                self.remote_ok,
                self.min_experience_months,
                self.seeking,
                self.max_salary_base,
            )
        )


#: How many typed skill names one search resolves.
MAX_SKILL_NAMES = 10


def _skill_names(raw: Any) -> tuple[dict[str, int], list[str]]:
    """Typed skill names resolved through the taxonomy (FR-9.2 "filter by skills").

    The query always supported skill ids, but the form had no way to send one,
    so an employer could only type skills into the free-text box. Names are
    resolved with the same resolver postings use, aliases included ("JS").
    """
    from apps.taxonomy.services import skills as skill_resolver

    resolved: dict[str, int] = {}
    unknown: list[str] = []
    for part in str(raw or "").split(",")[:MAX_SKILL_NAMES]:
        name = " ".join(part.split())[:80]
        if not name:
            continue
        match = skill_resolver.resolve(name)
        if match is None:
            unknown.append(name)
        else:
            resolved[match.skill.name] = match.skill.pk
    return resolved, unknown


@dataclass
class TalentResult:
    profiles: list[CandidateProfile] = field(default_factory=list)
    total: int = 0
    used_trigram: bool = False


def search(query: TalentQuery, *, base: QuerySet[CandidateProfile]) -> TalentResult:
    """Rank `base` against this query.

    `base` is required and has no default, deliberately: there is no safe
    default queryset for this search. A caller that forgot to scope would
    otherwise get every profile in the database, and the mistake would look like
    working code.
    """
    queryset = _apply_filters(base, query)
    queryset = _annotate(queryset, query)
    queryset = _order(queryset, query.sort)

    total = queryset.count()
    profiles = list(
        queryset.select_related("location", "user").prefetch_related("skills__skill")[:MAX_RESULTS]
    )
    return TalentResult(profiles=profiles, total=total, used_trigram=False)


# --- filters --------------------------------------------------------------


def _apply_filters(
    queryset: QuerySet[CandidateProfile], query: TalentQuery
) -> QuerySet[CandidateProfile]:
    """Hard filters. Everything here is a fact the employer stated."""
    if query.skill_ids:
        # `distinct` because a candidate with three of the wanted skills would
        # otherwise appear three times — the classic join fan-out, and the one
        # that makes a result count wrong rather than merely ugly.
        queryset = queryset.filter(skills__skill_id__in=query.skill_ids).distinct()

    if query.location_id is not None:
        # Remote-willing candidates match any location: an employer filtering
        # by city is saying where the work is, not where the person must sleep.
        location = Q(location_id=query.location_id)
        if query.remote_ok:
            location |= Q(open_to__contains=["remote"])
        queryset = queryset.filter(location)
    elif query.remote_ok:
        queryset = queryset.filter(open_to__contains=["remote"])

    if query.min_experience_months:
        queryset = queryset.filter(experience_months__gte=query.min_experience_months)

    if query.seeking:
        queryset = queryset.filter(seeking=query.seeking)

    if query.max_salary_base is not None:
        # A candidate who stated nothing is not excluded. Most have not, and
        # filtering them out would hide the majority of the corpus to enforce a
        # ceiling they never contradicted.
        queryset = queryset.filter(
            Q(salary_min_base__isnull=True) | Q(salary_min_base__lte=query.max_salary_base)
        )

    if query.text:
        queryset = queryset.filter(_text_matches(query.text))

    return queryset


#: Shortest search term that may match skills by substring (decision D-05).
#: Below it, "a" matched every candidate with an "a" in any skill name.
SUBSTRING_MIN_LENGTH = 3


def is_short_term(text: str) -> bool:
    return 0 < len(text) < SUBSTRING_MIN_LENGTH


def is_known_short_skill(text: str) -> bool:
    """Whether a short term is exactly a skill name or alias: R, C, Go, UI."""
    from apps.taxonomy.models import Skill, SkillAlias

    return (
        Skill.objects.filter(name__iexact=text).exists()
        or SkillAlias.objects.filter(alias__iexact=text).exists()
    )


def _text_matches(text: str) -> Q:
    """What the free-text box matches (decision D-05).

    Three characters or more: the words of the headline and summary, or any
    skill whose name or alias contains the text. Shorter: only a skill whose
    name or alias *is* the text — so "R", "C", "Go" and "UI" still find the
    people with those skills — and, for two characters, the word itself in the
    headline or summary. A one-character word is not searched in prose at all.

    Skills count as words on the profile. `search_vector` is a generated
    column over the headline and the summary, because skills live in a related
    table and a generated column cannot reach one; without the skill branch a
    recruiter typing "Python" matched nobody while candidates with Python on
    their profile sat in the results below. `EXISTS` rather than a join,
    because a join fans out on a candidate with several matching skills, and
    `distinct()` cannot repair that once the ranking joins the table again.
    """
    if not is_short_term(text):
        return Q(search_vector=SearchQuery(text, config="simple")) | Q(Exists(_skill_named(text)))
    match = Q(Exists(_skill_named(text, exact=True)))
    if len(text) > 1:
        match |= Q(search_vector=SearchQuery(text, config="simple"))
    return match


def _skill_named(text: str, *, exact: bool = False):
    """This candidate's skills, restricted to ones called `text`.

    Correlated on `profile_id` so it can be used with `Exists` in both the
    filter and the ranking without joining the table into the outer query.
    `exact` compares the whole name or alias, for terms too short to match by
    substring (D-05).
    """
    from .models import CandidateSkill

    lookup = "iexact" if exact else "icontains"
    return CandidateSkill.objects.filter(
        Q(**{f"skill__name__{lookup}": text}) | Q(**{f"skill__aliases__alias__{lookup}": text}),
        profile_id=OuterRef("pk"),
    )


# --- ranking --------------------------------------------------------------


def _annotate(
    queryset: QuerySet[CandidateProfile], query: TalentQuery
) -> QuerySet[CandidateProfile]:
    """Attach the four ranking signals and their weighted total.

    Computed in the database rather than in Python because the ordering has to
    survive `LIMIT`: ranking a page in Python ranks the page, not the corpus,
    and the second page would be drawn from a different ordering than the first.
    """
    cutoff = timezone.now() - dt.timedelta(days=RECENCY_DAYS)

    wanted = max(len(query.skill_ids), 1)

    skill_score: Combinable
    if query.skill_ids:
        skill_score = Coalesce(
            Count("skills", filter=Q(skills__skill_id__in=query.skill_ids), distinct=True)
            * (1.0 / wanted),
            Value(0.0),
            output_field=FloatField(),
        )
    else:
        # No skills asked for means no skill signal to rank on. One rather than
        # zero, so a query filtered only by location does not rank every
        # candidate identically at the bottom.
        skill_score = Value(1.0, output_field=FloatField())

    # A skill-name hit ranks as a strong text match. Without this the
    # candidates the widened filter just admitted would all rank at zero on
    # text and sort below anybody whose summary happened to mention the word in
    # passing — which is the wrong way round.
    text_score: Combinable
    if query.text:
        text_score = Greatest(
            SearchRank(F("search_vector"), SearchQuery(query.text, config="simple")),
            Case(
                When(
                    Exists(_skill_named(query.text, exact=is_short_term(query.text))),
                    then=Value(SKILL_NAME_RANK),
                ),
                default=Value(0.0),
                output_field=FloatField(),
            ),
            output_field=FloatField(),
        )
    else:
        text_score = Value(0.0, output_field=FloatField())

    queryset = queryset.annotate(
        skill_score=skill_score,
        text_score=text_score,
        # Real months of work, from the derived column the profile service
        # maintains — not a proxy. Capped at ten years so a thirty-year career
        # does not outrank a well-matched candidate on length alone; past a
        # decade, more is not more relevant.
        experience_score=Least(
            Coalesce(F("experience_months") / 120.0, Value(0.0), output_field=FloatField()),
            Value(1.0, output_field=FloatField()),
        ),
        # These two are CASE expressions rather than post-hoc Python because the
        # *ordering* needs them in SQL: ranking a page in Python ranks the page.
        availability_score=_availability_case(),
        recency_score=_recency_case(cutoff),
    )

    return queryset.annotate(
        relevance=(
            F("skill_score") * WEIGHTS["skills"]
            + F("text_score") * WEIGHTS["text"]
            + F("experience_score") * WEIGHTS["experience"]
            + F("availability_score") * WEIGHTS["availability"]
            + F("recency_score") * WEIGHTS["recency"]
        )
    )


def _availability_case() -> Case:
    return Case(
        *[
            When(seeking=value, then=Value(score, output_field=FloatField()))
            for value, score in AVAILABILITY.items()
            if value
        ],
        default=Value(AVAILABILITY[""], output_field=FloatField()),
        output_field=FloatField(),
    )


def _recency_case(cutoff: Any) -> Case:
    return Case(
        When(updated_at__gte=cutoff, then=Value(1.0, output_field=FloatField())),
        default=Value(0.3, output_field=FloatField()),
        output_field=FloatField(),
    )


def _order(queryset: QuerySet[CandidateProfile], sort: str) -> QuerySet[CandidateProfile]:
    """Order, always ending with a deterministic tiebreaker.

    `(-relevance, -id)` and never a bare `-relevance`: two equally ranked
    candidates would otherwise swap places between pages, and the employer would
    see one twice while never seeing the other (SRS §10.5).
    """
    if sort == "recent":
        return queryset.order_by("-updated_at", "-id")
    if sort == "experience":
        # `relevance` is an annotation this module added, so mypy's model-field
        # resolution cannot see it. The alternative is not annotating, which
        # would move ranking into Python and rank the page rather than the
        # corpus.
        return queryset.order_by("-experience_months", F("relevance").desc(), "-id")
    return queryset.order_by(F("relevance").desc(), "-id")


# --- parsing --------------------------------------------------------------


def _as_ints(value: Any) -> list[int]:
    if value is None:
        return []
    values = value if isinstance(value, list | tuple) else [value]
    found = []
    for item in values:
        parsed = _as_int(item)
        if parsed is not None:
            found.append(parsed)
    return found


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001 - any unparseable value is simply absent
        return None
    # "NaN", "Infinity" and negatives parse, and none is a salary.
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _as_choice(value: Any, choices) -> str:
    text = str(value or "").strip()
    return text if text in choices.values else ""
