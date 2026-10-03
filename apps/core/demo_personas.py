"""
Ready-to-test accounts for manual verification (ledger §11, DEMO-01).

`seed_demo` builds a market — twenty-odd postings, a dozen candidates, a
pipeline in motion. That is right for a demo and wrong for a manual test,
where the tester needs *one* candidate, *one* employer and *one* reviewer in
known states, and needs to know which accounts they are. This module adds
exactly that, inside the same command, through the same services:

  * **demo.candidate@example.com** — Maya Karki, on Pro (so analyses against
    a target job can be run as often as a test needs). A CV, an analysis, an
    application to the Backend Developer posting, a live interview booked for
    it, a job-aware practice interview ready to answer, and a follow on
    Summit Digital Labs (and deliberately not on Lakeside Logistics).
  * **demo.employer@example.com** — Rohan Adhikari, owner of Summit Digital
    Labs. Three published postings that are deliberately unlike each other
    (backend, design, marketing), one draft ready to publish so the follower
    notice can be watched arriving, and Maya's application on the board.
  * **demo.employer2@example.com** — Sunita Rai, owner of Lakeside Logistics:
    the company Maya does *not* follow.
  * **demo.verify@example.com** — Kiran Thapa, owner of Gandaki Traders, which
    has submitted verification with only its registration certificate — so the
    queue shows "Not sent: PAN or VAT certificate" and every decision can be
    tried.
  * **demo.staff@example.com** — Asha Pokharel, a platform moderator. A
    `StaffRole` row, not Django's `is_staff`: the verification queue is gated
    on the role, and this account has nothing more than a real moderator has.

Added for the manual-testing pack (ledger §19):

  * **demo.interview@example.com** — Arjun Shrestha, a designer with a CV and
    an application to UI/UX Designer that the employer has moved to the
    Interview stage, with open times to book and a job-aware practice
    interview waiting: the whole interview path without setting anything up.
  * **demo.admin@example.com** — Bina Tamang, Company admin at Summit Digital
    Labs (joined by invitation, not owner).
  * **demo.recruiter@example.com** — Nabin Gurung, Recruiter at Summit Digital
    Labs, assigned to Backend Developer and deliberately *not* to UI/UX
    Designer — both have applicants, so D-13 can be seen from both sides.
  * **demo.candidate2@example.com** — Sita Magar, a second candidate with her
    own CV and applications (Junior Data Scientist at Summit, Operations
    Coordinator at Lakeside), following Lakeside and not Summit: the other
    side of every cross-user check.
  * **demo.newcandidate@example.com** — Ramesh Bhatta, signed up and confirmed
    with nothing else: the empty states.

Nothing here bypasses anything. Accounts are registered through
`identity.register`, companies through `create_company`, postings through the
posting and lifecycle services, the application through the apply service,
the interview through the scheduling service. **Two-factor authentication is
not switched off**: owners and staff get the same fourteen-day grace as any
new owner or moderator (`accounts.mfa_policy`), and after it they are asked to
set it up like everybody else.

Idempotent: every step looks for what it would create first. Running it twice
changes nothing the second time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

#: The seed's own password, shared with every demo account and printed on
#: completion. A fixture, not a secret: `@example.com` cannot receive mail.
PASSWORD = "CareerAI!Demo24"  # noqa: S105 - printed on purpose
DOMAIN = "example.com"

CANDIDATE_EMAIL = f"demo.candidate@{DOMAIN}"
EMPLOYER_EMAIL = f"demo.employer@{DOMAIN}"
OTHER_EMPLOYER_EMAIL = f"demo.employer2@{DOMAIN}"
VERIFY_OWNER_EMAIL = f"demo.verify@{DOMAIN}"
STAFF_EMAIL = f"demo.staff@{DOMAIN}"
INTERVIEW_CANDIDATE_EMAIL = f"demo.interview@{DOMAIN}"
ADMIN_EMAIL = f"demo.admin@{DOMAIN}"
RECRUITER_EMAIL = f"demo.recruiter@{DOMAIN}"
SECOND_CANDIDATE_EMAIL = f"demo.candidate2@{DOMAIN}"
NEW_CANDIDATE_EMAIL = f"demo.newcandidate@{DOMAIN}"

#: Every account this module creates, in the order the command prints them.
ACCOUNTS: tuple[tuple[str, str, str], ...] = (
    ("A", CANDIDATE_EMAIL, "candidate - CV, analysis, application, live + practice interview"),
    ("B", INTERVIEW_CANDIDATE_EMAIL, "candidate - application at Interview stage, times to book"),
    ("C", EMPLOYER_EMAIL, "employer - Owner, Summit Digital Labs"),
    ("D", ADMIN_EMAIL, "employer - Company admin, Summit Digital Labs"),
    ("E", RECRUITER_EMAIL, "employer - Recruiter, Summit (Backend Developer only)"),
    ("F", STAFF_EMAIL, "staff - moderator"),
    ("G", SECOND_CANDIDATE_EMAIL, "candidate - second candidate, own CV and applications"),
    ("H", OTHER_EMPLOYER_EMAIL, "employer - Owner, Lakeside Logistics (second company)"),
    ("I", VERIFY_OWNER_EMAIL, "employer - Owner, Gandaki Traders (verification pending)"),
    ("J", NEW_CANDIDATE_EMAIL, "candidate - signed up, no CV (empty states)"),
)

COMPANY = "Summit Digital Labs"
OTHER_COMPANY = "Lakeside Logistics"
PENDING_COMPANY = "Gandaki Traders"

#: Three postings that share nothing, so an interview for one can be compared
#: with an interview for another and the difference is the job, not chance.
JOBS: list[dict[str, Any]] = [
    {
        "title": "Backend Developer",
        "level": "mid",
        "category": "Software Engineering",
        "description": (
            "Summit Digital Labs builds booking and payments software for hotels in "
            "Pokhara and Kathmandu. You will work on the Django service that takes "
            "every reservation."
        ),
        "responsibilities": [
            "Design and maintain REST APIs used by our booking apps",
            "Own PostgreSQL schema changes and migrations",
            "Review pull requests and keep the test suite fast",
        ],
        "requirements": [
            "Two or more years building web services in Python",
            "Experience with Django or a similar framework",
        ],
        "required": ["Python", "Django", "REST APIs", "PostgreSQL"],
        "preferred": ["Docker"],
        "salary": (90000, 140000),
    },
    {
        "title": "UI/UX Designer",
        "level": "senior",
        "category": "Design and creative",
        "description": (
            "Own the experience of our guest-facing booking app, from research with "
            "hotel staff to the component library the engineers build from."
        ),
        "responsibilities": [
            "Run usability research sessions with hotel front-desk staff",
            "Prototype new booking flows in Figma and test them",
            "Maintain the product design system",
        ],
        "requirements": ["A portfolio of shipped product design work"],
        "required": ["Figma", "UX research", "Prototyping"],
        "preferred": ["Design systems"],
        "salary": (80000, 120000),
    },
    {
        "title": "Marketing Specialist",
        "level": "entry",
        "category": "Sales and marketing",
        "description": (
            "Grow direct bookings for the hotels we serve through campaigns, content "
            "and careful measurement."
        ),
        "responsibilities": [
            "Plan and run seasonal marketing campaigns",
            "Report on campaign performance with web analytics",
            "Coordinate content with the design and sales teams",
        ],
        "requirements": ["Some experience running a campaign, paid or organic"],
        "required": ["Content marketing", "SEO", "Data analysis"],
        "preferred": ["Social media marketing", "Email marketing"],
        "salary": (45000, 65000),
    },
    # Added for the manual-testing pack: a data/ML posting with multi-word
    # required skills and preferred ones, so required-versus-preferred gap
    # analysis has something to compare that is unlike the other three.
    {
        "title": "Junior Data Scientist",
        "level": "entry",
        "category": "Data and analytics",
        "description": (
            "Help our hotel partners forecast occupancy: clean booking data, build "
            "and check simple models, and explain the results to people who are not "
            "data scientists."
        ),
        "responsibilities": [
            "Build and validate occupancy forecasting models in Python",
            "Write SQL against the bookings warehouse",
            "Present model results to hotel partners in plain language",
        ],
        "requirements": [
            "A degree or project work in statistics, computer science or similar",
            "Comfortable explaining a model's limits",
        ],
        "required": ["Python", "SQL", "Machine learning"],
        "preferred": ["pandas", "Tableau"],
        "salary": (60000, 85000),
    },
]

#: Left as a draft on purpose: publishing it is how a tester watches the
#: follower notice arrive for Maya and not for anybody else.
DRAFT_JOB: dict[str, Any] = {
    "title": "Data Analyst",
    "level": "mid",
    "category": "Data and analytics",
    "description": "Turn booking data into the weekly numbers our hotel partners run on.",
    "responsibilities": ["Build weekly occupancy and revenue reports for partners"],
    "requirements": ["Comfortable with SQL"],
    "required": ["SQL", "Data analysis"],
    "preferred": ["Power BI", "Python"],
    "salary": (70000, 100000),
}

OTHER_JOB: dict[str, Any] = {
    "title": "Operations Coordinator",
    "level": "entry",
    "category": "Business and operations",
    "description": "Keep our Pokhara depot's daily dispatch on schedule.",
    "responsibilities": ["Schedule daily dispatch for twelve vehicles"],
    "requirements": ["Good spoken Nepali and English"],
    "required": ["Communication"],
    "preferred": [],
    "salary": (35000, 50000),
}

#: Maya's CV. Fictional throughout; every fact is one she "gave" and nothing
#: the analysis or the assistant may embellish.
CANDIDATE_CV: dict[str, Any] = {
    "schema_version": 1,
    "contact": {
        "name": "Maya Karki",
        "headline": "Junior Backend Developer",
        "email": CANDIDATE_EMAIL,
        "phone": "+977 9800 000 111",
        "location": "Pokhara, Nepal",
    },
    "summary": (
        "Backend developer with two years of Python. Built the booking API for a "
        "family-run guesthouse chain and keeps it running."
    ),
    "sections": [
        {
            "id": "demo-summary",
            "key": "summary",
            "kind": "text",
            "title": "Summary",
            "visible": True,
            "order": 0,
            "text": (
                "Backend developer with two years of Python. Built the booking API for "
                "a family-run guesthouse chain and keeps it running."
            ),
        },
        {
            "id": "demo-experience",
            "key": "experience",
            "kind": "entries",
            "title": "Experience",
            "visible": True,
            "order": 1,
            "entries": [
                {
                    "id": "demo-exp-1",
                    "title": "Backend Developer",
                    "org": "Phewa Stays",
                    "dates": "2024 – Present",
                    "bullets": [
                        "Built the booking API in Django that the guesthouses' website uses.",
                        "Moved the booking data from spreadsheets into PostgreSQL.",
                    ],
                    "source_ref": "",
                    "sync_state": "detached",
                },
            ],
        },
        {
            "id": "demo-skills",
            "key": "skills",
            "kind": "list",
            "title": "Skills",
            "visible": True,
            "order": 2,
            "items": ["Python", "Django", "PostgreSQL", "Git"],
        },
        {
            "id": "demo-education",
            "key": "education",
            "kind": "entries",
            "title": "Education",
            "visible": True,
            "order": 3,
            "entries": [
                {
                    "id": "demo-edu-1",
                    "title": "BSc Computer Science",
                    "org": "Pokhara University",
                    "dates": "2019 – 2023",
                    "bullets": [],
                    "source_ref": "",
                    "sync_state": "detached",
                }
            ],
        },
    ],
}


def _cv(
    *,
    name: str,
    email: str,
    headline: str,
    location: str,
    summary: str,
    role: dict[str, Any],
    skills: list[str],
    education: dict[str, Any],
    slug: str,
) -> dict[str, Any]:
    """A CV in the builder's own shape. Fictional; nothing to embellish."""
    return {
        "schema_version": 1,
        "contact": {
            "name": name,
            "headline": headline,
            "email": email,
            "phone": "",
            "location": location,
        },
        "summary": summary,
        "sections": [
            {
                "id": f"{slug}-summary",
                "key": "summary",
                "kind": "text",
                "title": "Summary",
                "visible": True,
                "order": 0,
                "text": summary,
            },
            {
                "id": f"{slug}-experience",
                "key": "experience",
                "kind": "entries",
                "title": "Experience",
                "visible": True,
                "order": 1,
                "entries": [
                    {"id": f"{slug}-exp-1", "source_ref": "", "sync_state": "detached", **role}
                ],
            },
            {
                "id": f"{slug}-skills",
                "key": "skills",
                "kind": "list",
                "title": "Skills",
                "visible": True,
                "order": 2,
                "items": skills,
            },
            {
                "id": f"{slug}-education",
                "key": "education",
                "kind": "entries",
                "title": "Education",
                "visible": True,
                "order": 3,
                "entries": [
                    {
                        "id": f"{slug}-edu-1",
                        "bullets": [],
                        "source_ref": "",
                        "sync_state": "detached",
                        **education,
                    }
                ],
            },
        ],
    }


