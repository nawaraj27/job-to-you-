"""
What a job advert is asking for.

The product's central promise to a candidate is "tailor this CV to that
advert", and until now the interface asked for a *role name* — one line of free
text — while the scorer could only compare against a `Job` row the platform
happened to own. A candidate applying to a role they found on a company's own
site had nothing to tailor against, which is most of the applications most
people make.

So this module reads the advert itself. Two kinds of thing come out of it:

  * **Skills**, resolved through the taxonomy. "JS", "Javascript" and
    "JavaScript" are one skill, and the comparison against the CV is made
    between canonical names rather than between spellings. That is the same
    resolver a recruiter's own posting goes through, so a candidate's coverage
    figure means the same thing on both sides of the market.
  * **Keywords**, which is everything else the advert leans on — a tool, a
    certification, a domain word. Not resolved, not scored, just surfaced,
    because an applicant tracking system searching for "PCI-DSS" does not care
    that the taxonomy has never heard of it.

**Nothing here calls a model.** Extraction is a lookup against a table the
platform already curates, so it is reproducible, explicable line by line, and
free. The model's job in this feature is to rewrite bullets, not to decide what
the advert said.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

#: The advert is read up to here. Long enough for any real posting, short
#: enough that somebody pasting a whole careers page does not cost a scan of
#: fifty thousand words.
MAX_ADVERT_CHARS = 20_000

#: How many keywords to surface. A list of forty is a list nobody reads, and
#: the ones that matter are the ones the advert repeats.
MAX_KEYWORDS = 14

#: Words that appear in every advert and mean nothing on a CV. Deliberately
#: short: the frequency filter does most of the work, and a long stop list is
#: a long list of judgements nobody can audit.
STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at back be because been
    before being below between both but by can cannot could did do does doing down during
    each few for from further had has have having he her here hers him his how i if in into
    is it its itself just me more most my no nor not now of off on once only or other our
    out over own same she should so some such than that the their them then there these they
    this those through to too under until up very was we were what when where which while who
    whom why will with would you your yours
    role job work working team teams company opportunity position candidate candidates
    experience experienced years year join looking seeking apply application applicants
    responsibilities requirements benefits salary please send email contact us we our
    ability able strong good great excellent proven track record understanding knowledge
    skills skill including include included etc across within using use used help helping
    ensure ensuring support supporting deliver delivering build building work works
    new well high level levels day days month months full time part remote hybrid office
    """.split()  # noqa: SIM905 - a block of prose is the readable way to keep a stop
    # list; a list literal of a hundred quoted words is not
)

#: A word worth counting. Two characters is enough for "Go" and "R"; anything
#: shorter is noise, and the `+`, `#` and `.` keep "C++", "C#" and ".NET".
WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,}")

#: A line that reads like a job title. Adverts almost always open with one.
TITLE_HINT = re.compile(
    r"\b(engineer|developer|manager|analyst|designer|consultant|accountant|nurse|teacher|"
    r"scientist|architect|administrator|officer|specialist|director|lead|head|intern|"
    r"assistant|executive|coordinator|technician|researcher|advisor|associate|partner)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Target:
    """What the CV is being measured against."""

    #: Where this came from, so the report can say so: a posting on the
    #: platform, an advert the candidate pasted, or a role they typed.
    source: str = "none"

    #: The role, as named by the posting or read off the advert's first lines.
    role: str = ""

    #: Canonical skill names, resolved through the taxonomy.
    skills: list[str] = field(default_factory=list)

    #: Terms the advert leans on that are not skills the platform knows.
    keywords: list[str] = field(default_factory=list)

    #: The skills the target *requires*. A posting on the platform says which
    #: of its skills are required and which preferred; a pasted advert does
    #: not, so every skill it names is treated as required. Readiness is
    #: judged on these (CVR-01): a missing preferred skill is advice, not a
    #: reason to call the CV incomplete. `None` means "all of `skills`".
    required: list[str] | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.skills or self.keywords)


