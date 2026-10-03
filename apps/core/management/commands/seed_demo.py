"""
A platform with people on it.

A recruitment product with three jobs and two companies cannot be judged, sold
or usability-tested: every screen is an empty state, matching has nothing to
match against, the pipeline board has no cards, and a candidate who signs up
concludes — correctly — that nobody is hiring here. This command fills the
database with a market: companies, recruiters, live postings, candidates with
real CVs, applications spread across the pipeline, and interviews that have
already happened.

**Everything goes through the domain services.** Rows written straight to the
tables would skip the events, the audit trail, the application counters and
the pipeline invariants, and the result would be a database that looks
populated and behaves wrongly the moment anyone clicks something. It is slower
this way, and it is the only version worth having.

**Demo accounts are obvious.** Every seeded user is on `@example.com`, which
is the reserved documentation domain and can never receive mail, and every one
shares a password that is printed on completion. Nothing here should ever exist
in production, so the command refuses to run when `DEBUG` is off unless it is
forced.

    python manage.py seed_demo
    python manage.py seed_demo --reset      # delete previous demo rows first
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services import identity
from apps.applications.models import Application
from apps.candidates.services import profile as profile_service
from apps.companies.models import Company, CompanyMembership, Role
from apps.companies.services import company as company_service
from apps.jobs.models import ExperienceLevel, Job, JobStatus, RemoteScope, Workplace
from apps.jobs.services import lifecycle as job_lifecycle
from apps.jobs.services import postings as job_postings
from apps.resumes.models import ResumeTemplate, VersionReason
from apps.resumes.services import documents as document_service
from apps.resumes.services import versions as version_service
from apps.taxonomy.models import Industry, JobCategory, Location, Skill

#: Every seeded account uses this, and the command prints it when it finishes.
#: A demo nobody can sign into is a database dump with extra steps.
DEMO_PASSWORD = "CareerAI!Demo24"  # noqa: S105 - a fixture password, printed on purpose

#: The reserved documentation domain (RFC 2606). Mail to it cannot be
#: delivered, so a demo dataset can never email a real person by accident.
DEMO_DOMAIN = "example.com"

#: A fixed seed. Two people running this command should be looking at the same
#: platform when they compare notes about it.
RANDOM_SEED = 20260910

#: The pipeline order from which a recruiter would realistically write to
#: somebody. Shortlisting is when outreach happens — that is the point at which
#: a person has read the application and decided to spend time on it.
OUTREACH_STAGE_ORDER = 30


COMPANIES: list[dict[str, Any]] = [
    {
        "name": "Fonepay",
        "tagline": "The payment rails behind most of Nepal's digital wallets.",
        "industry": "Financial services",
        "location": "Kathmandu, NP",
        "size_band": "201-500",
        "founded": "2015",
        "website": "https://fonepay.example.com",
        "about": [
            "Fonepay clears interbank payments for most of the wallets and mobile "
            "banking apps in Nepal. On a busy festival day that is a few million "
            "transactions, and none of them are allowed to go missing.",
            "Engineering is about forty people across payments, settlement and "
            "platform. We run Django and Go on our own hardware in Kathmandu with "
            "a warm standby in Singapore.",
        ],
        "recruiters": [("Anita Gurung", "anita.gurung"), ("Bikash Shrestha", "bikash.shrestha")],
    },
    {
        "name": "Himalaya Ledger Systems",
        "tagline": "Accounting software for businesses that outgrew spreadsheets.",
        "industry": "Software & IT",
        "location": "Lalitpur, NP",
        "size_band": "51-200",
        "founded": "2012",
        "website": "https://himalayaledger.example.com",
        "about": [
            "We build the accounting and inventory system used by around nine "
            "thousand small businesses across Nepal and northern India.",
            "The product is unglamorous and the reliability bar is absolute: our "
            "customers file their taxes out of it.",
        ],
        "recruiters": [("Sabina Maharjan", "sabina.maharjan")],
    },
    {
        "name": "Sajilo Health",
        "tagline": "Teleconsultation and pharmacy delivery across seven cities.",
        "industry": "Health technology",
        "location": "Kathmandu, NP",
        "size_band": "51-200",
        "founded": "2019",
        "website": "https://sajilohealth.example.com",
        "about": [
            "Sajilo connects patients to licensed doctors by video and delivers "
            "the prescription afterwards. Around 4,000 consultations a week.",
            "We are a small engineering team with an unusually large amount of "
            "regulatory surface, and we would rather ship slowly than get a "
            "prescription wrong.",
        ],
        "recruiters": [("Rojina Thapa", "rojina.thapa")],
    },
    {
        "name": "Everest Logistics",
        "tagline": "Freight and last-mile delivery, Kathmandu to the border.",
        "industry": "Logistics",
        "location": "Birgunj, NP",
        "size_band": "501-1000",
        "founded": "2008",
        "website": "https://everestlogistics.example.com",
        "about": [
            "We move about 60% of the containerised freight that crosses at "
            "Birgunj, and run a last-mile fleet in five cities.",
            "The technology team is new — four years old — and is replacing "
            "twenty years of paper with something drivers will actually use.",
        ],
        "recruiters": [("Deepak Yadav", "deepak.yadav")],
    },
    {
        "name": "Kora Studio",
        "tagline": "A product design studio working with teams across Asia.",
        "industry": "Media & marketing",
        "location": "Pokhara, NP",
        "size_band": "11-50",
        "founded": "2017",
        "website": "https://korastudio.example.com",
        "about": [
            "Fourteen people doing product design, brand and front-end build for "
            "clients in Nepal, Singapore and Australia.",
            "We work in the open: every project has a public design file and a "
            "written rationale, and we hire people who like explaining decisions.",
        ],
        "recruiters": [("Prakriti Rana", "prakriti.rana")],
    },
    {
        "name": "Ashadh Labs",
        "tagline": "Remote-first data platform team, hiring across the region.",
        "industry": "Software & IT",
        "location": "Bengaluru, IN",
        "size_band": "51-200",
        "founded": "2020",
        "website": "https://ashadhlabs.example.com",
        "about": [
            "We build the warehouse and analytics layer that four fintechs run "
            "their reporting on. Fully remote across India, Nepal and Sri Lanka.",
            "Everything is written down. If a decision only exists in a meeting, "
            "it did not happen.",
        ],
        "recruiters": [("Meera Iyer", "meera.iyer")],
    },
    {
        "name": "Gyan Academy",
        "tagline": "Online tuition for the SEE and +2 curriculum.",
        "industry": "Education",
        "location": "Bhaktapur, NP",
        "size_band": "51-200",
        "founded": "2016",
        "website": "https://gyanacademy.example.com",
        "about": [
            "Live classes and revision material for around 30,000 students "
            "preparing for the SEE and +2 examinations.",
            "Half the company is teachers, and the product team sits with them "
            "rather than in a separate building.",
        ],
        "recruiters": [("Nabin Adhikari", "nabin.adhikari")],
    },
    {
        "name": "Machhapuchhre Retail",
        "tagline": "Supermarket group modernising a twenty-store estate.",
        "industry": "E-commerce",
        "location": "Kathmandu, NP",
        "size_band": "1000+",
        "founded": "1998",
        "website": "https://machhapuchhre.example.com",
        "about": [
            "Twenty stores, an online arm that grew faster than anyone planned, "
            "and a stock system that was written in 2004.",
            "We are hiring a team to replace it without ever closing a till.",
        ],
        "recruiters": [("Sunita Basnet", "sunita.basnet")],
    },
]


#: (company, title, category, level, workplace, location, salary, skills, blurb)
#:
#: Written out rather than generated. A generated job advert reads like a
#: generated job advert, and the whole point of the dataset is that a candidate
#: browsing it believes the platform has a market on it.
JOBS: list[dict[str, Any]] = [
    # --- Fonepay ---------------------------------------------------------
    {
        "company": "Fonepay",
        "title": "Senior Backend Engineer — Settlement",
        "category": "Software engineering",
        "level": ExperienceLevel.SENIOR,
        "workplace": Workplace.HYBRID,
        "location": "Kathmandu, NP",
        "salary": (180000, 260000, "NPR", "month"),
        "skills": ["Python", "Django", "PostgreSQL", "Celery", "Redis"],
        "description": (
            "Settlement is the part of Fonepay that decides who owes whom at the "
            "end of the day. It runs nightly today and we are moving it to "
            "near-real-time without ever double-paying anyone.\n\n"
            "You would own the reconciliation service end to end: the schema, the "
            "queue, the deploy, and the pager when it goes wrong at 2am on the "
            "second day of Dashain."
        ),
        "responsibilities": [
            "Own the reconciliation service and its data model.",
            "Move settlement from a nightly batch to a streaming pipeline.",
            "Write the runbooks the on-call rota actually uses.",
        ],
        "requirements": [
            "Five or more years writing production Python.",
            "You have debugged a money bug in a live system and can describe it.",
            "Comfortable with Postgres beyond the ORM: locks, isolation, EXPLAIN.",
        ],
        "benefits": [
            "Provident fund and full family medical cover.",
            "Two days a week from home, fixed rather than negotiated each time.",
            "NPR 60,000 a year for training or conferences.",
        ],
    },
    {
        "company": "Fonepay",
        "title": "Site Reliability Engineer",
        "category": "Infrastructure and DevOps",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.ON_SITE,
        "location": "Kathmandu, NP",
        "salary": (150000, 210000, "NPR", "month"),
        "skills": ["Kubernetes", "Terraform", "Linux", "Prometheus", "AWS"],
        "description": (
            "We run our own hardware in Kathmandu because the regulator requires "
            "it, with a warm standby in Singapore. That makes reliability an "
            "engineering problem rather than a purchasing one.\n\n"
            "You would join a team of three keeping a payments platform up "
            "through load spikes that arrive on the festival calendar."
        ),
        "responsibilities": [
            "Run the Kubernetes estate and the observability stack on it.",
            "Lead incident reviews and make sure the actions actually land.",
            "Cut the failover drill from four hours to under thirty minutes.",
        ],
        "requirements": [
            "Three or more years operating Linux systems in production.",
            "Kubernetes in anger, not just in a tutorial.",
            "You write postmortems without blaming anyone.",
        ],
        "benefits": [
            "On-call allowance paid on top of salary.",
            "Provident fund.",
            "Medical cover for you and your dependants.",
        ],
    },
    {
        "company": "Fonepay",
        "title": "Fraud Analyst",
        "category": "Data and analytics",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.ON_SITE,
        "location": "Kathmandu, NP",
        "salary": (110000, 150000, "NPR", "month"),
        "skills": ["SQL", "Python", "Data analysis", "Statistics"],
        "description": (
            "Every declined transaction is a customer who could not pay for "
            "something, and every accepted fraudulent one is money gone. This "
            "role sits on that trade-off with real numbers attached to it."
        ),
        "responsibilities": [
            "Investigate flagged transactions and write up what actually happened.",
            "Tune the rule set and measure the effect on both error rates.",
            "Report monthly to the risk committee.",
        ],
        "requirements": [
            "Strong SQL. You should be able to answer a question without asking an engineer.",
            "Two or more years in fraud, risk or financial crime.",
        ],
        "benefits": ["Provident fund.", "Medical cover.", "Study support for CFE."],
    },
    {
        "company": "Fonepay",
        "title": "Engineering Manager — Platform",
        "category": "Software engineering",
        "level": ExperienceLevel.LEAD,
        "workplace": Workplace.HYBRID,
        "location": "Kathmandu, NP",
        "salary": (280000, 360000, "NPR", "month"),
        "skills": ["Leadership", "Python", "System design", "Communication"],
        "description": (
            "Eleven engineers across two teams, and a platform that everything "
            "else at Fonepay is built on. We are looking for someone who has "
            "done the job before and is honest about what they got wrong."
        ),
        "responsibilities": [
            "Run two teams: hiring, growth, delivery and the difficult conversations.",
            "Own the platform roadmap with the CTO.",
            "Keep writing enough code to stay credible, and not more than that.",
        ],
        "requirements": [
            "Two or more years managing engineers, including a promotion and a departure.",
            "A background as a backend engineer.",
        ],
        "benefits": ["Provident fund.", "Family medical cover.", "Leadership coaching budget."],
    },
    # --- Himalaya Ledger --------------------------------------------------
    {
        "company": "Himalaya Ledger Systems",
        "title": "Backend Engineer II",
        "category": "Software engineering",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.HYBRID,
        "location": "Lalitpur, NP",
        "salary": (120000, 170000, "NPR", "month"),
        "skills": ["Python", "Django", "PostgreSQL", "REST APIs"],
        "description": (
            "Our customers file their taxes out of this software, so the bar for "
            "correctness is higher than the bar for speed. You would work on the "
            "ledger and the tax engine that sits on top of it."
        ),
        "responsibilities": [
            "Build and maintain the ledger's public API.",
            "Keep the tax engine current as the rules change each fiscal year.",
            "Write tests that a finance person could read.",
        ],
        "requirements": [
            "Two or more years with Django or a comparable framework.",
            "Care about correctness in a way colleagues would describe as slightly excessive.",
        ],
        "benefits": [
            "Hybrid, three days in the office.",
            "Annual bonus tied to company results.",
            "Medical cover.",
        ],
    },
    {
        "company": "Himalaya Ledger Systems",
        "title": "QA Automation Engineer",
        "category": "Quality assurance",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.ON_SITE,
        "location": "Lalitpur, NP",
        "salary": (95000, 130000, "NPR", "month"),
        "skills": ["Selenium", "Python", "Test automation", "CI/CD"],
        "description": (
            "A release that breaks invoicing costs nine thousand businesses their "
            "morning. You would own the suite that stops that happening."
        ),
        "responsibilities": [
            "Own the regression suite and its runtime.",
            "Bring the release checklist down from two days to two hours.",
            "Sit with support and turn recurring tickets into tests.",
        ],
        "requirements": [
            "Two or more years in test automation.",
            "You can read the application's code, not just its screens.",
        ],
        "benefits": ["Medical cover.", "Annual bonus."],
    },
    {
        "company": "Himalaya Ledger Systems",
        "title": "Technical Support Specialist",
        "category": "Business and operations",
        "level": ExperienceLevel.ENTRY,
        "workplace": Workplace.ON_SITE,
        "location": "Lalitpur, NP",
        "salary": (45000, 65000, "NPR", "month"),
        "skills": ["Customer service", "SQL", "Communication", "Accounting"],
        "description": (
            "The first person a shopkeeper in Nepalgunj speaks to when their "
            "invoice will not print. Half the job is accounting, half is patience."
        ),
        "responsibilities": [
            "Answer support tickets and calls, in Nepali and English.",
            "Reproduce faults properly before passing them to engineering.",
            "Write the help articles for the questions that keep coming back.",
        ],
        "requirements": [
            "Some bookkeeping or accounting background.",
            "Clear written Nepali and English.",
        ],
        "benefits": [
            "Structured training in the first month.",
            "Medical cover.",
            "A real path into QA or implementation.",
        ],
    },
    # --- Sajilo Health ----------------------------------------------------
    {
        "company": "Sajilo Health",
        "title": "Full Stack Engineer",
        "category": "Software engineering",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.REMOTE,
        "location": "Kathmandu, NP",
        "remote_scope": RemoteScope.COUNTRY,
        "salary": (130000, 180000, "NPR", "month"),
        "skills": ["React", "TypeScript", "Node.js", "PostgreSQL"],
        "description": (
            "Consultations, prescriptions and delivery, in one product used by "
            "patients who are unwell and in a hurry. Every extra tap is a real "
            "cost to somebody.\n\n"
            "Remote within Nepal, with one week in Kathmandu each quarter."
        ),
        "responsibilities": [
            "Build patient-facing features from design through to production.",
            "Work directly with the clinical team on anything touching prescriptions.",
            "Keep the consultation flow working on a 3G connection.",
        ],
        "requirements": [
            "Three or more years across a JavaScript front end and a real back end.",
            "You have shipped something used by people who are not technologists.",
        ],
        "benefits": [
            "Fully remote within Nepal.",
            "Free consultations for you and your family.",
            "Medical cover.",
        ],
    },
    {
        "company": "Sajilo Health",
        "title": "Product Designer",
        "category": "Design and creative",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.HYBRID,
        "location": "Kathmandu, NP",
        "salary": (110000, 155000, "NPR", "month"),
        "skills": ["Figma", "User research", "Design systems", "Accessibility"],
        "description": (
            "Designing for people who are ill, worried, and often using a "
            "borrowed phone. This is not a portfolio-polish job — it is research, "
            "then flows, then sitting with the support queue."
        ),
        "responsibilities": [
            "Run research with patients, including people who are not confident with phones.",
            "Own the consultation and prescription flows.",
            "Keep the design system honest about accessibility.",
        ],
        "requirements": [
            "A portfolio with at least one case study showing research changing the design.",
            "Three or more years in product design.",
        ],
        "benefits": ["Hybrid.", "Research budget.", "Medical cover."],
    },
    {
        "company": "Sajilo Health",
        "title": "Data Engineer",
        "category": "Data and analytics",
        "level": ExperienceLevel.SENIOR,
        "workplace": Workplace.REMOTE,
        "location": "Kathmandu, NP",
        "remote_scope": RemoteScope.COUNTRY,
        "salary": (160000, 220000, "NPR", "month"),
        "skills": ["Python", "ETL", "PostgreSQL", "Airflow", "SQL"],
        "description": (
            "Clinical data with a regulator attached to it. You would build the "
            "pipeline that lets the clinical team answer questions without ever "
            "moving identifiable data somewhere it should not be."
        ),
        "responsibilities": [
            "Build and own the analytics pipeline.",
            "Design the de-identification layer with the compliance lead.",
            "Make the weekly clinical report something nobody assembles by hand.",
        ],
        "requirements": [
            "Four or more years in data engineering.",
            "You have worked under a data-protection regime and can talk about it.",
        ],
        "benefits": ["Fully remote within Nepal.", "Medical cover.", "Conference budget."],
    },
    # --- Everest Logistics ------------------------------------------------
    {
        "company": "Everest Logistics",
        "title": "Mobile Engineer (Android)",
        "category": "Software engineering",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.ON_SITE,
        "location": "Birgunj, NP",
        "salary": (110000, 150000, "NPR", "month"),
        "skills": ["Kotlin", "Android", "REST APIs", "Git"],
        "description": (
            "The driver app is used by six hundred people who are usually "
            "outdoors, often with no signal, and never in a position to read a "
            "help article. Offline-first is not a feature here, it is the "
            "premise."
        ),
        "responsibilities": [
            "Own the Android driver app.",
            "Make the offline sync reliable enough that drivers stop keeping paper.",
            "Spend a day a month in a truck.",
        ],
        "requirements": [
            "Two or more years of Android in Kotlin.",
            "You have handled a genuinely offline-first sync problem.",
        ],
        "benefits": [
            "Accommodation support for a relocation to Birgunj.",
            "Medical cover.",
            "Annual bonus.",
        ],
    },
    {
        "company": "Everest Logistics",
        "title": "Operations Analyst",
        "category": "Business and operations",
        "level": ExperienceLevel.ENTRY,
        "workplace": Workplace.ON_SITE,
        "location": "Birgunj, NP",
        "salary": (55000, 80000, "NPR", "month"),
        "skills": ["Excel", "SQL", "Data analysis", "Communication"],
        "description": (
            "Where is the freight, why is it late, and what did that cost. The "
            "answers exist in six systems and a WhatsApp group, and this role is "
            "how they become one number."
        ),
        "responsibilities": [
            "Build the daily operations report.",
            "Track fleet utilisation and border clearance times.",
            "Work with the yard supervisors, in person, most days.",
        ],
        "requirements": [
            "Very strong spreadsheets.",
            "Willing to learn SQL in the first three months — we will teach you.",
        ],
        "benefits": ["Training in SQL and reporting.", "Medical cover."],
    },
    {
        "company": "Everest Logistics",
        "title": "Warehouse Systems Lead",
        "category": "Infrastructure and DevOps",
        "level": ExperienceLevel.LEAD,
        "workplace": Workplace.ON_SITE,
        "location": "Birgunj, NP",
        "salary": (200000, 270000, "NPR", "month"),
        "skills": ["System design", "Linux", "PostgreSQL", "Leadership"],
        "description": (
            "Replacing a twenty-year-old warehouse system across five sites "
            "without stopping the freight. The hard part is the migration, not "
            "the software."
        ),
        "responsibilities": [
            "Own the replacement programme end to end.",
            "Lead four engineers and work daily with the warehouse managers.",
            "Cut over site by site, with a rollback that has been rehearsed.",
        ],
        "requirements": [
            "Experience replacing a live operational system.",
            "Five or more years engineering, some of it leading.",
        ],
        "benefits": ["Relocation and accommodation support.", "Annual bonus.", "Medical cover."],
    },
    # --- Kora Studio ------------------------------------------------------
    {
        "company": "Kora Studio",
        "title": "Front-End Developer",
        "category": "Software engineering",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.HYBRID,
        "location": "Pokhara, NP",
        "salary": (95000, 140000, "NPR", "month"),
        "skills": ["React", "TypeScript", "CSS", "Accessibility", "Figma"],
        "description": (
            "Client work, which means a new codebase every few months and a real "
            "deadline attached to each one. You would build what the design team "
            "draws, and push back when it cannot be built well."
        ),
        "responsibilities": [
            "Build client front ends to a standard that survives a handover.",
            "Own accessibility on every project — we test it before we invoice.",
            "Write the handover documentation.",
        ],
        "requirements": [
            "Three or more years of front-end work.",
            "Care about CSS as a craft, not as a thing to avoid.",
        ],
        "benefits": [
            "Four-day fortnight in the office, rest remote.",
            "Conference travel.",
            "A studio in Pokhara.",
        ],
    },
    {
        "company": "Kora Studio",
        "title": "Senior Product Designer",
        "category": "Design and creative",
        "level": ExperienceLevel.SENIOR,
        "workplace": Workplace.HYBRID,
        "location": "Pokhara, NP",
        "salary": (140000, 200000, "NPR", "month"),
        "skills": ["Figma", "Design systems", "User research", "Brand strategy"],
        "description": (
            "Leading design on two client engagements at a time, and writing the "
            "rationale that goes with them. We hire people who like being "
            "questioned about their choices."
        ),
        "responsibilities": [
            "Lead design on client engagements from kickoff to handover.",
            "Present work to client executives and defend it.",
            "Mentor two junior designers.",
        ],
        "requirements": [
            "Five or more years in product design.",
            "A portfolio where the writing is as good as the pictures.",
        ],
        "benefits": ["Hybrid.", "Conference travel.", "Profit share."],
    },
    {
        "company": "Kora Studio",
        "title": "Design Intern",
        "category": "Design and creative",
        "level": ExperienceLevel.INTERN,
        "workplace": Workplace.ON_SITE,
        "location": "Pokhara, NP",
        "salary": (25000, 25000, "NPR", "month"),
        "skills": ["Figma", "Communication"],
        "description": (
            "Six months, paid, with a named mentor and real client work from "
            "week three. We keep roughly half our interns."
        ),
        "responsibilities": [
            "Work on live client projects alongside a senior designer.",
            "Run one piece of research yourself, start to finish.",
        ],
        "requirements": [
            "A portfolio of some kind — coursework and personal projects count.",
            "No professional experience required.",
        ],
        "benefits": [
            "Paid, with a named mentor.",
            "Lunch provided.",
            "A real chance of a permanent offer.",
        ],
    },
    # --- Ashadh Labs ------------------------------------------------------
    {
        "company": "Ashadh Labs",
        "title": "Senior Data Engineer (Remote)",
        "category": "Data and analytics",
        "level": ExperienceLevel.SENIOR,
        "workplace": Workplace.REMOTE,
        "location": "Bengaluru, IN",
        "remote_scope": RemoteScope.REGION,
        "remote_countries": ["IN", "NP", "LK"],
        "salary": (2400000, 3600000, "INR", "year"),
        "skills": ["Python", "Spark", "SQL", "Airflow", "AWS"],
        "description": (
            "The warehouse four fintechs run their regulatory reporting on. If "
            "it is wrong, somebody files a wrong return.\n\n"
            "Fully remote across India, Nepal and Sri Lanka. We overlap four "
            "hours a day and write everything else down."
        ),
        "responsibilities": [
            "Own a domain of the warehouse: models, tests, freshness, cost.",
            "Review other people's pipelines properly.",
            "Write the design docs — they are how decisions happen here.",
        ],
        "requirements": [
            "Five or more years in data engineering.",
            "You are a good written communicator. This is not negotiable on a remote team.",
        ],
        "benefits": [
            "Fully remote across three countries.",
            "Annual company gathering, travel paid.",
            "Home office budget.",
        ],
    },
    {
        "company": "Ashadh Labs",
        "title": "Analytics Engineer",
        "category": "Data and analytics",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.REMOTE,
        "location": "Bengaluru, IN",
        "remote_scope": RemoteScope.REGION,
        "remote_countries": ["IN", "NP", "LK"],
        "salary": (1600000, 2400000, "INR", "year"),
        "skills": ["SQL", "dbt", "Data modelling", "Python"],
        "description": (
            "Between the raw data and the people asking questions of it. You "
            "would own the models the analysts actually query."
        ),
        "responsibilities": [
            "Own the dbt project and its documentation.",
            "Sit with analysts and turn their questions into models.",
            "Keep the semantic layer from becoming four hundred unused views.",
        ],
        "requirements": [
            "Three or more years of SQL-heavy work.",
            "dbt or an equivalent transformation framework.",
        ],
        "benefits": ["Fully remote.", "Home office budget.", "Learning stipend."],
    },
    {
        "company": "Ashadh Labs",
        "title": "Platform Engineer",
        "category": "Infrastructure and DevOps",
        "level": ExperienceLevel.SENIOR,
        "workplace": Workplace.REMOTE,
        "location": "Bengaluru, IN",
        "remote_scope": RemoteScope.REGION,
        "remote_countries": ["IN", "NP", "LK"],
        "salary": (2200000, 3200000, "INR", "year"),
        "skills": ["Terraform", "AWS", "Kubernetes", "Python", "CI/CD"],
        "description": (
            "The infrastructure the data platform runs on, and the developer "
            "experience of the twenty people using it."
        ),
        "responsibilities": [
            "Own the Terraform estate and the deployment pipeline.",
            "Get a pipeline from commit to production in under fifteen minutes.",
            "Keep the cloud bill explicable.",
        ],
        "requirements": [
            "Four or more years of infrastructure work on AWS.",
            "Terraform as a matter of routine.",
        ],
        "benefits": ["Fully remote.", "On-call allowance.", "Home office budget."],
    },
    # --- Gyan Academy -----------------------------------------------------
    {
        "company": "Gyan Academy",
        "title": "Physics Teacher (+2)",
        "category": "Education and training",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.HYBRID,
        "location": "Bhaktapur, NP",
        "salary": (70000, 100000, "NPR", "month"),
        "skills": ["Curriculum design", "Classroom management", "Communication"],
        "description": (
            "Live online classes of around sixty students, plus the recorded "
            "material that goes with them. Our students are preparing for an "
            "examination that decides a great deal, and they know it."
        ),
        "responsibilities": [
            "Teach four live sessions a week.",
            "Write and revise the +2 physics material.",
            "Review question banks with the academic lead.",
        ],
        "requirements": [
            "A master's degree in physics or a related field.",
            "Three or more years teaching +2 or equivalent.",
        ],
        "benefits": ["Hybrid — two days on campus.", "Festival bonus.", "Medical cover."],
    },
    {
        "company": "Gyan Academy",
        "title": "Product Manager — Learning",
        "category": "Product and project",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.HYBRID,
        "location": "Bhaktapur, NP",
        "salary": (130000, 180000, "NPR", "month"),
        "skills": ["Product management", "User research", "Data analysis", "Communication"],
        "description": (
            "Half the company is teachers. The product manager's job here is "
            "largely translation, in both directions, and the successful "
            "candidate will spend a lot of time in classrooms."
        ),
        "responsibilities": [
            "Own the student-facing roadmap.",
            "Run research with students and with teachers.",
            "Decide what not to build, and say why in writing.",
        ],
        "requirements": [
            "Three or more years in product.",
            "Comfortable with a metric that takes a term to move.",
        ],
        "benefits": ["Hybrid.", "Festival bonus.", "Medical cover."],
    },
    {
        "company": "Gyan Academy",
        "title": "Video Production Assistant",
        "category": "Design and creative",
        "level": ExperienceLevel.ENTRY,
        "workplace": Workplace.ON_SITE,
        "location": "Bhaktapur, NP",
        "salary": (35000, 50000, "NPR", "month"),
        "skills": ["Video editing", "Communication"],
        "description": (
            "We record about forty lessons a week. This role is the person who "
            "makes them watchable."
        ),
        "responsibilities": [
            "Edit recorded lessons to a weekly schedule.",
            "Run the studio during recordings.",
            "Keep the media library organised.",
        ],
        "requirements": [
            "Some editing experience, professional or otherwise.",
            "Reliable and organised — the schedule is unforgiving.",
        ],
        "benefits": ["Training on the studio kit.", "Festival bonus."],
    },
    # --- Machhapuchhre Retail ---------------------------------------------
    {
        "company": "Machhapuchhre Retail",
        "title": "Software Engineer — Point of Sale",
        "category": "Software engineering",
        "level": ExperienceLevel.MID,
        "workplace": Workplace.ON_SITE,
        "location": "Kathmandu, NP",
        "salary": (100000, 145000, "NPR", "month"),
        "skills": ["C#", ".NET", "SQL Server", "REST APIs"],
        "description": (
            "Twenty stores, four hundred tills, and a system written in 2004 "
            "that we are replacing one component at a time. Nothing may ever go "
            "down during trading hours."
        ),
        "responsibilities": [
            "Build the replacement point-of-sale services.",
            "Run the strangler migration alongside the old system.",
            "Be in a store on cutover nights.",
        ],
        "requirements": [
            "Three or more years with C# and .NET.",
            "You have worked alongside a legacy system rather than around it.",
        ],
        "benefits": ["Staff discount.", "Medical cover.", "Annual bonus."],
    },
    {
        "company": "Machhapuchhre Retail",
        "title": "Head of E-commerce",
        "category": "Sales and marketing",
        "level": ExperienceLevel.LEAD,
        "workplace": Workplace.HYBRID,
        "location": "Kathmandu, NP",
        "salary": (250000, 350000, "NPR", "month"),
        "skills": ["E-commerce", "Leadership", "Data analysis", "Content marketing"],
        "description": (
            "The online arm grew faster than the plan and is now a fifth of "
            "revenue, run by four people who are all doing three jobs. This role "
            "makes it a proper business unit."
        ),
        "responsibilities": [
            "Own the online P&L.",
            "Build the team out from four to about twelve.",
            "Fix the fulfilment promise before growing the marketing spend.",
        ],
        "requirements": [
            "Five or more years in e-commerce, some of it leading a team.",
            "You have owned a P&L.",
        ],
        "benefits": ["Hybrid.", "Performance bonus.", "Staff discount."],
    },
    {
        "company": "Machhapuchhre Retail",
        "title": "HR Officer",
        "category": "People and HR",
        "level": ExperienceLevel.ENTRY,
        "workplace": Workplace.ON_SITE,
        "location": "Kathmandu, NP",
        "salary": (55000, 75000, "NPR", "month"),
        "skills": ["Recruitment", "Communication", "Employee relations"],
        "description": (
            "Around 1,200 staff across twenty stores and a head office. This "
            "role covers hiring for the stores and the first line of employee "
            "relations."
        ),
        "responsibilities": [
            "Run store-level hiring end to end.",
            "Handle first-line employee relations cases.",
            "Keep the records the labour audit asks for.",
        ],
        "requirements": [
            "One or more years in HR.",
            "Clear Nepali and English, written and spoken.",
        ],
        "benefits": ["Staff discount.", "Medical cover.", "Support towards an HR qualification."],
    },
]


#: Demo candidates. Each gets a profile, a CV with real content, and a place in
#: somebody's pipeline.
CANDIDATES: list[dict[str, Any]] = [
    {
        "name": "Priya Sharma",
        "username": "priya.sharma",
        "seeking": "active",
        "open_to": ["full_time", "hybrid", "remote"],
        "headline": "Senior Backend Engineer",
        "location": "Kathmandu, NP",
        "summary": (
            "Backend engineer with six years on payment systems in Django and Go. "
            "Took a nightly settlement batch to near-real-time and cut "
            "reconciliation disputes by 40%."
        ),
        "skills": ["Python", "Django", "PostgreSQL", "Celery", "Redis", "Docker", "AWS"],
        "experience": [
            (
                "Senior Backend Engineer",
                "Fonepay",
                "2022 – Present",
                [
                    "Rebuilt the settlement pipeline on Celery and Postgres advisory locks, moving reconciliation from T+1 to under fifteen minutes.",
                    "Cut p99 checkout latency from 1.9s to 420ms by moving fraud scoring off the request path.",
                    "Led the migration of 40 services off a shared database with no maintenance window.",
                ],
            ),
            (
                "Backend Engineer",
                "Himalaya Ledger Systems",
                "2019 – 2022",
                [
                    "Built the double-entry ledger that records every transaction on the platform.",
                    "Introduced contract tests between the ledger and four consuming teams, ending a class of release-day incident.",
                ],
            ),
        ],
        "education": [("BE Computer Engineering", "Tribhuvan University", "2015 – 2019")],
        "applies": ["Senior Backend Engineer — Settlement", "Senior Data Engineer (Remote)"],
    },
    {
        "name": "Saugat Khadka",
        "username": "saugat.khadka",
        "seeking": "open",
        "open_to": ["full_time", "remote", "hybrid"],
        "headline": "Front-End Developer",
        "location": "Pokhara, NP",
        "summary": (
            "Front-end developer, four years, mostly React and TypeScript. I care "
            "about accessibility and I test with a screen reader before I open a "
            "pull request."
        ),
        "skills": ["React", "TypeScript", "CSS", "Accessibility", "Figma", "Git"],
        "experience": [
            (
                "Front-End Developer",
                "Kora Studio",
                "2022 – Present",
                [
                    "Built eleven client front ends, every one passing WCAG 2.2 AA before invoicing.",
                    "Wrote the studio's component library, now the starting point for every project.",
                ],
            ),
            (
                "Junior Developer",
                "Sajilo Health",
                "2021 – 2022",
                ["Worked on the patient booking flow and the pharmacy tracking screens."],
            ),
        ],
        "education": [("BSc Computer Science", "Pokhara University", "2017 – 2021")],
        "applies": ["Front-End Developer", "Full Stack Engineer"],
    },
    {
        "name": "Manisha Tuladhar",
        "username": "manisha.tuladhar",
        "seeking": "active",
        "open_to": ["full_time", "hybrid"],
        "headline": "Product Designer",
        "location": "Lalitpur, NP",
        "summary": (
            "Product designer with five years in health and education products. "
            "I run research myself rather than reading somebody else's summary."
        ),
        "skills": ["Figma", "User research", "Design systems", "Accessibility", "Prototyping"],
        "experience": [
            (
                "Product Designer",
                "Gyan Academy",
                "2021 – Present",
                [
                    "Redesigned the live class experience after sitting in on thirty sessions; completion rates up 18%.",
                    "Built and maintain the design system used by three product squads.",
                ],
            ),
            (
                "UX Designer",
                "Kora Studio",
                "2019 – 2021",
                ["Client work across fintech and retail, mostly research and flows."],
            ),
        ],
        "education": [("BA Design", "Kathmandu University", "2015 – 2019")],
        "applies": ["Product Designer", "Senior Product Designer"],
    },
    {
        "name": "Rabin Karki",
        "username": "rabin.karki",
        "discoverable": True,
        "seeking": "open",
        "open_to": ["full_time", "remote"],
        "headline": "DevOps Engineer",
        "location": "Kathmandu, NP",
        "summary": (
            "Infrastructure engineer, three years. Kubernetes, Terraform and a "
            "strong preference for boring, well-documented systems."
        ),
        "skills": ["Kubernetes", "Terraform", "AWS", "Linux", "CI/CD", "Prometheus"],
        "experience": [
            (
                "DevOps Engineer",
                "Ashadh Labs",
                "2022 – Present",
                [
                    "Moved twelve services onto a single Terraform estate, cutting environment drift incidents to zero.",
                    "Brought the deployment pipeline from 40 minutes to 11.",
                ],
            ),
            (
                "Systems Administrator",
                "Everest Logistics",
                "2021 – 2022",
                ["Ran the on-premise Linux fleet across three warehouse sites."],
            ),
        ],
        "education": [("BE Electronics and Communication", "Tribhuvan University", "2016 – 2020")],
        "applies": ["Site Reliability Engineer", "Platform Engineer"],
    },
    {
        "name": "Sita Rai",
        "username": "sita.rai",
        "discoverable": True,
        "seeking": "active",
        "open_to": ["full_time", "on_site", "hybrid"],
        "headline": "Data Analyst",
        "location": "Kathmandu, NP",
        "summary": (
            "Data analyst moving into data engineering. Strong SQL, and I have "
            "spent the last year building the pipelines behind my own reports."
        ),
        "skills": ["SQL", "Python", "Data analysis", "ETL", "Excel"],
        "experience": [
            (
                "Data Analyst",
                "Machhapuchhre Retail",
                "2022 – Present",
                [
                    "Own the daily sales and stock reporting across twenty stores.",
                    "Replaced a four-hour manual reconciliation with a scheduled pipeline.",
                ],
            ),
            (
                "Operations Analyst",
                "Everest Logistics",
                "2020 – 2022",
                ["Reported on fleet utilisation and border clearance times."],
            ),
        ],
        "education": [("BBA", "Kathmandu University", "2016 – 2020")],
        "applies": ["Analytics Engineer", "Fraud Analyst", "Data Engineer"],
    },
    {
        "name": "Arjun Basnet",
        "username": "arjun.basnet",
        "seeking": "active",
        "open_to": ["full_time", "internship", "on_site"],
        "headline": "Graduate Software Engineer",
        "location": "Bhaktapur, NP",
        "summary": (
            "Recent computer engineering graduate. Two internships, one open "
            "source contribution I am proud of, and no professional experience "
            "yet — which is why I am applying to junior roles rather than "
            "pretending otherwise."
        ),
        "skills": ["Python", "JavaScript", "Git", "SQL"],
        "experience": [
            (
                "Software Engineering Intern",
                "Himalaya Ledger Systems",
                "2025 – 2026",
                [
                    "Built the CSV import tool now used by around 600 customers a month.",
                    "Wrote the first integration tests for the invoicing module.",
                ],
            )
        ],
        "education": [("BE Computer Engineering", "Tribhuvan University", "2021 – 2025")],
        "applies": ["Technical Support Specialist", "Operations Analyst"],
    },
]


class Command(BaseCommand):
    help = "Fill the database with a realistic market: companies, jobs, candidates, applications."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete previously seeded demo rows before writing new ones.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even when DEBUG is off. There is no good reason to do this.",
        )
        parser.add_argument(
            "--personas-only",
            action="store_true",
            help=(
                "Only the manual-testing accounts (demo.candidate, demo.employer, "
                "demo.staff, ...), not the whole market. Idempotent."
            ),
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "seed_demo writes fictional companies, jobs and people. "
                "It refuses to run with DEBUG off; pass --force if you are certain."
            )

        random.seed(RANDOM_SEED)

        if options["personas_only"]:
            # Not combined with `--reset`. The personas cannot be hard-deleted
            # and should not be: a finalized CV version is immutable at the
            # database (resumes 0003), an application protects the version it
            # was sent with, and verification evidence is protected on purpose
            # (D-02). Instead every run converges the personas in place.
            if options["reset"]:
                raise CommandError(
                    "--personas-only does not combine with --reset. Persona rows are "
                    "updated in place on every run; nothing needs deleting."
                )
            self._personas()
            return

        if options["reset"]:
            self._reset()

        self._ensure_prerequisites()

        companies = self._companies()
        jobs = self._jobs(companies)
        self._assign_recruiters(jobs)
        candidates = self._candidates()
        applications = self._applications(candidates, jobs)
        self._advance_pipeline(applications)
        self._conversations(applications)
        self._deliver()
        self._analytics()
        self._personas(summary=False)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(f"  companies    {len(companies)}")
        self.stdout.write(f"  jobs         {len(jobs)} published")
        self.stdout.write(f"  candidates   {len(candidates)}")
        self.stdout.write(f"  applications {len(applications)}")
        self.stdout.write("")
        self.stdout.write(f"  Every demo account signs in with:  {DEMO_PASSWORD}")
        self.stdout.write(f"  Candidate:  priya.sharma@{DEMO_DOMAIN}")
        self.stdout.write(f"  Recruiter:  anita.gurung@{DEMO_DOMAIN}")
        self._persona_summary()

    # --- manual-testing personas -----------------------------------------

    def _personas(self, *, summary: bool = True) -> None:
        """The fixed accounts the manual-testing handoff names (DEMO-01)."""
        from apps.core import demo_personas

        report = demo_personas.seed(write=self.stdout.write)
        for line in report.skipped:
            self.stderr.write(f"  persona step skipped: {line}")
        if summary:
            self.stdout.write(self.style.SUCCESS("Manual-testing personas ready."))
            self._persona_summary()

    def _persona_summary(self) -> None:
        from apps.core import demo_personas as personas

        self.stdout.write("")
        self.stdout.write(
            "  Manual-testing accounts - LOCAL DEMO ONLY (password for all: "
            + personas.PASSWORD
            + ")"
        )
        for letter, email, purpose in personas.ACCOUNTS:
            self.stdout.write(f"    {letter}  {email:<34} {purpose}")

    # --- prerequisites ----------------------------------------------------

    def _ensure_prerequisites(self) -> None:
        """The catalogues the demo rows point at.

        Called rather than documented, because "run these four commands first"
        is an instruction somebody will skip and then report as a bug.
        """
        from django.core.management import call_command

        if not Skill.objects.exists():
            call_command("seed_taxonomy", verbosity=0)
        if not ResumeTemplate.objects.exists():
            call_command("seed_resume_templates", "--quiet-summary")
        call_command("seed_pipeline_stages", verbosity=0)

        # Without a rate, a job priced in rupees and a job priced in Indian
        # rupees cannot be compared, so the salary sort silently drops half the
        # board and the "minimum salary" filter matches nothing. Indicative
        # figures, marked as such, and skipped if somebody has loaded real ones.
        from apps.taxonomy.models import FxRate

        for base, quote, rate in (("INR", "NPR", "1.60"), ("USD", "NPR", "141.50")):
            if not FxRate.objects.filter(base=base, quote=quote).exists():
                call_command(
                    "load_fx_rates",
                    f"--pair={base}:{quote}",
                    f"--rate={rate}",
                    "--source=demo-indicative",
                    verbosity=0,
                )

    def _reset(self) -> None:
        """Remove the previous demo dataset.

        Scoped by the reserved email domain rather than by a flag column: the
        demo users own everything the demo created, and deleting them cascades
        to their profiles, CVs and applications. Companies are matched by slug
        so a company whose owner was removed by hand still goes.
        """
        emails = [f"{c['username']}@{DEMO_DOMAIN}" for c in CANDIDATES]
        emails += [
            f"{username}@{DEMO_DOMAIN}"
            for company in COMPANIES
            for _name, username in company["recruiters"]
        ]

        users = User.objects.filter(email__in=emails)
        removed_users = users.count()
        users.delete()

        slugs = [_slugify(company["name"]) for company in COMPANIES]
        companies = Company.objects.filter(slug__in=slugs)
        removed_companies = companies.count()
        companies.delete()

        self.stdout.write(f"reset: {removed_users} users, {removed_companies} companies removed")

    # --- companies --------------------------------------------------------

    def _companies(self) -> dict[str, Company]:
        built: dict[str, Company] = {}

        for spec in COMPANIES:
            slug = _slugify(spec["name"])
            existing = Company.objects.filter(slug=slug).first()
            if existing is not None:
                built[spec["name"]] = existing
                self._recruiters(existing, spec["recruiters"])
                continue

            owner_name, owner_username = spec["recruiters"][0]
            owner = self._user(owner_name, owner_username, intent="employer")

            company = company_service.create_company(
                actor=owner,
                name=spec["name"],
                website=spec["website"],
                tagline=spec["tagline"],
                size_band=spec["size_band"],
                founded=spec["founded"],
                about=spec["about"],
                industry=_industry(spec["industry"]),
                location=_location(spec["location"]),
            )
            built[spec["name"]] = company
            self._recruiters(company, spec["recruiters"][1:])
            self.stdout.write(f"company: {company.name}")

        return built

    def _publisher(self, company: Company) -> CompanyMembership:
        """A member of this company who is allowed to publish.

        Owners first. `filter(...).first()` returned whichever row the database
        felt like, and a recruiter without the grant makes `publish` refuse —
        which, in a seeder, surfaces as a company whose board is mysteriously
        half empty.
        """
        owner = CompanyMembership.objects.filter(company=company, role=Role.OWNER).first()
        return owner or CompanyMembership.objects.filter(company=company).first()

    def _assign_recruiters(self, jobs: list[Job]) -> None:
        """Put each company's recruiters on its postings (decision D-13).

        Recruiters reach applicants only on jobs they are assigned to, so a demo
        recruiter with no assignments opens an empty board and reads as broken.
        Idempotent: `assign` is get-or-create.
        """
        for job in jobs:
            publisher = self._publisher(job.company)
            for membership in CompanyMembership.objects.active().filter(
                company=job.company, role=Role.RECRUITER
            ):
                job_postings.assign(job=job, actor=publisher.user, membership=membership)

    def _recruiters(self, company: Company, people: list[tuple[str, str]]) -> None:
        for name, username in people:
            user = self._user(name, username, intent="employer")
            CompanyMembership.objects.get_or_create(
                company=company,
                user=user,
                defaults={"role": Role.RECRUITER, "joined_at": timezone.now()},
            )

    def _user(self, full_name: str, username: str, *, intent: str = "candidate") -> User:
        """One demo account, created the way a real sign-up creates one.

        Through `identity.register` rather than `create_user`, so the account
        gets its primary `EmailAddress`, its signup consent row and its
        `user.registered` event. An account missing those is one that behaves
        differently from every real account the moment anything touches
        verification or email — which is precisely the sort of difference a
        demo dataset exists to avoid.

        The address is then marked verified: a demo where nobody can be
        emailed, and every account is stuck behind a verification link that
        will never arrive, is a demo nobody can use.
        """
        email = f"{username}@{DEMO_DOMAIN}"
        user = User.objects.filter(email=email).first()
        if user is not None:
            return user

        user = identity.register(
            email=email,
            password=DEMO_PASSWORD,
            full_name=full_name,
            signup_intent=intent,
        )

        first, _, last = full_name.partition(" ")
        user.first_name, user.last_name = first, last
        user.save(update_fields=["first_name", "last_name"])

        user.email_addresses.filter(is_primary=True).update(verified_at=timezone.now())
        return user

    # --- jobs -------------------------------------------------------------

    def _jobs(self, companies: dict[str, Company]) -> list[Job]:
        published: list[Job] = []
        now = timezone.now()

        for spec in JOBS:
            company = companies[spec["company"]]
            membership = self._publisher(company)
            actor = membership.user

            existing = Job.objects.filter(company=company, title=spec["title"]).first()
            if existing is not None:
                # Finish a job an earlier run left half-made. A run that failed
                # part way through — the first one here did, on a service
                # signature — leaves a created-but-unpublished posting, and
                # this branch used to accept it as done. It then stayed a draft
                # through every later run: invisible on the board, unreachable
                # from search, and impossible to apply to.
                if existing.status == JobStatus.DRAFT:
                    job_lifecycle.publish(job=existing, actor=actor, membership=membership)
                    self.stdout.write(f"job: published the draft {existing.title}")
                published.append(existing)
                continue

            minimum, maximum, currency, period = spec["salary"]

            job = job_postings.create_job(
                company=company,
                actor=actor,
                title=spec["title"],
                description=spec["description"],
                responsibilities=spec["responsibilities"],
                requirements=spec["requirements"],
                benefits=spec["benefits"],
                # `_id`, not the instance: the posting service's editable-field
                # list names `category_id` and `location_id`, and anything not
                # on that list is dropped without a word. Passing the objects
                # produced 25 published jobs that all said "Location not given".
                category_id=_category_id(spec["category"]),
                location_id=_location_id(spec["location"]),
                workplace=spec["workplace"],
                remote_scope=spec.get("remote_scope", RemoteScope.NONE),
                remote_countries=spec.get("remote_countries", []),
                experience_level=spec["level"],
                salary_min=minimum,
                salary_max=maximum,
                currency=currency,
                period=period,
            )

            for position, name in enumerate(spec["skills"]):
                try:
                    # The first three are what the role genuinely requires; the
                    # rest are preferred. A posting where every skill is
                    # mandatory is a posting nobody qualified applies to.
                    job_postings.add_skill(
                        job=job,
                        actor=actor,
                        text=name,
                        requirement="required" if position < 3 else "preferred",
                    )
                except Exception as exc:  # noqa: BLE001 - an unresolved skill is not fatal
                    self.stderr.write(f"  skill skipped ({name}): {exc}")

            job_lifecycle.publish(job=job, actor=actor, membership=membership)

            # Spread the publication dates over the last six weeks. A board
            # where every posting says "today" reads as a fixture, and the
            # "posted 3 days ago" line is one of the first things a candidate
            # uses to judge whether a platform is alive.
            # Demo spread, not security: `random` is right here.
            job.published_at = now - timedelta(
                days=random.randint(1, 42),  # noqa: S311
                hours=random.randint(0, 23),  # noqa: S311
            )
            job.views_count = random.randint(18, 640)  # noqa: S311
            job.save(update_fields=["published_at", "views_count"])

            published.append(job)
            self.stdout.write(f"job: {job.title} @ {company.name}")

        return published

    # --- candidates -------------------------------------------------------

    def _candidates(self) -> list[dict[str, Any]]:
        built: list[dict[str, Any]] = []

        for spec in CANDIDATES:
            user = self._user(spec["name"], spec["username"])
            profile = profile_service.get_or_create_profile(user=user)

            profile_service.update_basics(
                profile=profile,
                actor=user,
                headline=spec["headline"],
                summary=spec["summary"],
                location_id=_location_id(spec["location"]),
            )
            profile.refresh_from_db()

            document = profile.resume_documents.alive().first()
            if document is None:
                document = document_service.create_document(
                    profile=profile,
                    actor=user,
                    name=f"{spec['name']} CV",
                    content=_cv_content(spec),
                )

            # Finalize unconditionally. `finalize` returns the current version
            # unchanged when nothing has moved, so this is idempotent — and a
            # document left with no version is the state that made the first
            # run of this command hand `None` to the apply service.
            version = version_service.finalize(
                document=document, actor=user, reason=VersionReason.SAVED
            )

            self._profile_skills(profile=profile, user=user, names=spec["skills"])
            self._profile_history(profile=profile, user=user, spec=spec)
            self._availability(profile=profile, user=user, spec=spec)

            built.append({"spec": spec, "user": user, "profile": profile, "version": version})
            self.stdout.write(f"candidate: {spec['name']}")

        return built

    def _profile_skills(self, *, profile, user, names: list[str]) -> None:
        """The candidate's skills, on the profile as well as on the CV.

        Matching and candidate search both read `CandidateSkill`, not the CV —
        the profile is canon for skills (ADR-A46). Seeding the CV alone
        produced six candidates whom no job matched and no recruiter search
        found, on a platform whose headline feature is matching.
        """
        from apps.candidates.services import skills as skill_service

        if profile.skills.exists():
            return

        for name in names:
            try:
                skill_service.add_skill(profile=profile, actor=user, text=name)
            except Exception as exc:  # noqa: BLE001 - an unresolved skill is not fatal
                self.stderr.write(f"  profile skill skipped ({name}): {exc}")

    def _profile_history(self, *, profile, user, spec: dict[str, Any]) -> None:
        """Work history and education on the profile, not only on the CV.

        The matcher scores experience from `profile.experience_months`, which
        is derived from these rows. Seeding the CV alone left every candidate
        at "0 months against a band asking for about 4 years" — a fifth of the
        match score, zeroed, for six people who each have years of history
        written on the CV beside it.
        """
        from apps.candidates.services import sections as section_service

        if profile.experiences.exists():
            return

        for title, org, dates, bullets in spec["experience"]:
            start, end, current = _period(dates)
            try:
                section_service.add_entry(
                    profile=profile,
                    actor=user,
                    section="experience",
                    title=title,
                    org_name=org,
                    start_date=start,
                    end_date=end,
                    is_current=current,
                    summary=" ".join(bullets)[:600],
                )
            except Exception as exc:  # noqa: BLE001 - one bad row is not fatal
                self.stderr.write(f"  experience skipped ({title}): {exc}")

        for title, org, dates in spec["education"]:
            start, end, _current = _period(dates)
            try:
                section_service.add_entry(
                    profile=profile,
                    actor=user,
                    section="education",
                    institution=org,
                    qualification=title,
                    start_date=start,
                    end_date=end,
                )
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"  education skipped ({title}): {exc}")

        profile.refresh_from_db()

    def _availability(self, *, profile, user, spec: dict[str, Any]) -> None:
        """Whether they are looking, and what they will take.

        Without it every candidate reads as "not stated", the availability
        filter on candidate search matches nobody, and the profile sits at a
        completeness that keeps them out of the search index entirely.
        """
        from apps.candidates.services import profile as profile_service

        profile_service.update_seeking(
            profile=profile,
            actor=user,
            seeking=spec.get("seeking", "active"),
            open_to=spec.get("open_to", ["full_time"]),
        )

        # A couple of them choose to be discoverable, which is roughly the real
        # ratio and is the only thing that puts anybody in a recruiter's search
        # results. The rest stay on the default, where an employer finds them
        # only by having received their application — which is the whole point
        # of the distinction and worth having represented in the data.
        if spec.get("discoverable"):
            profile_service.update_privacy(profile=profile, actor=user, visibility="everyone")

        profile.refresh_from_db()

    # --- applications -----------------------------------------------------

    def _applications(self, candidates: list[dict[str, Any]], jobs: list[Job]) -> list[Application]:
        from apps.applications.services import apply as apply_service

        by_title = {job.title: job for job in jobs}
        created: list[Application] = []
        existing: list[Application] = []

        for row in candidates:
            for title in row["spec"]["applies"]:
                job = by_title.get(title)
                if job is None or job.status != JobStatus.PUBLISHED:
                    continue

                already = Application.objects.filter(job=job, candidate=row["profile"]).first()
                if already is not None:
                    # Kept, not skipped. Every later step — moving people
                    # through the pipeline, opening a conversation — takes this
                    # list, and returning only the *new* rows meant a second
                    # run of the seed did nothing at all to a database that
                    # already had applications in it.
                    existing.append(already)
                    continue
                try:
                    application = apply_service.apply_to_job(
                        job=job,
                        profile=row["profile"],
                        actor=row["user"],
                        resume_version=row["version"],
                    )
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the seed
                    self.stderr.write(f"  application skipped ({title}): {exc}")
                    continue

                created.append(application)

        self.stdout.write(f"applications: {len(created)} new, {len(existing)} already there")
        return [*created, *existing]

    #: What a recruiter actually writes to somebody they are moving forward.
    #: Two of them, so a demo shows a thread rather than a single bubble.
    OUTREACH = (
        "Hi {first} — thanks for applying for {job}. Your {topic} experience is "
        "close to what this team is doing, and I would like to set up a first "
        "conversation. Are you free at any point next week?",
        "One more thing: the first conversation is with the hiring manager and "
        "runs about forty-five minutes. Nothing to prepare — it is mostly about "
        "what you have built and what you want to do next.",
    )

    def _conversations(self, applications: list[Application]) -> None:
        """A recruiter writing to the candidates they have moved forward.

        Messaging is the part of the product a candidate judges an employer by,
        and an empty inbox demonstrates nothing. Sent through the real service,
        so the visibility rule that makes cold outreach impossible (INV-4) is
        the thing being exercised rather than bypassed.
        """
        from apps.messaging.models import SenderKind
        from apps.messaging.services import conversations as messaging

        sent = 0
        for application in applications:
            stage = application.stage
            if stage is None or stage.order < OUTREACH_STAGE_ORDER:
                continue

            company = application.job.company
            membership = self._publisher(company)
            if membership is None:
                continue

            try:
                thread = messaging.open_conversation(
                    company=company,
                    candidate=application.candidate,
                    actor=membership.user,
                    membership=membership,
                    job=application.job,
                    application=application,
                    subject=application.job.title,
                )
                if thread.messages.exists():
                    continue

                first = (application.candidate.user.full_name or "there").split(" ")[0]
                # A skill, not the headline: "your work on senior backend
                # engineer" is not a sentence anybody has ever written.
                skill = application.candidate.skills.select_related("skill").first()
                topic = skill.skill.name if skill else "the"
                for body in self.OUTREACH:
                    messaging.send(
                        conversation=thread,
                        actor=membership.user,
                        sender_kind=SenderKind.COMPANY,
                        body=body.format(first=first, job=application.job.title, topic=topic),
                    )
                sent += 1
            except Exception as exc:  # noqa: BLE001 - one refused thread is not fatal
                self.stderr.write(f"  message skipped: {exc}")

        self.stdout.write(f"conversations: {sent}")

    def _deliver(self) -> None:
        """Drain the outbox and run the fan-out, so notifications exist.

        Both are long-running processes in a real deployment — `outbox_worker`
        and the fan-out beat task — and neither runs on a laptop. Without them
        a seeded candidate whose application was moved and whose interview was
        booked opens the bell to "Nothing has arrived yet", which is the one
        screen in the product that is *supposed* to prove things happened.

        Bounded rather than looped forever: a seed that cannot finish because
        an event keeps failing is worse than a seed with a few undelivered
        events in it.
        """
        from apps.notifications.services import fanout, outbox

        drained = 0
        for _ in range(20):
            batch = outbox.drain()
            count = batch if isinstance(batch, int) else batch.get("processed", 0)
            drained += count
            if not count:
                break

        delivered = 0
        for _ in range(20):
            result = fanout.run_due(limit=200)
            delivered += result.get("delivered", 0)
            if not result.get("chunks"):
                break

        self.stdout.write(f"notifications: {drained} events drained, {delivered} delivered")

    def _analytics(self) -> None:
        """Roll the analytics up so the employer's numbers are not empty.

        The rollups are a nightly job, which is right in production and wrong
        in a dataset somebody is about to look at: a recruiter opening
        Analytics on a seeded platform was told to come back "the morning after
        your first applications arrive", beside a board with seven live
        applications on it.
        """
        from apps.analytics.services import rollups

        counts = rollups.backfill(days=45)
        self.stdout.write(f"analytics: {counts}")

    def _advance_pipeline(self, applications: list[Application]) -> None:
        """Put the applications somewhere other than the first column.

        A pipeline board with every card in "Applied" tells a recruiter nothing
        about whether the board works. These are moved through the real service
        so the stage history and the events exist too.
        """
        from apps.applications.services import pipeline as pipeline_service

        for index, application in enumerate(applications):
            if index % 3 == 0:
                continue  # some stay new — that is what a real board looks like

            company = application.job.company
            stages = pipeline_service.stages_for(company)
            if len(stages) < 3:
                continue

            target = stages[min(1 + index % 3, len(stages) - 2)]
            membership = self._publisher(company)
            if membership is None:
                continue

            try:
                pipeline_service.move(
                    application=application,
                    actor=membership.user,
                    membership=membership,
                    stage=target,
                )
            except Exception as exc:  # noqa: BLE001 - a refused move is not fatal here
                self.stderr.write(f"  move skipped: {exc}")


# --- lookups ---------------------------------------------------------------


def _period(dates: str) -> tuple[Any, Any, bool]:
    """ "2022 – Present" as two dates and a flag.

    The CV writes a period the way a person does; the profile stores it the way
    a database does, because that is what the matcher can arithmetic on.
    """
    import datetime as dt
    import re

    spans = re.findall(r"(?:19|20)\d{2}", dates)
    current = "present" in dates.lower()

    start = dt.date(int(spans[0]), 1, 1) if spans else None
    end = None
    if not current and len(spans) > 1:
        end = dt.date(int(spans[1]), 12, 31)
    return start, end, current


def _slugify(name: str) -> str:
    from django.utils.text import slugify

    return slugify(name)[:80]


def _industry(name: str) -> Industry | None:
    return Industry.objects.filter(name=name).first()


def _category(name: str) -> JobCategory | None:
    return JobCategory.objects.filter(name=name).first()


def _category_id(name: str) -> int | None:
    category = _category(name)
    return category.pk if category else None


def _location(label: str) -> Location | None:
    city, _, country = label.partition(", ")
    return Location.objects.filter(city=city, country=country).first()


def _location_id(label: str) -> int | None:
    location = _location(label)
    return location.pk if location else None


def _skill(name: str) -> Skill | None:
    return Skill.objects.filter(name__iexact=name).first()


def _cv_content(spec: dict[str, Any]) -> dict[str, Any]:
    """One candidate's CV, in the shape the builder and the exporters read."""
    sections: list[dict[str, Any]] = [
        {
            "id": "sec-summary",
            "key": "summary",
            "kind": "text",
            "title": "Summary",
            "visible": True,
            "order": 1,
            "text": spec["summary"],
        },
        {
            "id": "sec-experience",
            "key": "experience",
            "kind": "entries",
            "title": "Experience",
            "visible": True,
            "order": 2,
            "entries": [
                {
                    "id": f"exp-{index}",
                    "title": title,
                    "org": org,
                    "dates": dates,
                    "bullets": bullets,
                }
                for index, (title, org, dates, bullets) in enumerate(spec["experience"])
            ],
        },
        {
            "id": "sec-skills",
            "key": "skills",
            "kind": "list",
            "title": "Skills",
            "visible": True,
            "order": 3,
            "items": spec["skills"],
        },
        {
            "id": "sec-education",
            "key": "education",
            "kind": "entries",
            "title": "Education",
            "visible": True,
            "order": 4,
            "entries": [
                {"id": f"edu-{index}", "title": title, "org": org, "dates": dates, "bullets": []}
                for index, (title, org, dates) in enumerate(spec["education"])
            ],
        },
    ]

    username = spec["username"]
    return {
        "contact": {
            "name": spec["name"],
            "headline": spec["headline"],
            "email": f"{username}@{DEMO_DOMAIN}",
            "phone": "+977 98" + str(random.randint(10000000, 99999999)),  # noqa: S311 - fake demo number
            "location": spec["location"].replace(", NP", ", Nepal"),
        },
        "summary": spec["summary"],
        "sections": sections,
    }