INTERVIEW_CANDIDATE_CV = _cv(
    name="Arjun Shrestha",
    email=INTERVIEW_CANDIDATE_EMAIL,
    headline="Product Designer",
    location="Kathmandu, Nepal",
    summary=(
        "Product designer with three years on booking and travel apps. Runs "
        "usability tests with real users and prototypes in Figma."
    ),
    role={
        "title": "Product Designer",
        "org": "Trekking Route Planner (fictional)",
        "dates": "2022 – Present",
        "bullets": [
            "Prototyped the trip-booking flow in Figma and tested it with eight trekkers.",
            "Ran usability tests that cut the booking form from nine fields to five.",
        ],
    },
    skills=["Figma", "Prototyping", "UX research"],
    education={
        "title": "BFA Graphic Design",
        "org": "Kathmandu University",
        "dates": "2017 – 2021",
    },
    slug="demo-arjun",
)

SECOND_CANDIDATE_CV = _cv(
    name="Sita Magar",
    email=SECOND_CANDIDATE_EMAIL,
    headline="Data Analyst",
    location="Butwal, Nepal",
    summary=(
        "Data analyst who writes SQL every day and has started building simple "
        "forecasting models in Python."
    ),
    role={
        "title": "Data Analyst",
        "org": "Lumbini Microfinance (fictional)",
        "dates": "2023 – Present",
        "bullets": [
            "Wrote the SQL behind the weekly loan-repayment report used by 14 branches.",
            "Built a repayment-risk model in Python with pandas for a pilot branch.",
        ],
    },
    skills=["SQL", "Python", "pandas", "Data analysis"],
    education={"title": "BSc Statistics", "org": "Tribhuvan University", "dates": "2019 – 2023"},
    slug="demo-sita",
)


