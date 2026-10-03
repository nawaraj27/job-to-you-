"""
The worked example every template is shown with.

A template gallery is a choice between layouts, and a layout does not exist
until there is text sitting in it: ten empty sheets tell nobody anything. So
every card, every detail page and every comparison draws one complete CV, and
the only thing that varies between them is the template.

Two rules shape this module, and both are about the line between a
demonstration and somebody's real work:

  * **It is the same shape as a real CV.** The example is built out of
    `content.py`'s vocabulary — the sections a blank document starts with, the
    entry fields the editor writes — so a template that renders the example
    renders a real document, and there is no second schema to keep in step.
  * **It is never saved by accident.** `EXAMPLE_CONTENT` is a module constant
    the preview reads and nothing writes. A candidate who wants to start from
    it asks for it, and `example_content()` hands back a fresh copy with its
    own ids, which the ordinary document-creation path then owns. Nothing
    copies the example into a draft on a page view.
"""

from __future__ import annotations

import copy
from typing import Any

from .content import CONTENT_SCHEMA_VERSION, SyncState, new_id

#: The person the example is about. Fictional, and named as such in the
#: contact line's domain: a reviewer who meets this in a screenshot should be
#: able to tell in one glance that it is not a real applicant. The employers
#: are fictional too. A sample that credits invented results to a real company
#: is a false statement about that company, however small the print.
EXAMPLE_CONTENT: dict[str, Any] = {
    "schema_version": CONTENT_SCHEMA_VERSION,
    "contact": {
        "name": "Priya Sharma",
        "headline": "Senior Backend Engineer",
        "email": "priya.sharma@example.com",
        "phone": "+977 9812 345 678",
        "location": "Kathmandu, Nepal",
    },
    # Held twice, exactly as a real document holds it: the top-level field is
    # what `content._canonical` hashes, the section is what the page draws.
    "summary": (
        "Backend engineer with six years on payment systems. Took a nightly "
        "settlement batch to near-real-time and cut reconciliation disputes "
        "by 40%. Comfortable owning a service end to end."
    ),
    "sections": [
        {
            "key": "summary",
            "kind": "text",
            "title": "Summary",
            "visible": True,
            "order": 0,
            "text": (
                "Backend engineer with six years on payment systems. Took a nightly "
                "settlement batch to near-real-time and cut reconciliation disputes "
                "by 40%. Comfortable owning a service end to end."
            ),
        },
        {
            "key": "experience",
            "kind": "entries",
            "title": "Experience",
            "visible": True,
            "order": 1,
            "entries": [
                {
                    "title": "Senior Backend Engineer",
                    "org": "Nexa Payments",
                    "dates": "2022 – present",
                    "bullets": [
                        "Rebuilt the settlement pipeline, moving reconciliation "
                        "from T+1 to under fifteen minutes.",
                        "Cut p99 checkout latency from 1.9s to 420ms.",
                        "Mentored three engineers through their first on-call rotation.",
                    ],
                },
                {
                    "title": "Backend Engineer",
                    "org": "Himal Ledger Co.",
                    "dates": "2019 – 2022",
                    "bullets": [
                        "Built the double-entry ledger behind every transaction on the platform.",
                        "Moved reporting off the write database, taking month-end from "
                        "six hours to twenty minutes.",
                    ],
                },
            ],
        },
        {
            "key": "skills",
            "kind": "list",
            "title": "Skills",
            "visible": True,
            "order": 2,
            "items": [
                "Python, Django",
                "Go",
                "PostgreSQL, Redis",
                "AWS",
                "Kubernetes",
                "Payment systems, reconciliation",
            ],
        },
        {
            "key": "education",
            "kind": "entries",
            "title": "Education",
            "visible": True,
            "order": 3,
            "entries": [
                {
                    "title": "BE Computer Engineering",
                    "org": "Tribhuvan University",
                    "dates": "2015 – 2019",
                    "bullets": ["First division. Final-year project on offline-first payments."],
                }
            ],
        },
        {
            "key": "languages",
            "kind": "list",
            "title": "Languages",
            "visible": True,
            "order": 4,
            "items": ["Nepali (native)", "English (fluent)", "Hindi (conversational)"],
        },
        {
            "key": "projects",
            "kind": "entries",
            "title": "Projects",
            "visible": True,
            "order": 5,
            "entries": [
                {
                    "title": "khata, a small double-entry bookkeeping library",
                    "org": "Personal project",
                    "dates": "2021 – present",
                    "bullets": [
                        "Written in Python to learn how ledgers balance. Open source, with tests "
                        "for every posting rule.",
                    ],
                },
                {
                    "title": "Settlement load harness",
                    "org": "Nexa Payments",
                    "dates": "2023",
                    "bullets": [
                        "Replayed a festival day's traffic against staging before every release.",
                    ],
                },
            ],
        },
        {
            "key": "certifications",
            "kind": "list",
            "title": "Certifications",
            "visible": True,
            "order": 6,
            "items": [
                "AWS Certified Solutions Architect - Associate (2023)",
                "Certified Kubernetes Application Developer (2022)",
            ],
        },
    ],
}


def example_content(*, name: str = "", email: str = "", template_slug: str = "") -> dict[str, Any]:
    """A fresh, writable copy of the example, ready to be a real draft.

    Deep-copied and re-identified, because ids are how an edit names its
    target: two documents started from the example on the same afternoon must
    not share section ids, and the constant above must not be mutated by
    whoever gets the copy. Entries get the provenance fields
    `content.new_entry` gives them — an entry without them is one the
    reconcile protocol cannot reason about, and this content is going into the
    same editor as anything else.

    `name` and `email` replace the example's own, because the one thing a
    candidate must never accidentally export is somebody else's name at the
    top of their CV. Everything below the contact line stays illustrative and
    is theirs to overwrite.
    """
    from .example_personas import for_template

    # The persona this template was designed for, if it has one
    # (`example_personas`); otherwise the shared example.
    content = for_template(template_slug) or copy.deepcopy(EXAMPLE_CONTENT)
    content["schema_version"] = CONTENT_SCHEMA_VERSION
    if name:
        content["contact"]["name"] = name
    if email:
        content["contact"]["email"] = email

    for section in content["sections"]:
        section["id"] = new_id()
        for entry in section.get("entries", []) or []:
            entry["id"] = new_id()
            # Detached: the example came from nowhere, so nothing on the
            # candidate's profile is allowed to overwrite it later.
            entry["source_ref"] = ""
            entry["sync_state"] = SyncState.DETACHED
    return content