def from_advert(text: str, *, role: str = "") -> Target:
    """Read one pasted job advert."""
    body = (text or "")[:MAX_ADVERT_CHARS]
    if not body.strip():
        return Target(source="role", role=role.strip()[:120]) if role.strip() else Target()

    skills = _skills_in(body)
    return Target(
        source="advert",
        role=(role.strip() or _role_from(body))[:120],
        skills=skills,
        keywords=_keywords_in(body, exclude=skills, role=role.strip() or _role_from(body)),
    )


def from_job(job) -> Target:
    """A posting the platform owns. Its skills are already canonical."""
    from apps.jobs.models import Requirement

    rows = [row for row in job.skills.select_related("skill").all() if row.skill_id]
    return Target(
        source="job",
        role=job.title,
        skills=[row.skill.name for row in rows],
        keywords=[],
        required=[row.skill.name for row in rows if row.requirement == Requirement.REQUIRED],
    )


def coverage(target: Target, present: str | set[str]) -> dict:
    """Which of the target's asks the CV already answers.

    `present` is the CV's whole text, lower-cased. A skill counts as present
    when its canonical name, or one of its taxonomy aliases, appears as a
    phrase — with the same boundary rule the advert reader uses, so "REST
    APIs", "UX research" and "Content marketing" are found (TGT-01).

    This used to compare against the set of *single words* in the CV, so a
    skill of two words could never be present: a marketing CV that said
    "content marketing" in every bullet was told it lacked Content marketing,
    and its readiness could never be Complete. A set of words is still
    accepted, for callers that only have one; a multi-word skill then counts
    when every one of its words is there.
    """
    wanted = {name.lower(): name for name in target.skills}
    matched = sorted(wanted[key] for key in wanted if _mentions(wanted[key], present))
    missing = sorted(wanted[key] for key in wanted if wanted[key] not in matched)
    asked = target.skills if target.required is None else target.required
    required = {name.lower() for name in asked}

    missing_keywords = sorted(
        keyword for keyword in target.keywords if not _mentions(keyword, present)
    )

    return {
        "role": target.role,
        "source": target.source,
        "matched": matched,
        "missing": missing,
        # The part of `missing` the target requires; readiness reads this.
        "missing_required": [name for name in missing if name.lower() in required],
        "missing_keywords": missing_keywords[:MAX_KEYWORDS],
        "wanted": len(wanted),
        "percent": round(len(matched) / len(wanted) * 100) if wanted else None,
    }


def _mentions(skill: str, present: str | set[str]) -> bool:
    """Whether the CV mentions this skill, by name or by a taxonomy alias."""
    if isinstance(present, set):
        words = skill.lower().split()
        return skill.lower() in present or bool(words) and all(word in present for word in words)
    return any(pattern.search(present) for pattern in _patterns_for(skill))


def _patterns_for(skill: str) -> list[re.Pattern[str]]:
    """The skill's own name and every alias the vocabulary maps onto it."""
    patterns = [pattern for pattern, canonical in _vocabulary() if canonical == skill]
    if not patterns:
        # A skill the vocabulary does not know (retired since, or typed into a
        # pasted advert's role): its own name, with the same boundaries.
        patterns = [re.compile(rf"(?<![\w+#]){re.escape(skill.lower())}(?![\w+#])")]
    return patterns


# --- extraction -----------------------------------------------------------


def _skills_in(text: str) -> list[str]:
    """Every skill the taxonomy recognises in this advert.

    Matched against the whole vocabulary at once — one pass over a compiled
    alternation — rather than by asking the resolver about each n-gram. A
    500-word advert has around 1,500 one-to-three word windows, and a database
    round trip per window is a request that takes seconds to answer a question
    a regular expression answers in microseconds.
    """
    lowered = text.lower()
    found: list[str] = []
    for pattern, canonical in _vocabulary():
        if pattern.search(lowered):
            found.append(canonical)
    return sorted(set(found))