@dataclass
class Report:
    """What the personas step did, for the command to print."""

    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def made(self, what: str) -> None:
        self.created.append(what)

    def had(self, what: str) -> None:
        self.kept.append(what)


def seed(*, write=lambda message: None) -> Report:
    """Create or confirm every persona. Safe to run any number of times."""
    report = Report()
    _prerequisites()

    employer = _account("Rohan Adhikari", EMPLOYER_EMAIL, "employer", report)
    other_employer = _account("Sunita Rai", OTHER_EMPLOYER_EMAIL, "employer", report)
    verify_owner = _account("Kiran Thapa", VERIFY_OWNER_EMAIL, "employer", report)
    candidate_user = _account("Maya Karki", CANDIDATE_EMAIL, "candidate", report)
    staff_user = _account("Asha Pokharel", STAFF_EMAIL, "candidate", report)

    company = _company(COMPANY, employer, report, website="https://summit-digital.example.com")
    other_company = _company(OTHER_COMPANY, other_employer, report)
    pending_company = _company(PENDING_COMPANY, verify_owner, report)

    jobs = [_job(company, spec, report, publish=True) for spec in JOBS]
    _job(company, DRAFT_JOB, report, publish=False)
    profile, version, document = _candidate(candidate_user, report)
    _pro(profile, report)
    _analysis(document, candidate_user, report)
    backend = jobs[0]
    application = _application(backend, profile, candidate_user, version, report)
    _follow(candidate_user, company, report)
    _live_interview(application, company, employer, report)
    _practice_interview(profile, candidate_user, backend, report)
    _pending_verification(pending_company, verify_owner, report)
    _staff(staff_user, report)
    # After the staff role exists: the demo employer's company is verified by
    # the demo moderator through the real claim-and-decide path (DEMO-04).
    _verified_company(company, employer, staff_user, report)
    _complete_profile(profile, candidate_user, report)
    _company_updates(company, employer, report)

    # --- the manual-testing pack (ledger §19) ---------------------------------
    uiux, data_job = jobs[1], jobs[3]
    other_job = _job(other_company, OTHER_JOB, report, publish=True)

    admin = _account("Bina Tamang", ADMIN_EMAIL, "employer", report)
    _member(company, admin, "admin", employer, report)
    recruiter = _account("Nabin Gurung", RECRUITER_EMAIL, "employer", report)
    recruiter_membership = _member(company, recruiter, "recruiter", employer, report)
    _assign(backend, recruiter_membership, employer, report)

    arjun = _account("Arjun Shrestha", INTERVIEW_CANDIDATE_EMAIL, "candidate", report)
    arjun_profile, arjun_version, arjun_document = _candidate_with_cv(
        arjun, INTERVIEW_CANDIDATE_CV, "Arjun Shrestha CV", "Kathmandu", report
    )
    _analysis(arjun_document, arjun, report, target_role="UI/UX Designer")
    arjun_application = _application(uiux, arjun_profile, arjun, arjun_version, report)
    _to_interview_stage(arjun_application, company, employer, report)
    _bookable_times(company, employer, report)
    _practice_interview(arjun_profile, arjun, uiux, report)

    sita = _account("Sita Magar", SECOND_CANDIDATE_EMAIL, "candidate", report)
    sita_profile, sita_version, sita_document = _candidate_with_cv(
        sita, SECOND_CANDIDATE_CV, "Sita Magar CV", "Butwal", report
    )
    _analysis(sita_document, sita, report, target_role="Junior Data Scientist")
    _application(data_job, sita_profile, sita, sita_version, report)
    _application(other_job, sita_profile, sita, sita_version, report)
    _follow(sita, other_company, report)

    newcomer = _account("Ramesh Bhatta", NEW_CANDIDATE_EMAIL, "candidate", report)
    _bare_profile(newcomer, report)

    for line in report.created:
        write(f"persona: {line}")
    return report


