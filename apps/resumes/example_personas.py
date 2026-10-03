"""
Worked examples for different kinds of CV (docs/BRANDING.md, CP-2).

The gallery and the comparison keep one shared example (`examples.py`),
because there only the layout should vary. A template's own page, and "start
from the example", show somebody the template was designed for: a nurse in
the plain layout, a designer in the portfolio one, a lecturer in the academic
one.

Every person and every organisation here is fictional, and named so a reader
can tell ("(fictional)" is not printed, the organisations simply do not
exist). Nothing claims a figure nobody could check: where a line has a number
it is the kind a person would know about their own work (team size, years,
hours), not a percentage improvement. Dates use an en dash.
"""

from __future__ import annotations

import copy
from typing import Any

from .content import CONTENT_SCHEMA_VERSION


def _entries(rows: list[tuple[str, str, str, list[str]]]) -> list[dict[str, Any]]:
    return [
        {"title": title, "org": org, "dates": dates, "bullets": bullets}
        for title, org, dates, bullets in rows
    ]


def _cv(
    *,
    name: str,
    headline: str,
    email: str,
    phone: str,
    location: str,
    summary: str,
    experience: list[tuple[str, str, str, list[str]]],
    education: list[tuple[str, str, str, list[str]]],
    skills: list[str],
    extra: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sections: list[dict[str, Any]] = [
        {"key": "summary", "kind": "text", "title": "Summary", "visible": True, "text": summary},
        {
            "key": "experience",
            "kind": "entries",
            "title": "Experience",
            "visible": True,
            "entries": _entries(experience),
        },
        {"key": "skills", "kind": "list", "title": "Skills", "visible": True, "items": skills},
        {
            "key": "education",
            "kind": "entries",
            "title": "Education",
            "visible": True,
            "entries": _entries(education),
        },
        *(extra or []),
    ]
    for order, section in enumerate(sections):
        section["order"] = order
    return {
        "schema_version": CONTENT_SCHEMA_VERSION,
        "contact": {
            "name": name,
            "headline": headline,
            "email": email,
            "phone": phone,
            "location": location,
        },
        "summary": summary,
        "sections": sections,
    }


NURSE = _cv(
    name="Sabina Thapa",
    headline="Staff Nurse",
    email="sabina.thapa@example.com",
    phone="+977 9841 000 101",
    location="Lalitpur, Nepal",
    summary=(
        "Registered nurse with five years on a busy medical ward. Calm with acutely "
        "unwell patients and their families, and the person new staff are paired with "
        "in their first weeks."
    ),
    experience=[
        (
            "Staff Nurse",
            "Valley District Hospital",
            "2021 – present",
            [
                "Care for up to eight patients a shift on a 30-bed medical ward.",
                "Give and record IV medication and blood transfusions to hospital policy.",
                "Mentor newly qualified nurses through their first three months.",
            ],
        ),
        (
            "Nursing Officer",
            "Hillside Community Clinic",
            "2019 – 2021",
            [
                "Ran the weekly immunisation clinic with two health assistants.",
                "Kept patient records up to date in the clinic's paper and digital systems.",
            ],
        ),
    ],
    education=[
        ("BSc Nursing", "Valley Institute of Health Sciences", "2015 – 2019", []),
    ],
    skills=["Patient assessment", "IV therapy", "Wound care", "Triage", "Record keeping"],
    extra=[
        {
            "key": "certifications",
            "kind": "list",
            "title": "Registration",
            "visible": True,
            "items": ["Registered with the nursing council (licence number available on request)"],
        },
        {
            "key": "languages",
            "kind": "list",
            "title": "Languages",
            "visible": True,
            "items": ["Nepali (native)", "English (fluent)", "Newari (conversational)"],
        },
    ],
)

GRADUATE = _cv(
    name="Aarav Shrestha",
    headline="Graduate Software Developer",
    email="aarav.shrestha@example.com",
    phone="+977 9801 000 202",
    location="Kathmandu, Nepal",
    summary=(
        "Recent BIT graduate looking for a first developer role. Comfortable with "
        "Python and JavaScript, and used to working in small teams from my final-year "
        "project and a three-month internship."
    ),
    experience=[
        (
            "Software Development Intern",
            "Bluepine Labs",
            "Jun 2025 – Aug 2025",
            [
                "Fixed bugs and wrote tests for the company's booking web app.",
                "Joined daily stand-ups and code reviews with a team of five.",
            ],
        ),
    ],
    education=[
        (
            "Bachelor in Information Technology",
            "Valley College of Technology",
            "2021 – 2025",
            ["Final-year project: a class attendance app used by my department for one term."],
        ),
    ],
    skills=["Python", "JavaScript", "Django", "HTML and CSS", "Git", "SQL"],
    extra=[
        {
            "key": "projects",
            "kind": "entries",
            "title": "Projects",
            "visible": True,
            "entries": _entries(
                [
                    (
                        "Class attendance app",
                        "Final-year project",
                        "2024 – 2025",
                        [
                            "Built the Django back end and the teacher-facing pages with two classmates."
                        ],
                    ),
                ]
            ),
        },
    ],
)

DESIGNER = _cv(
    name="Nisha Gurung",
    headline="Product Designer",
    email="nisha.gurung@example.com",
    phone="+977 9860 000 303",
    location="Pokhara, Nepal",
    summary=(
        "Product designer with four years on mobile and web apps. I do the research, "
        "the flows and the final screens, and I work closely with developers until a "
        "feature ships."
    ),
    experience=[
        (
            "Product Designer",
            "Lakeside Digital",
            "2022 – present",
            [
                "Lead design for a travel booking app, from interviews with guides to final screens.",
                "Built and maintain the company's shared component library in Figma.",
            ],
        ),
        (
            "Junior Designer",
            "Mountain Print Studio",
            "2020 – 2022",
            ["Designed brochures, menus and social posts for local hotels and cafes."],
        ),
    ],
    education=[("BFA Graphic Design", "Valley School of Fine Arts", "2016 – 2020", [])],
    skills=["User research", "Figma", "Prototyping", "Design systems", "Usability testing"],
    extra=[
        {
            "key": "projects",
            "kind": "entries",
            "title": "Selected work",
            "visible": True,
            "entries": _entries(
                [
                    (
                        "Trekking permit flow",
                        "Lakeside Digital",
                        "2023",
                        ["Redesigned the permit application so guides could finish it on a phone."],
                    ),
                ]
            ),
        },
    ],
)

MANAGER = _cv(
    name="Rajesh Adhikari",
    headline="Operations Manager",
    email="rajesh.adhikari@example.com",
    phone="+977 9851 000 404",
    location="Biratnagar, Nepal",
    summary=(
        "Operations manager with twelve years in manufacturing and distribution. I run "
        "teams of 40 to 60 people, plan production against orders, and work with "
        "finance on budgets and stock."
    ),
    experience=[
        (
            "Operations Manager",
            "Eastern Valley Foods",
            "2019 – present",
            [
                "Run day-to-day operations of a packaged-food plant with a team of 55.",
                "Plan weekly production with sales and purchasing.",
                "Introduced a monthly safety walk with supervisors from every shift.",
            ],
        ),
        (
            "Production Supervisor",
            "Terai Packaging",
            "2013 – 2019",
            ["Supervised two production lines and trained new machine operators."],
        ),
    ],
    education=[
        ("MBA", "Eastern University", "2010 – 2012", []),
        ("BBA", "Eastern University", "2006 – 2010", []),
    ],
    skills=[
        "Production planning",
        "Team leadership",
        "Budgeting",
        "Inventory control",
        "Health and safety",
    ],
)

ACADEMIC = _cv(
    name="Dr Meera Joshi",
    headline="Lecturer in Environmental Science",
    email="meera.joshi@example.com",
    phone="+977 9841 000 505",
    location="Kirtipur, Nepal",
    summary=(
        "Lecturer in environmental science, teaching undergraduate ecology and "
        "supervising master's research on water quality in the Kathmandu valley."
    ),
    experience=[
        (
            "Lecturer",
            "Valley University, Department of Environmental Science",
            "2020 – present",
            [
                "Teach Ecology I and Field Methods to around 60 undergraduates a year.",
                "Supervise four master's students' research projects.",
            ],
        ),
        (
            "Research Assistant",
            "Himal Water Research Centre",
            "2016 – 2020",
            ["Collected and analysed river water samples for a long-running monitoring project."],
        ),
    ],
    education=[
        ("PhD Environmental Science", "Valley University", "2016 – 2020", []),
        ("MSc Environmental Science", "Valley University", "2013 – 2015", []),
    ],
    skills=["Field sampling", "R", "Water quality analysis", "Teaching", "Research supervision"],
    extra=[
        {
            "key": "publications",
            "kind": "list",
            "title": "Publications",
            "visible": True,
            "items": ["A list of publications is available on request."],
        },
    ],
)

ACCOUNTANT = _cv(
    name="Pooja Karki",
    headline="Accountant",
    email="pooja.karki@example.com",
    phone="+977 9803 000 606",
    location="Kathmandu, Nepal",
    summary=(
        "Accountant with three years in a trading company. I handle month-end close, "
        "VAT returns and supplier payments, and I prepare the figures for the annual audit."
    ),
    experience=[
        (
            "Accountant",
            "Everest Trading House",
            "2022 – present",
            [
                "Prepare monthly accounts and bank reconciliations.",
                "File VAT returns and work with the external auditors each year.",
            ],
        ),
        (
            "Accounts Assistant",
            "Bagmati Supplies",
            "2021 – 2022",
            ["Entered invoices and payments, and chased overdue customer accounts."],
        ),
    ],
    education=[("BBS (Accounting)", "Valley Commerce Campus", "2017 – 2021", [])],
    skills=["Bookkeeping", "Tally", "Excel", "VAT returns", "Bank reconciliation"],
)

#: Which persona each template's own page shows.
BY_TEMPLATE: dict[str, dict[str, Any]] = {
    "meridian": ACCOUNTANT,
    "meridian-compact": MANAGER,
    "vertex": MANAGER,
    "regent": MANAGER,
    "atlas-warm": DESIGNER,
    "prism": DESIGNER,
    "plainspoke": NURSE,
    "lyceum": ACADEMIC,
    "circuit": GRADUATE,
}

#: Every persona, for tests and for anybody choosing one.
ALL = {
    "nurse": NURSE,
    "graduate": GRADUATE,
    "designer": DESIGNER,
    "manager": MANAGER,
    "academic": ACADEMIC,
    "accountant": ACCOUNTANT,
}


def for_template(slug: str) -> dict[str, Any] | None:
    """A private copy of the persona for this template, or None for the shared example."""
    persona = BY_TEMPLATE.get(slug)
    return copy.deepcopy(persona) if persona is not None else None