@lru_cache(maxsize=1)
def _vocabulary() -> tuple[tuple[re.Pattern[str], str], ...]:
    """Every skill name and alias, as word-boundary patterns.

    Cached for the process: the taxonomy changes when staff edit it, which is
    rare, and `reset_vocabulary()` exists for the code that does the editing.
    """
    from apps.taxonomy.models import Skill, SkillAlias, SkillStatus

    entries: dict[str, str] = {}
    active = Skill.objects.filter(status=SkillStatus.ACTIVE)
    for skill in active.only("id", "name"):
        entries[skill.name.lower()] = skill.name

    # A retired skill's aliases go with it: an advert mentioning "AngularJS"
    # should not credit a CV with a skill the taxonomy has withdrawn.
    aliases = SkillAlias.objects.filter(skill__status=SkillStatus.ACTIVE).select_related("skill")
    for alias in aliases.only("alias", "skill__name"):
        entries.setdefault(alias.alias.lower(), alias.skill.name)

    compiled = []
    for text, canonical in entries.items():
        if len(text) < 2:
            continue
        # `\b` does not fire next to "+" or "#", so "C++" and "C#" need the
        # boundary spelled out as "not a word character" on each side.
        compiled.append((re.compile(rf"(?<![\w+#]){re.escape(text)}(?![\w+#])"), canonical))
    return tuple(compiled)


def reset_vocabulary() -> None:
    """Forget the cached skill vocabulary. For staff edits and for tests."""
    _vocabulary.cache_clear()


def _keywords_in(text: str, *, exclude: list[str], role: str = "") -> list[str]:
    """The terms the advert leans on.

    Two ways in, because two different things matter:

      * **Repetition.** An advert that says "reconciliation" five times is an
        advert about reconciliation, and no vocabulary maintained here would
        ever have contained that word.
      * **Acronyms and standards.** "PCI-DSS", "GDPR", "IFRS", "SOC2" appear
        once and are the single term a keyword search is most likely to run.
        Requiring a second mention would drop exactly the words that matter.

    The role's own words are excluded. "Senior", "Backend" and "Engineer" are
    in the title of the advert and in the title on the CV, so reporting them as
    missing keywords is reporting a gap that does not exist.
    """
    skipped = {name.lower() for name in exclude}
    skipped |= {word.lower().strip(".,-—:") for word in role.split()}

    counts: Counter[str] = Counter()
    casing: dict[str, str] = {}
    standards: list[str] = []

    for match in WORD.finditer(text):
        word = match.group(0).strip(".-")
        key = word.lower()
        if len(key) < 3 or key in STOPWORDS or key in skipped or key.isdigit():
            continue

        counts[key] += 1
        # Keep the advert's own capitalisation for anything it writes as a
        # proper noun or an acronym: "PCI-DSS" is not "pci-dss".
        if word[:1].isupper() and key not in casing:
            casing[key] = word
        if _looks_like_a_standard(word) and key not in {item.lower() for item in standards}:
            standards.append(word)

    repeated = [
        casing.get(key, key) for key, count in counts.most_common(MAX_KEYWORDS * 3) if count >= 2
    ]

    ordered: list[str] = []
    for word in [*standards, *repeated]:
        if word.lower() not in {item.lower() for item in ordered}:
            ordered.append(word)
    return ordered[:MAX_KEYWORDS]


def _looks_like_a_standard(word: str) -> bool:
    """An acronym or a named standard, which counts even said once.

    All upper case, or upper case around a hyphen or a digit — which is what
    "PCI-DSS", "SOC2", "ISO27001" and "GDPR" all look like, and what ordinary
    prose does not.
    """
    letters = [character for character in word if character.isalpha()]
    return len(word) >= 3 and bool(letters) and all(character.isupper() for character in letters)


def _role_from(text: str) -> str:
    """The advert's own job title, off its first few lines."""
    for line in text.splitlines()[:8]:
        line = line.strip(" #*-—:")
        if 2 <= len(line.split()) <= 10 and TITLE_HINT.search(line):
            return line
    return ""