# --- steps ----------------------------------------------------------------


def _prerequisites() -> None:
    from django.core.management import call_command

    from apps.interviews.models import InterviewTemplate
    from apps.resumes.models import ResumeTemplate
    from apps.taxonomy.models import Skill

    if not Skill.objects.exists():
        call_command("seed_taxonomy", verbosity=0)
    if not ResumeTemplate.objects.exists():
        call_command("seed_resume_templates", "--quiet-summary")
    if not InterviewTemplate.objects.filter(slug="live-conversation").exists():
        call_command("seed_interview_templates", verbosity=0)
    call_command("seed_plans", "--quiet-summary")
    call_command("seed_pipeline_stages", verbosity=0)


def _account(full_name: str, email: str, intent: str, report: Report):
    """A real sign-up, with a confirmed address."""
    from apps.accounts.models import User
    from apps.accounts.services import identity

    user = User.objects.filter(email=email).first()
    if user is not None:
        report.had(email)
        return user

    user = identity.register(
        email=email, password=PASSWORD, full_name=full_name, signup_intent=intent
    )
    first, _, last = full_name.partition(" ")
    user.first_name, user.last_name = first, last
    user.save(update_fields=["first_name", "last_name"])
    user.email_addresses.filter(is_primary=True).update(verified_at=timezone.now())
    report.made(f"account {email}")
    return user


def _company(name: str, owner, report: Report, *, website: str = ""):
    from apps.companies.models import Company
    from apps.companies.services import company as company_service

    existing = Company.objects.filter(name=name, created_by=owner).first()
    if existing is not None:
        report.had(name)
        return existing
    company = company_service.create_company(
        actor=owner,
        name=name,
        website=website,
        tagline=f"{name} — fictional company for manual testing.",
        about=[f"{name} is demo data. It does not exist."],
    )
    report.made(f"company {name}")
    return company


def _owner_membership(company):
    from apps.companies.models import CompanyMembership, Role

    return CompanyMembership.objects.get(company=company, role=Role.OWNER)


