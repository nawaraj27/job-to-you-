"""
From "what we found" to "what to do about it".

The analysis report lists what is weak about a CV. Improving it happens on
other screens — guided, assisted — and those screens used to start from
nothing: the candidate read "none of these bullets carry a number" on the
report, pressed Improve, and had to remember what the report had said. This
module carries the findings across. It is the one place that decides, for each
finding:

  * **what was detected** and **why it matters** — the finding's own words,
    plus one sentence of consequence;
  * **which part of the CV it affects**, resolved to the section and entry
    names the candidate sees (not a path);
  * **what the candidate can add**, as the concrete questions to answer;
  * **what kind of change it is** — the distinction everything downstream
    depends on:

      - `REWORD` — the words are already on the page and only the phrasing is
        weak (a summary in stock phrases, bullets written as duties). A draft
        can be offered, because rephrasing what the CV says invents nothing.
      - `NEEDS_INPUT` — the fix is a fact the platform does not hold: the
        result of a piece of work, a missing phone number, what a project
        did. These become questions. Nothing is drafted until the candidate
        answers, and then only from what they said.
      - `EDIT` — a fix the candidate makes in place (a date format, a job
        title), with a link to the field.

It reads; it never writes. Drafting and asking run through
`services.improvements`, and every change still reaches the CV only through an
accepted suggestion (INV-3).

**Where the findings come from.** The latest analysis of this CV, because that
is the report the candidate read. Each finding is then checked against the
current draft with the same deterministic rules, so one that has since been
fixed is shown as done rather than nagging. With no analysis yet, the same
deterministic rules run on the draft directly — nothing is spent, and the
candidate is told that this is a quick check rather than their report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.utils import timezone

from . import content as content_rules
from .analysis import checks as ats
from .analysis import findings as finding_rules
from .models import FindingSeverity, ResumeDocument, SuggestionState

REWORD, NEEDS_INPUT, EDIT = "reword", "needs_input", "edit"
OPEN, ADDRESSED, DRAFTED = "open", "addressed", "drafted"


@dataclass(frozen=True)
class Guidance:
    kind: str
    why: str
    asks: tuple[str, ...] = ()


#: Per finding code: what kind of change it is, why it matters, and what to
#: ask. The finding's own text says *what* was detected; this says what a
#: candidate can do about it. A code missing here falls back to the finding's
#: action as the one thing to add, and is treated as needing their input —
#: the safe default, since nothing is drafted for it.
GUIDANCE: dict[str, Guidance] = {
    "contact_incomplete": Guidance(
        NEEDS_INPUT,
        "A shortlisted CV with no way to reach its author is a CV nobody calls.",
        ("The phone number or email address an employer should use.",),
    ),
    "no_summary": Guidance(
        REWORD,
        "The summary is the first thing read; without one a recruiter guesses from your latest role.",
        ("What you do, and for how long.", "What kind of role you want next."),
    ),
    "thin_summary": Guidance(
        REWORD,
        "A one-line summary gives a recruiter nothing to shortlist on.",
        ("What you do, and for how long.", "What kind of role you want next."),
    ),
    "generic_summary": Guidance(
        REWORD,
        "Stock phrases are on most CVs, so a reader skips them and learns nothing about you.",
        ("What you actually do, and for whom.", "One thing you are known for at work."),
    ),
    "duty_language": Guidance(
        REWORD,
        "Duties describe the job; recruiters shortlist on what you did in it.",
        ("What you did yourself, in active verbs.",),
    ),
    "no_outcomes": Guidance(
        NEEDS_INPUT,
        "A result is the difference between what you were asked to do and what happened. "
        "Only you know it, so it is asked, never guessed.",
        (
            "What did you build, change or fix yourself?",
            "Which tools or methods did you use?",
            "What was the result? Faster, cheaper, more accurate, more people served?",
            "Roughly how much or how many, if you remember? Leave it out if you do not.",
        ),
    ),
    "thin_entry": Guidance(
        NEEDS_INPUT,
        "A role with no description tells a reader you had the job and nothing about how you did it.",
        (
            "What were you responsible for day to day?",
            "What did you build, run or improve?",
            "What came of it?",
        ),
    ),
    "thin_project": Guidance(
        NEEDS_INPUT,
        "A project is evidence only when it says what you did in it.",
        (
            "What did you build, and what does it do?",
            "Which tools or languages did you use?",
            "Who used it, or what did it achieve?",
        ),
    ),
    "untitled_role": Guidance(
        EDIT,
        "A role that cannot be placed may be filed under the wrong heading or dropped.",
        ("The exact job title and employer, as on your contract or payslip.",),
    ),
    "unreadable_dates": Guidance(
        EDIT,
        "An applicant tracking system that cannot read a date can drop the whole role.",
        ("Start and end month and year, in one format, for example “Mar 2021 – Present”.",),
    ),
    "no_skills": Guidance(
        NEEDS_INPUT,
        "Employer searches read the skills list; a CV without one does not come up.",
        ("The tools, languages and methods you actually use at work.",),
    ),
    "unevidenced_skills": Guidance(
        NEEDS_INPUT,
        "An interviewer asks about exactly the skills you list but never show.",
        (
            "Where did you use each of these: which job or project?",
            "What did you do with it there?",
        ),
    ),
    "employment_gap": Guidance(
        NEEDS_INPUT,
        "A gap is not a problem; an unexplained one invites the reader to invent a reason.",
        ("What were you doing then? Study, contract work, caring, travel?",),
    ),
    "no_education": Guidance(
        NEEDS_INPUT,
        "Many applications are screened on education as well as experience.",
        ("Your highest qualification, where you studied, and the years, if you have one.",),
    ),
    "no_experience": Guidance(
        NEEDS_INPUT,
        "A reader needs something to assess: a job, a course, a project.",
        ("Whatever you have done: a job, a course, a project, volunteering.",),
    ),
    "no_paid_experience": Guidance(
        NEEDS_INPUT,
        "Without a job, your projects and study do the work a role would.",
        ("For each project or course: what you built or ran, and what came of it.",),
    ),
}

#: Where an answer to a question lands, per code: an experience entry's
#: bullets, or a contact field the candidate fills in themselves.
ANSWERABLE_IN_CONVERSATION = frozenset(
    {"no_outcomes", "thin_entry", "thin_project", "unevidenced_skills", "employment_gap"}
)
CONTACT_FIELDS = ("email", "phone")


@dataclass
class PlanItem:
    key: str
    code: str
    severity: str
    title: str
    detected: str
    why: str
    evidence: str
    action: str
    asks: tuple[str, ...]
    kind: str
    section_ref: str
    affects: str
    anchor: str = ""
    entry_id: str = ""
    contact_field: str = ""
    status: str = OPEN
    from_ai: bool = False
    finding_id: int | None = None
    suggestions: list[Any] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status != ADDRESSED

    @property
    def can_answer(self) -> bool:
        return self.kind == NEEDS_INPUT and self.code in ANSWERABLE_IN_CONVERSATION

    @property
    def can_fill_contact(self) -> bool:
        return bool(self.contact_field)

    @property
    def can_draft(self) -> bool:
        return self.kind == REWORD and self.status == OPEN

    @property
    def first_question(self) -> str:
        return self.asks[0] if self.asks else self.action


@dataclass
class Plan:
    items: list[PlanItem]
    #: "analysis" when the items come from the candidate's report; "live" when
    #: there is no report and the deterministic rules ran on the draft.
    source: str
    analysis: Any = None
    strengths: list[str] = field(default_factory=list)

    @property
    def open_items(self) -> list[PlanItem]:
        return [item for item in self.items if item.is_open]

    @property
    def reword_items(self) -> list[PlanItem]:
        return [item for item in self.open_items if item.kind == REWORD]

    @property
    def input_items(self) -> list[PlanItem]:
        return [item for item in self.open_items if item.kind != REWORD]

    @property
    def draftable(self) -> list[PlanItem]:
        return [item for item in self.items if item.can_draft]

    @property
    def addressed_count(self) -> int:
        return sum(1 for item in self.items if item.status == ADDRESSED)

    @property
    def proposed(self) -> list[Any]:
        """Drafted changes waiting for a decision, in plan order, once each."""
        seen: set[int] = set()
        rows = []
        for item in self.items:
            for suggestion in item.suggestions:
                if suggestion.pk not in seen:
                    seen.add(suggestion.pk)
                    rows.append(suggestion)
        return rows

    @property
    def is_empty(self) -> bool:
        return not self.items


#: "Not passed" for `plan_for(analysis=...)`, which may legitimately be None.
_UNSET: Any = object()


def plan_for(
    document: ResumeDocument, *, analysis: Any = _UNSET, offered: list[Any] | None = None
) -> Plan:
    """What is worth improving on this CV, and how.

    `analysis` (with its findings prefetched) and `offered` (the draft's open
    suggestions) may be passed by a screen that has already read them: the
    builder screens sit under NFR-2's query ceiling, and reading either twice
    is what breaks it.
    """
    content = document.draft.content
    live = _live_findings(content)
    live_keys = {(finding.code, finding.section_ref) for finding in live}
    live_summary = any(finding.section_ref == "summary" for finding in live)

    if analysis is _UNSET:
        analysis, rows = _latest_findings(document)
    else:
        rows = list(analysis.findings.all()) if analysis is not None else []
    #: Saved `ResumeFinding` rows, or live `Finding` dataclasses: `_item` reads
    #: the attributes both share.
    source_rows: list[Any]
    if analysis is not None:
        source_rows = [row for row in rows if row.severity != FindingSeverity.STRENGTH]
        strengths = [row.title for row in rows if row.severity == FindingSeverity.STRENGTH]
        source = "analysis"
    else:
        source_rows = [finding for finding in live if finding.severity != finding_rules.STRENGTH]
        strengths = [
            finding.title for finding in live if finding.severity == finding_rules.STRENGTH
        ]
        source = "live"

    # Every pending suggestion, whoever drafted it: an answer to a finding is
    # drafted by the assistant into the entry the finding is about, and that
    # draft is what the point is waiting on.
    if offered is None:
        offered = list(document.draft.suggestions.filter(state=SuggestionState.OFFERED))
    else:
        offered = [row for row in offered if row.state == SuggestionState.OFFERED]

    items: list[PlanItem] = []
    for row in source_rows:
        item = _item(row, content)
        # Checked against the draft as it is now, with the same rules the
        # report used. A summary finding can change under the candidate — a
        # thin summary rewritten into a generic one is a different code — so a
        # summary point stays open while any summary finding still applies.
        still_there = (item.code, item.section_ref) in live_keys or (
            item.section_ref == "summary" and live_summary
        )
        if not item.from_ai and not still_there:
            item.status = ADDRESSED
        item.suggestions = [suggestion for suggestion in offered if _answers(item, suggestion)]
        if item.status == OPEN and item.suggestions:
            item.status = DRAFTED
        items.append(item)

    order: dict[str, int] = {"critical": 0, "improve": 1}
    items.sort(key=lambda item: (item.status == ADDRESSED, order.get(str(item.severity), 2)))
    return Plan(items=items, source=source, analysis=analysis, strengths=strengths)


def _latest_findings(document: ResumeDocument) -> tuple[Any, list[Any]]:
    """The newest analysis of this CV and its findings, in one query.

    The findings are read with their analysis joined on, so a report with
    findings costs one query rather than three (`latest_analysis_for` also
    prefetches the checks, which the plan never reads).
    """
    from django.db.models import Subquery

    from .models import ResumeAnalysis, ResumeFinding

    newest = (
        ResumeAnalysis.objects.filter(version__document=document)
        .order_by("-created_at", "-pk")
        .values("pk")[:1]
    )
    rows = list(
        ResumeFinding.objects.filter(analysis_id=Subquery(newest))
        .select_related("analysis__version", "analysis__job")
        .order_by("order", "pk")
    )
    if rows:
        return rows[0].analysis, rows
    # No findings at all — no analysis, or (in practice never) a report with
    # nothing to say, not even a strength. Either way the live check below is
    # the right thing to show, and not asking again keeps the builder screens
    # under NFR-2's query ceiling.
    return None, []


def _answers(item: PlanItem, suggestion: Any) -> bool:
    """Whether a pending suggestion is a draft for this point."""
    if item.finding_id and suggestion.finding_id == item.finding_id:
        return True
    if suggestion.source == "finding" and suggestion.target_path == item.section_ref:
        return True
    # An answer drafted into the entry the point is about.
    return bool(
        item.entry_id
        and item.can_answer
        and suggestion.target_path.startswith(f"entry.{item.entry_id}.")
    )


def item_by_key(plan: Plan, key: str) -> PlanItem | None:
    return next((item for item in plan.items if item.key == key), None)


def _live_findings(content: dict[str, Any]) -> list[finding_rules.Finding]:
    today = timezone.localdate()
    results = ats.ats_checks(content, today=today)
    return finding_rules.deterministic_findings(content, ats_results=results, today=today)


def _item(row: Any, content: dict[str, Any]) -> PlanItem:
    """One finding — a saved `ResumeFinding` or a live `Finding` — as a plan item."""
    guidance = GUIDANCE.get(row.code) or Guidance(
        NEEDS_INPUT, "", ((row.action,) if row.action else ())
    )
    ref = row.section_ref or ""
    affects, anchor, entry_id = _locate(ref, row.code, content)
    contact_field = ""
    if ref.startswith("contact."):
        wanted = ref.split(".", 1)[1]
        current = str((content.get("contact") or {}).get(wanted, "") or "").strip()
        if wanted in CONTACT_FIELDS and not current:
            contact_field = wanted
    finding_id = getattr(row, "pk", None)
    return PlanItem(
        key=f"{row.code}:{ref}",
        code=row.code,
        severity=row.severity,
        title=row.title,
        detected=row.text,
        why=guidance.why,
        evidence=(row.evidence or "")[:200],
        action=row.action,
        asks=guidance.asks or ((row.action,) if row.action else ()),
        kind=guidance.kind,
        section_ref=ref,
        affects=affects,
        anchor=anchor,
        entry_id=entry_id,
        contact_field=contact_field,
        from_ai=bool(getattr(row, "from_ai", False)),
        finding_id=finding_id,
    )


#: Where a finding with no section reference lands, per code.
_CODE_SECTIONS = {"employment_gap": "experience", "no_experience": "experience"}


def _locate(ref: str, code: str, content: dict[str, Any]) -> tuple[str, str, str]:
    """(what the candidate calls it, the editor field to jump to, entry id)."""
    if ref == "summary":
        section = content_rules.section_by_key(content, "summary")
        return "Summary", f"text-{section['id']}" if section else "", ""
    if ref.startswith("contact."):
        wanted = ref.split(".", 1)[1]
        return f"Contact details — {wanted}", f"c-{wanted}", ""
    if ref.startswith("entry."):
        parts = ref.split(".")
        entry_id = parts[1] if len(parts) > 1 else ""
        slot = parts[2] if len(parts) > 2 else "bullets"
        located = content_rules.entry_by_id(content, entry_id)
        if located is None:
            return "An entry that has since been removed", "", ""
        section, entry = located
        name = (
            " at ".join(part for part in (entry.get("title", ""), entry.get("org", "")) if part)
            or "Untitled entry"
        )
        prefix = {"title": "et", "org": "eo", "dates": "ed"}.get(slot, "eb")
        return f"{section.get('title', 'Section')} — {name}", f"{prefix}-{entry_id}", entry_id
    if ref.startswith("section."):
        parts = ref.split(".")
        token = parts[1] if len(parts) > 1 else ""
        section = content_rules.section_by_id(content, token) or content_rules.section_by_key(
            content, token
        )
        if section is not None:
            return section.get("title", token.title()), f"title-{section['id']}", ""
        return token.replace("_", " ").title() or "Section", "", ""
    key = _CODE_SECTIONS.get(code, "")
    if key:
        section = content_rules.section_by_key(content, key)
        if section is not None:
            return section.get("title", key.title()), f"title-{section['id']}", ""
        return key.title(), "", ""
    return "The whole CV", "", ""
