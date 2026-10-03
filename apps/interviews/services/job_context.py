"""
What an interview knows about the job it is for (INT-04).

One structured reading of a posting, used by both ways an interview gets its
questions:

  * the **AI kit** (`planning.plan_job`) sends it to the model, and
  * the **job-aware fallback** (`fallback_pools`) builds questions from it
    directly, for every interview that runs before a kit exists.

The fallback is the fix for what manual testing found. A kit is written by a
model off the request, so the first practice run for any posting — and every
run on a deployment with no model configured — used the platform's four
general competencies. A Backend Developer and a UI/UX Designer were asked the
same six questions, word for word.

Two defects fed that, both fixed here:

  * **The fallback ignored the job entirely**, even when a job was named.
  * **The structured posting never reached the model.** `Job.responsibilities`,
    `Job.requirements` and the required/preferred split on `JobSkill` are what
    an employer fills in on purpose; the prompt only saw sentences split out
    of the free-text description, and one flat skills list.

Questions built here are templated, so they are plainer than a model's; what
they are not is generic. Every one names something the posting itself says —
a skill, a responsibility, a requirement, the title — and a posting that says
little gets few job questions rather than invented ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from apps.jobs.models import Job

#: Levels where "tell me how you led" is the wrong question and "tell me how
#: you learned" is the right one.
_JUNIOR = {"intern", "entry"}
_SENIOR = {"senior", "lead", "executive"}


@dataclass(frozen=True)
class JobContext:
    """The parts of a posting an interview is written from."""

    title: str
    level: str = ""
    level_label: str = ""
    company: str = ""
    category: str = ""
    industry: str = ""
    description: str = ""
    responsibilities: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)

    @property
    def skills(self) -> list[str]:
        return [*self.required_skills, *self.preferred_skills]

    @property
    def is_junior(self) -> bool:
        return self.level in _JUNIOR

    @property
    def is_senior(self) -> bool:
        return self.level in _SENIOR

    def as_prompt_inputs(self) -> dict[str, Any]:
        """The model's inputs, every structured field named.

        `skills` and `responsibilities` keep their old names and shapes so the
        prompt's existing instructions still apply; the new fields add what
        they could not carry.
        """
        numbered = "\n".join(
            f"{number}. {sentence}"
            for number, sentence in enumerate(self.all_responsibilities(), start=1)
        )
        return {
            "job_title": self.title,
            "experience_level": self.level_label or "unspecified",
            "company": self.company or "not stated",
            "job_description": self.description[:6_000],
            "skills": ", ".join(self.skills[:20]) or "none listed",
            "required_skills": ", ".join(self.required_skills[:20]) or "none listed",
            "preferred_skills": ", ".join(self.preferred_skills[:20]) or "none listed",
            "requirements": "\n".join(f"- {item}" for item in self.requirements[:15])
            or "none listed",
            "category": self.category or "not stated",
            "industry": self.industry or "not stated",
            "responsibilities": numbered,
        }

    def all_responsibilities(self) -> list[str]:
        """The employer's own list first, then the description's sentences.

        The structured list is what the employer wrote *as* responsibilities;
        the description is prose that may repeat it. Both are kept, deduped,
        because an advert that only has prose must still be covered.
        """
        rows: list[str] = []
        seen: set[str] = set()
        for sentence in [*self.responsibilities, *_sentences(self.description)]:
            key = sentence.lower().rstrip(".")
            if key and key not in seen:
                seen.add(key)
                rows.append(sentence)
        if self.skills:
            rows.append("Skills used in this role: " + ", ".join(self.skills[:20]) + ".")
        return rows[:15] or [self.title]


def from_job(job: Job) -> JobContext:
    """Read one posting into a `JobContext`."""
    from apps.jobs.models import Requirement

    required: list[str] = []
    preferred: list[str] = []
    for row in job.skills.select_related("skill").order_by("order", "pk"):
        if not row.skill_id:
            continue
        name = row.skill.name
        if row.requirement == Requirement.REQUIRED:
            required.append(name)
        else:
            preferred.append(name)

    company = job.company if job.company_id else None
    industry = getattr(getattr(company, "industry", None), "name", "") if company else ""
    category = getattr(getattr(job, "category", None), "name", "") or ""

    return JobContext(
        title=job.title,
        level=job.experience_level or "",
        level_label=job.get_experience_level_display() if job.experience_level else "",
        company=company.name if company else "",
        category=category,
        industry=industry,
        description=job.description or "",
        responsibilities=_clean_list(job.responsibilities),
        requirements=_clean_list(job.requirements),
        required_skills=required,
        preferred_skills=preferred,
    )


# --- the job-aware fallback -----------------------------------------------


def fallback_pools(context: JobContext, general: dict[str, Any]) -> dict[str, Any]:
    """Competencies and questions for this job, built without a model.

    Only the competencies the posting gives evidence for are included: a job
    with no listed skills gets no "tools" competency rather than one about
    tools it never named. Communication and problem solving are kept from the
    general set unchanged, because they apply to every role.
    """
    pools: dict[str, Any] = {}

    skills = context.required_skills or context.preferred_skills
    if skills:
        pools["role_skills"] = {
            "label": f"Skills for the {context.title} role",
            "why": "Whether they have actually used what this posting asks for.",
            "questions": _skill_questions(context, skills),
        }

    family = discipline(context)
    if family in _CRAFT:
        label, why, questions = _CRAFT[family]
        pools["role_craft"] = {
            "label": label.format(title=context.title),
            "why": why,
            "questions": [question.format(**_slots(context)) for question in questions],
        }

    duties = _duties(context)
    if duties:
        pools["role_responsibilities"] = {
            "label": "The work itself",
            "why": "Whether they have done the things this job involves day to day.",
            "questions": _duty_questions(context, duties),
        }

    pools["role_understanding"] = {
        "label": f"Understanding the {context.title} role",
        "why": "Whether they know what this job is for and how it is done well.",
        "questions": _understanding_questions(context),
    }

    for key in ("problem_solving", "communication"):
        if key in general:
            pools[key] = general[key]

    return pools


#: Questions a specialist in the skill would ask (INT-05). Manual testing
#: found "Tell me about a project where you used Figma" too generic: what a
#: design lead asks is how the prototype was tested before it was final.
#: Keyed by the taxonomy's canonical name, lower-cased; phrased as something
#: the candidate did, like every other question (the plan prompt's rule).
_SKILL_QUESTIONS: dict[str, str] = {
    "figma": (
        "Walk me through the usability testing you ran on a Figma prototype before you "
        "finalised the design. What did you test, with whom, and what changed?"
    ),
    "ux research": (
        "Tell me about a study you planned: who you recruited, what you asked, and which "
        "design decision the findings changed."
    ),
    "prototyping": (
        "Tell me about a prototype you built to answer a specific question. What was the "
        "question, and what did the prototype tell you?"
    ),
    "design systems": (
        "Tell me about a one-off design you turned into a reusable component. How did you "
        "decide it belonged in the design system?"
    ),
    "accessibility": (
        "Tell me about an accessibility problem you found in a product before release. How "
        "did you find it, and what did you change?"
    ),
    "rest apis": (
        "Tell me about an API you designed that had to serve a lot of users, tens of "
        "thousands or more. What did you paginate, cache or rate-limit, and why?"
    ),
    "django": (
        "Tell me about a Django view or query that was slow in production. How did you find "
        "the cause, and what did you change?"
    ),
    "python": (
        "Tell me about a piece of Python you wrote that other people had to maintain. What "
        "did you do to keep it readable and tested?"
    ),
    "postgresql": (
        "Tell me about a PostgreSQL schema change or migration you ran on live data. How "
        "did you make it safe?"
    ),
    "docker": (
        "Tell me about a Docker image you built for production. How did you keep it small "
        "and the build reproducible?"
    ),
    "kubernetes": (
        "Tell me about a deployment on Kubernetes that went wrong. What did you see, and "
        "how did you roll back or fix it?"
    ),
    "celery": (
        "Tell me about a background job that failed or ran twice. How did you make it safe "
        "to retry?"
    ),
    "sql": (
        "Tell me about a SQL query whose result someone questioned. How did you check it was right?"
    ),
    "seo": (
        "Tell me about keywords you chose to target. How did you choose them, and how did "
        "you know months later whether it had worked?"
    ),
    "content marketing": (
        "Tell me about a campaign where you measured return on the money and time spent. "
        "How did you work out the ROI, and what did you change because of it?"
    ),
    "email marketing": (
        "Tell me about an email campaign where opens looked good but results did not. How "
        "did you find out why?"
    ),
    "social media marketing": (
        "Tell me about a social post or series that performed very differently from what "
        "you expected. What did you learn from the numbers?"
    ),
    "data analysis": (
        "Tell me about an analysis whose result surprised you. How did you check it before "
        "you shared it?"
    ),
    "power bi": (
        "Tell me about a dashboard you built that people actually used. How did you decide "
        "what went on it?"
    ),
}


def _skill_questions(context: JobContext, skills: list[str]) -> list[str]:
    questions: list[str] = [
        _SKILL_QUESTIONS[skill.lower()] for skill in skills if skill.lower() in _SKILL_QUESTIONS
    ][:4]
    for skill in skills[:4]:
        questions.append(
            f"Tell me about a project where you used {skill}. What was your part, "
            "and what did it produce?"
        )
    lead = skills[0]
    family = discipline(context)
    if context.is_junior:
        questions.append(
            f"How did you learn {lead}, and what is the most useful thing you have "
            f"{'built' if family in _BUILDERS else 'done'} with it?"
        )
    elif family in _BUILDERS:
        questions.append(
            f"Describe a time {lead} did not behave the way you expected. "
            "How did you find out why, and what did you change?"
        )
    else:
        # "Figma did not behave the way you expected" is a debugging question
        # asked of a designer. Outside engineering and data the useful
        # question is about judgement: how they knew the work was good.
        questions.append(
            f"Show me how {lead} fits into the way you work. What did you make with it "
            "most recently, and how did you know it was good enough?"
        )
    if len(skills) > 1:
        questions.append(
            f"This role uses {skills[0]} together with {skills[1]}. "
            "Tell me about work where you used them together."
        )
    return list(dict.fromkeys(questions))[:8]


def _duty_questions(context: JobContext, duties: list[str]) -> list[str]:
    questions = []
    for duty in duties[:5]:
        statement = duty.rstrip(".").strip()
        if len(statement) > 160:
            statement = statement[:157].rsplit(" ", 1)[0] + "…"
        questions.append(
            f"The posting says this role involves: “{statement}.” "
            "Tell me about a time you did something like that. What did you do, "
            "and what was the result?"
        )
    return questions


def _understanding_questions(context: JobContext) -> list[str]:
    title = context.title
    questions = [
        f"What do you think a {title} should have achieved after their first three months?",
        f"What separates a good {title} from an adequate one, in your experience?",
    ]
    if context.requirements:
        first = context.requirements[0].rstrip(".")
        questions.append(f"The posting asks for “{first}.” How does your experience meet that?")
    if context.is_senior:
        questions.append(
            f"Tell me about a time you set the direction for work like this {title} role "
            "and had to bring others with you."
        )
    elif context.is_junior:
        questions.append(
            f"What have you done so far, in study, projects or work, that is closest "
            f"to what a {title} does?"
        )
    if context.industry:
        questions.append(
            f"What do you think is different about doing this work in {context.industry}?"
        )
    return questions[:8]


# --- the discipline --------------------------------------------------------

#: Disciplines whose tools can be debugged, so "it did not behave as you
#: expected" is a real question.
_BUILDERS = {"engineering", "data"}

#: Read off the title first — it is the employer's own word for the job —
#: then the category. Order matters: "Data Engineer" is data, "Marketing
#: Analyst" is marketing.
_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("design", ("designer", "design", "ux", "ui/ux", "user experience", "creative")),
    ("marketing", ("marketing", "seo", "content", "brand", "growth", "e-commerce", "social media")),
    ("data", ("data", "analyst", "analytics", "bi ", "business intelligence")),
    (
        "engineering",
        ("engineer", "developer", "devops", "sre", "reliability", "software", "qa ", "programmer"),
    ),
    ("people", ("hr", "human resources", "people", "recruit", "talent")),
)

#: What a role in each discipline is judged on, beyond its listed skills.
#: Placeholders are filled from the posting (`_slots`), so a question names
#: this job's own work where the posting says what it is.
_CRAFT: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "engineering": (
        "Engineering practice",
        "How they design, test, debug and ship: the work behind the skills list.",
        (
            "Walk me through how you would build this: “{duty}.” How would you "
            "test it, and how would you release it without breaking what is live?",
            "Tell me about a production problem you tracked down. How did you find "
            "the cause, and what did you change so it would not happen again?",
            "How do you decide what to cover with automated tests, and what to leave out?",
            "Tell me about a code review where you and the reviewer disagreed. What "
            "happened, and what did the code look like in the end?",
        ),
    ),
    "design": (
        "Design practice",
        "How they research, decide, test and hand over design work.",
        (
            "Walk me through a design decision you changed because of what you saw "
            "in research or testing. What did you see, and what did you change?",
            "How would you approach this part of the job: “{duty}”? Who would you "
            "talk to first, and how would you know the result works for them?",
            "How do you hand a design to engineers so it is built as intended, and "
            "what do you do when it is not?",
            "Tell me about a time accessibility and a visual or business request "
            "pulled in different directions. What did you decide, and why?",
        ),
    ),
    "marketing": (
        "Marketing practice",
        "How they plan, run and measure campaigns and content.",
        (
            "Tell me about a campaign you ran. What was the goal, which channels did "
            "you use, and how did you measure whether it worked?",
            "The posting says the job involves: “{duty}.” How would you plan that "
            "for your first quarter, and what would you measure?",
            "Describe a piece of content or a campaign that did worse than you "
            "expected. What did the numbers tell you, and what did you change?",
            "With a small budget, how do you decide which channel gets it?",
        ),
    ),
    "data": (
        "Analysis practice",
        "How they get from messy data to a decision someone acts on.",
        (
            "Tell me about an analysis where the data was incomplete or wrong. How "
            "did you find out, and how did you check your final numbers?",
            "Describe a time your analysis changed a decision. Who made the "
            "decision, and how did you present it to them?",
            "How would you approach this part of the job: “{duty}”? What would you "
            "want to know before you started?",
        ),
    ),
    "people": (
        "People practice",
        "How they handle the conversations and processes the role runs.",
        (
            "Tell me about a difficult conversation with an employee or candidate. "
            "What did you say, and what happened afterwards?",
            "How would you approach this part of the job: “{duty}”?",
            "Describe a people process you improved. What was wrong with it, and how "
            "did you know your change worked?",
        ),
    ),
}


def discipline(context: JobContext) -> str:
    """Which family of work this posting is, or '' when it says too little."""
    for text in (f" {context.title.lower()} ", f" {context.category.lower()} "):
        for family, words in _FAMILIES:
            if any(word in text for word in words):
                return family
    return ""


def _slots(context: JobContext) -> dict[str, str]:
    duties = _duties(context)
    duty = duties[0].rstrip(".") if duties else f"the core work of a {context.title}"
    if len(duty) > 140:
        duty = duty[:137].rsplit(" ", 1)[0] + "…"
    return {"duty": duty, "title": context.title}


def _duties(context: JobContext) -> list[str]:
    """What the job involves, as statements a question can quote.

    The employer's structured responsibilities when there are any. Only when
    there are none are sentences taken from the description — and not the
    ones that describe the company rather than the job: "Summit Digital Labs
    builds booking software for hotels" was being quoted back to candidates
    as something the role involved.
    """
    if context.responsibilities:
        return list(context.responsibilities)
    company = context.company.lower()
    rows = []
    for sentence in _sentences(context.description):
        lowered = sentence.lower()
        if company and lowered.startswith(company):
            continue
        if re.match(r"(we|we're|we are|our|about us|join)\b", lowered):
            continue
        rows.append(sentence)
    return rows


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    rows = []
    for item in value:
        text = " ".join(str(item or "").split())
        if len(text.split()) >= 2:
            rows.append(text)
    return rows[:15]


def _sentences(text: str) -> list[str]:
    parts = [" ".join(part.split()) for part in re.split(r"(?<=[.!?])\s+|\n+|;\s+", text or "")]
    return [part for part in parts if len(part.split()) >= 4]