def _job(company, spec: dict[str, Any], report: Report, *, publish: bool):
    from apps.jobs.models import Job, JobStatus, Workplace
    from apps.jobs.services import lifecycle, postings
    from apps.taxonomy.models import JobCategory

    membership = _owner_membership(company)
    actor = membership.user
    category = JobCategory.objects.filter(name__iexact=spec.get("category", "")).first()
    matching = Job.objects.filter(company=company, title=spec["title"])
    if not publish:
        # The draft is there to be published by a tester (DEMO-02). Once it
        # has been, it stays published — what a tester did is kept — and a
        # fresh draft is written beside it, so the next tester still has one
        # to publish and a follower notice still to watch arrive. Idempotent:
        # while a draft exists, nothing is added.
        matching = matching.filter(status=JobStatus.DRAFT)
    existing = matching.order_by("pk").first()
    if existing is not None:
        # Converge rather than skip: a posting an earlier version of this seed
        # wrote keeps its identity (and anything a tester did to it) and gains
        # whatever category or skills the spec now names.
        if category is not None and existing.category_id != category.pk:
            existing.category = category
            existing.save(update_fields=["category", "updated_at"])
        _ensure_skills(existing, actor, spec, report)
        if publish and existing.status == JobStatus.DRAFT:
            lifecycle.publish(job=existing, actor=actor, membership=membership)
        report.had(f"job {spec['title']}")
        return existing

    low, high = spec["salary"]
    job = postings.create_job(
        company=company,
        actor=actor,
        title=spec["title"],
        description=spec["description"],
        responsibilities=spec["responsibilities"],
        requirements=spec["requirements"],
        benefits=[],
        category_id=category.pk if category else None,
        workplace=Workplace.ON_SITE,
        experience_level=spec["level"],
        salary_min=low,
        salary_max=high,
        currency="NPR",
        period="month",
    )
    _ensure_skills(job, actor, spec, report)
    if publish:
        lifecycle.publish(job=job, actor=actor, membership=membership)
    report.made(f"job {spec['title']} ({'published' if publish else 'draft'}) at {company.name}")
    return job


def _ensure_skills(job, actor, spec: dict[str, Any], report: Report) -> None:
    """Every skill the spec names, as required or preferred. Idempotent."""
    from apps.jobs.services import postings

    have = set(job.skills.values_list("skill__name", flat=True))
    for requirement, names in (("required", spec["required"]), ("preferred", spec["preferred"])):
        for name in names:
            if name in have:
                continue
            try:
                postings.add_skill(job=job, actor=actor, text=name, requirement=requirement)
            except Exception as exc:  # noqa: BLE001 - an unknown skill is reported, not fatal
                report.skipped.append(f"skill {name} on {spec['title']}: {exc}")


def _candidate(user, report: Report):
    from apps.candidates.services import profile as profile_service
    from apps.candidates.services import skills as skill_service
    from apps.resumes.models import VersionReason
    from apps.resumes.services import documents as document_service
    from apps.resumes.services import versions as version_service
    from apps.taxonomy.models import Location

    profile = profile_service.get_or_create_profile(user=user)
    if not profile.headline:
        location = Location.objects.filter(city__icontains="Pokhara").first()
        profile_service.update_basics(
            profile=profile,
            actor=user,
            headline="Junior Backend Developer",
            summary=CANDIDATE_CV["summary"],
            location_id=location.pk if location else None,
        )
        profile.refresh_from_db()
    if not profile.skills.exists():
        for name in ("Python", "Django", "PostgreSQL", "Git"):
            try:
                skill_service.add_skill(profile=profile, actor=user, text=name)
            except Exception as exc:  # noqa: BLE001
                report.skipped.append(f"profile skill {name}: {exc}")

    document = profile.resume_documents.alive().filter(name="Maya Karki CV").first()
    if document is None:
        document = document_service.create_document(
            profile=profile, actor=user, name="Maya Karki CV", content=CANDIDATE_CV
        )
        report.made("CV Maya Karki CV")
    version = version_service.finalize(document=document, actor=user, reason=VersionReason.SAVED)
    return profile, version, document


def _pro(profile, report: Report) -> None:
    """Maya on Pro, through the billing service's own manual-payment path.

    A free candidate has one analysis a month and the seed spends it, which
    would leave a tester unable to run the target-job comparison the handoff
    asks them to try. `subscribe(provider="manual")` is how staff record a
    bank-transfer payment; nothing about the entitlement checks changes, and
    the subscription lapses on the billing rules like any other (row 1.3k).
    """
    from apps.billing.models import Plan
    from apps.billing.services import entitlements, subscriptions

    if entitlements.live_subscription(profile) is not None:
        report.had("candidate plan")
        return
    plan = Plan.objects.filter(code="pro", is_active=True).first()
    if plan is None:
        report.skipped.append("candidate plan: no active Pro plan (run seed_plans)")
        return
    subscriptions.subscribe(owner=profile, plan=plan, provider="manual")
    report.made("Pro plan for the demo candidate (manual payment)")


def _analysis(document, user, report: Report, *, target_role: str = "Backend Developer") -> None:
    from apps.resumes.models import ResumeAnalysis
    from apps.resumes.services import analysis as analysis_service

    if ResumeAnalysis.objects.filter(version__document=document).exists():
        report.had(f"CV analysis ({document.name})")
        return
    # Deterministic half only: the model's prose is not something a seed
    # should wait on, and the report says it is pending until a worker runs.
    analysis_service.analyse(document=document, actor=user, target_role=target_role, defer_ai=True)
    report.made(f"CV analysis ({document.name})")


def _application(job, profile, user, version, report: Report):
    from apps.applications.models import Application
    from apps.applications.services import apply as apply_service

    existing = Application.objects.filter(job=job, candidate=profile).first()
    if existing is not None:
        report.had(f"application {profile.user.email} -> {job.title}")
        return existing
    application = apply_service.apply_to_job(
        job=job, profile=profile, actor=user, resume_version=version
    )
    report.made(f"application {profile.user.email} -> {job.title}")
    return application


def _follow(user, company, report: Report) -> None:
    from apps.companies.services import follows

    if follows.is_following(user=user, company=company):
        report.had(f"follow {user.email} -> {company.name}")
        return
    follows.follow(user=user, company=company)
    report.made(f"follow {company.name}")


def _live_interview(application, company, employer, report: Report) -> None:
    """A live interview booked for Maya's application, a few days out.

    DEMO-06: the booking used to be made once, three days after the first
    seed, and never moved — three days later the demo showed a "Scheduled"
    interview that had already happened. A booking that is in the past (or
    less than a day away) is now moved through the real reschedule path, so
    the tester always finds an upcoming one; the old session is cancelled as
    "Rescheduled", exactly as the product does it.
    """
    from apps.interviews.models import (
        InterviewKind,
        InterviewSession,
        InterviewTemplate,
        SessionState,
    )
    from apps.interviews.services import scheduling

    membership = _owner_membership(company)
    live = (
        InterviewSession.objects.filter(
            application=application, kind=InterviewKind.LIVE, state=SessionState.SCHEDULED
        )
        .select_related("slot")
        .order_by("-pk")
        .first()
    )
    soon = timezone.now() + dt.timedelta(days=1)
    if live is not None:
        starts = getattr(getattr(live, "slot", None), "starts_at", None)
        if starts is not None and starts >= soon:
            report.had("live interview")
            return
        slot = _open_demo_slot(membership, employer, report)
        if slot is None:
            return
        held = scheduling.hold(slot=slot, actor=employer)
        scheduling.reschedule(
            session=live, slot=held, actor=employer, reason="Demo data: moved to an upcoming day"
        )
        report.made("live interview moved to an upcoming day")
        return
    if (
        InterviewSession.objects.filter(application=application, kind=InterviewKind.LIVE)
        .exclude(state=SessionState.CANCELLED)
        .exists()
    ):
        report.had("live interview")
        return

    template = InterviewTemplate.objects.get(slug="live-conversation")
    slot = _open_demo_slot(membership, employer, report)
    if slot is None:
        return
    held = scheduling.hold(slot=slot, actor=employer)
    scheduling.book(
        slot=held,
        application=application,
        actor=employer,
        template=template,
        interviewers=[employer],
    )
    report.made("live interview booked")


def _open_demo_slot(membership, employer, report: Report):
    """The first free hour from 10:00, three days from now."""
    from apps.common.errors import Conflict
    from apps.interviews.models import InterviewSlot
    from apps.interviews.services import scheduling

    day = (timezone.localtime() + dt.timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    # A slot this seed opened on an earlier run may still be on the calendar,
    # and the calendar refuses an overlap (as it should); an open one is
    # reused rather than duplicated.
    for offset in range(8):
        start = day + dt.timedelta(hours=offset)
        open_slot = InterviewSlot.objects.filter(interviewer=membership, starts_at=start).first()
        if open_slot is not None and open_slot.is_bookable:
            return open_slot
        if open_slot is not None:
            continue
        try:
            return scheduling.publish_slots(
                membership=membership,
                actor=employer,
                windows=[(start, start + dt.timedelta(minutes=45))],
            )[0]
        except Conflict:
            continue
    report.skipped.append("live interview: no free hour on the seeded day")
    return None


def _practice_interview(profile, user, job, report: Report) -> None:
    """A practice interview for the Backend posting, open and waiting.

    Built on the job-aware pool (INT-04), so the first question a tester sees
    already names the posting's own skills and responsibilities.
    """
    from apps.interviews import selectors as interview_selectors
    from apps.interviews.models import InterviewTemplate
    from apps.interviews.services import engine

    # A practice session records its posting in its plan, not in a column.
    for session in interview_selectors.practice_for(profile)[:20]:
        if (session.plan or {}).get("job_title") == job.title:
            report.had("practice interview")
            return
    template = InterviewTemplate.objects.get(slug="general-practice")
    engine.start(candidate=profile, template=template, actor=user, job=job)
    report.made(f"practice interview for {job.title}")


def _pending_verification(company, owner, report: Report) -> None:
    """Submitted with one of the two certificates, so the reviewer has a gap."""
    from apps.companies.models import CompanyMembership, VerificationState
    from apps.companies.services import company as company_service

    verification = company.verification
    if verification.state != VerificationState.NOT_SUBMITTED:
        _rescan_pending(verification, report)
        report.had("pending verification")
        return
    membership = CompanyMembership.objects.get(company=company, user=owner)
    if not verification.documents.exists():
        company_service.add_verification_document(
            actor=owner,
            membership=membership,
            company=company,
            kind="registration",
            uploaded_file=SimpleUploadedFile(
                "gandaki-registration.pdf", _MINIMAL_PDF, content_type="application/pdf"
            ),
        )
    company_service.submit_verification(actor=owner, company=company)
    report.made(f"verification submitted for {company.name} (registration only)")


def _rescan_pending(verification, report: Report) -> None:
    """Ask again for any certificate scan that never came back (DEMO-03).

    The scan is a queued task. A broker restarted before a worker took it —
    an ordinary thing on a laptop — leaves the certificate "pending" for
    good, and the demo reviewer then cannot open the document the queue is
    asking them to review. Re-running the seed re-queues it.
    """
    from apps.files.constants import ScanStatus
    from apps.files.services import _enqueue_scan

    for document in verification.documents.select_related("attachment"):
        if document.attachment.scan_status == ScanStatus.PENDING:
            _enqueue_scan(document.attachment_id)
            report.made(f"scan re-queued for {document.attachment.filename}")


def _verified_company(company, owner, reviewer, report: Report) -> None:
    """Summit Digital Labs, verified — both certificates, a real decision.

    Manual testing asked for a verified demo employer. Done the way a real one
    is: the owner uploads the registration and PAN certificates and submits,
    and the moderator persona claims the submission and decides it. Nothing is
    written to the verification row directly.
    """
    from apps.companies.models import CompanyMembership, VerificationState
    from apps.companies.services import company as company_service

    verification = company.verification
    if verification.state == VerificationState.VERIFIED:
        report.had(f"verified {company.name}")
        return
    membership = CompanyMembership.objects.get(company=company, user=owner)
    if verification.state == VerificationState.NOT_SUBMITTED:
        kinds = set(verification.documents.values_list("kind", flat=True))
        for kind, name in (
            ("registration", "summit-registration.pdf"),
            ("pan_vat", "summit-pan.pdf"),
        ):
            if kind not in kinds:
                company_service.add_verification_document(
                    actor=owner,
                    membership=membership,
                    company=company,
                    kind=kind,
                    uploaded_file=SimpleUploadedFile(
                        name, _MINIMAL_PDF, content_type="application/pdf"
                    ),
                )
        company_service.submit_verification(actor=owner, company=company)
    company_service.claim_verification(actor=reviewer, company=company)
    company_service.review_verification(
        actor=reviewer, company=company, decision=VerificationState.VERIFIED
    )
    report.made(f"{company.name} verified by the demo moderator")


def _complete_profile(profile, user, report: Report) -> None:
    """Maya's profile filled in, so matching and completeness have it all.

    The CV had her history; the profile — which is what matching reads
    (ADR-A46) — was 44% complete. Filled through the profile services, each
    part only when it is missing, so a tester's own edits are kept.
    """
    import datetime as dt

    from apps.candidates.models import LinkKind, Seeking, WorkType
    from apps.candidates.services import profile as profile_service
    from apps.candidates.services import sections

    changed = False
    if not profile.seeking or not profile.open_to or profile.notice_period_days is None:
        profile_service.update_seeking(
            profile=profile,
            actor=user,
            seeking=profile.seeking or Seeking.ACTIVE,
            open_to=profile.open_to or [WorkType.FULL_TIME],
            notice_period_days=30
            if profile.notice_period_days is None
            else profile.notice_period_days,
            salary_min=profile.salary_min or 60000,
            salary_max=profile.salary_max or 90000,
            currency=profile.currency or "NPR",
            period=getattr(profile, "period", "") or "month",
        )
        changed = True
    if not profile.experiences.exists():
        sections.add_entry(
            profile=profile,
            actor=user,
            section="experience",
            title="Junior Backend Developer",
            org_name="Phewa Stays",
            start_date=dt.date(2024, 1, 1),
            is_current=True,
            summary="Booking APIs and internal reporting for 12 partner hotels.",
        )
        changed = True
    if not profile.education.exists():
        sections.add_entry(
            profile=profile,
            actor=user,
            section="education",
            institution="Tribhuvan University",
            qualification="BCA",
            start_date=dt.date(2019, 9, 1),
            end_date=dt.date(2023, 7, 1),
        )
        changed = True
    if not profile.links.exists():
        sections.add_entry(
            profile=profile,
            actor=user,
            section="link",
            kind=LinkKind.GITHUB,
            label="GitHub",
            url="https://github.com/example-maya",
        )
        changed = True
    if changed:
        report.made("complete candidate profile")
    else:
        report.had("complete candidate profile")


def _company_updates(company, owner, report: Report) -> None:
    """One announcement and one upcoming event, so a follower has news (FOL-03)."""
    import datetime as dt

    from apps.companies.models import CompanyMembership, CompanyUpdateKind
    from apps.companies.services import updates

    if company.updates.exists():
        report.had("company updates")
        return
    membership = CompanyMembership.objects.get(company=company, user=owner)
    updates.publish(
        actor=owner,
        membership=membership,
        company=company,
        kind=CompanyUpdateKind.ANNOUNCEMENT,
        title="We are hiring in Pokhara",
        body="Three new roles this quarter, all based at our Lakeside office.",
    )
    updates.publish(
        actor=owner,
        membership=membership,
        company=company,
        kind=CompanyUpdateKind.EVENT,
        title="Open office afternoon",
        body="Meet the engineering and design teams. No registration needed.",
        # 15:00 local time, ten days out.
        starts_at=timezone.localtime().replace(hour=15, minute=0, second=0, microsecond=0)
        + dt.timedelta(days=10),
        location="Lakeside, Pokhara",
    )
    report.made(f"announcement and event for {company.name}")


def _member(company, user, role: str, owner, report: Report):
    """A team member who joined the way a real one does: invited, then accepted."""
    from apps.companies.models import CompanyMembership
    from apps.companies.services import membership as membership_service

    existing = CompanyMembership.objects.filter(company=company, user=user).first()
    if existing is not None:
        report.had(f"{role} membership {user.email}")
        return existing
    _invite, token = membership_service.invite(
        actor=owner,
        actor_membership=_owner_membership(company),
        company=company,
        email=user.email,
        role=role,
    )
    joined = membership_service.accept_invite(user=user, token=token)
    report.made(f"{role} membership {user.email} at {company.name}")
    return joined


def _assign(job, membership, owner, report: Report) -> None:
    from apps.jobs.models import JobAssignment
    from apps.jobs.services import postings

    if JobAssignment.objects.filter(job=job, membership=membership).exists():
        report.had(f"assignment {membership.user.email} -> {job.title}")
        return
    postings.assign(job=job, actor=owner, membership=membership)
    report.made(f"assignment {membership.user.email} -> {job.title}")


def _candidate_with_cv(user, cv: dict[str, Any], name: str, city: str, report: Report):
    """A profile, its skills and a finalised CV — the same path as Maya's."""
    from apps.candidates.services import profile as profile_service
    from apps.candidates.services import skills as skill_service
    from apps.resumes.models import VersionReason
    from apps.resumes.services import documents as document_service
    from apps.resumes.services import versions as version_service
    from apps.taxonomy.models import Location

    profile = profile_service.get_or_create_profile(user=user)
    if not profile.headline:
        location = Location.objects.filter(city__icontains=city).first()
        profile_service.update_basics(
            profile=profile,
            actor=user,
            headline=cv["contact"]["headline"],
            summary=cv["summary"],
            location_id=location.pk if location else None,
        )
        profile.refresh_from_db()
    if not profile.skills.exists():
        skills = next(section["items"] for section in cv["sections"] if section["key"] == "skills")
        for skill in skills:
            try:
                skill_service.add_skill(profile=profile, actor=user, text=skill)
            except Exception as exc:  # noqa: BLE001 - reported, not fatal
                report.skipped.append(f"profile skill {skill}: {exc}")

    document = profile.resume_documents.alive().filter(name=name).first()
    if document is None:
        document = document_service.create_document(
            profile=profile, actor=user, name=name, content=cv
        )
        report.made(f"CV {name}")
    else:
        report.had(f"CV {name}")
    version = version_service.finalize(document=document, actor=user, reason=VersionReason.SAVED)
    return profile, version, document


def _to_interview_stage(application, company, owner, report: Report) -> None:
    """The employer moves the application to Interview, through the pipeline."""
    from apps.applications.models import PipelineStage
    from apps.applications.services import pipeline

    stage = PipelineStage.objects.for_company(company).filter(key="interview").first()
    if stage is None:
        report.skipped.append("interview stage: no stage with key 'interview'")
        return
    if application.stage_id == stage.pk:
        report.had(f"{application.candidate.user.email} at the Interview stage")
        return
    pipeline.move(
        application=application,
        actor=owner,
        membership=_owner_membership(company),
        stage=stage,
        message="We'd like to interview you — please pick a time.",
    )
    report.made(f"{application.candidate.user.email} moved to the Interview stage")


def _bookable_times(company, owner, report: Report) -> None:
    """Three open times, five days out, for the interview-stage candidate to book.

    Kept a working day away from the demo live interview's day so the two never
    overlap. When they have passed, the next run publishes fresh ones.
    """
    from apps.common.errors import Conflict
    from apps.interviews.services import scheduling

    if scheduling.open_slots(company=company).exists():
        report.had("open interview times")
        return
    day = (timezone.localtime() + dt.timedelta(days=5)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    windows = [
        (day + dt.timedelta(hours=hour), day + dt.timedelta(hours=hour, minutes=45))
        for hour in (10, 11, 14)
    ]
    try:
        scheduling.publish_slots(
            membership=_owner_membership(company), actor=owner, windows=windows
        )
    except Conflict as exc:
        report.skipped.append(f"open interview times: {exc.message}")
        return
    report.made("three open interview times, five days out")


def _bare_profile(user, report: Report) -> None:
    """Signed up, confirmed, and nothing else — no CV, no skills, no applications."""
    from apps.candidates.services import profile as profile_service

    profile_service.get_or_create_profile(user=user)
    report.had(f"bare candidate profile {user.email}")


def _staff(user, report: Report) -> None:
    """A platform moderator, through the role the staff screens check.

    Written directly because the product's grant path needs two superadmins
    to propose and approve, and a local demo has none. The row is exactly the
    one an approved grant writes; the audit entry says it came from the seed.
    """
    from apps.accounts.models import StaffRole
    from apps.audit import services as audit

    if StaffRole.objects.filter(user=user).exists():
        report.had("staff role")
        return
    StaffRole.objects.create(user=user, level=StaffRole.Level.MODERATOR, granted_at=timezone.now())
    audit.record(
        verb="staff.role_granted",
        actor=None,
        target=user,
        metadata={"level": "moderator", "reason": "seed_demo manual-testing persona"},
    )
    report.made(f"moderator role for {user.email}")


_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
