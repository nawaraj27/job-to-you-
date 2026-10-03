"""
Platform interview templates (FR-10.2).

Practice needs something to run against before any company has written a
competency framework, and a candidate practising should not be waiting on an AI
plan for a job they have not applied to. These pools are written rather than
generated for the same reason the prompts are files: they are reviewed, they are
diffable, and a question that turns out to be a bad question is fixed in a diff
somebody reads.

Idempotent. Running it on every deploy updates the pools in place rather than
creating a second template with the same name.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.interviews.models import InterviewKind, InterviewTemplate

#: The general-purpose practice interview. Four competencies that apply to
#: almost any role, with questions that ask what somebody did rather than what
#: they would do — the difference between an answer and a rehearsal.
GENERAL_POOLS: dict[str, Any] = {
    "problem_solving": {
        "label": "Problem solving",
        "why": "Whether they break a hard problem down or thrash at it.",
        "questions": [
            "Tell me about the hardest problem you solved in the last year. What made it hard?",
            "Describe a time you were stuck for more than a day. How did you get unstuck?",
            "Walk me through a decision you made with incomplete information.",
            "Tell me about something you built that turned out to be the wrong solution.",
            "Describe a problem you solved that nobody had asked you to look at.",
            "Tell me about a time you had to understand somebody else's work quickly.",
        ],
    },
    "communication": {
        "label": "Communication",
        "why": "Whether they can make somebody else understand their work.",
        "questions": [
            "Tell me about a time you had to explain something technical to somebody who was not.",
            "Describe a disagreement you had with a colleague about how to do something.",
            "Walk me through how you would hand over a project you had been running alone.",
            "Tell me about a time you had to say no to somebody senior.",
            "Describe feedback you received that changed how you work.",
            "Tell me about a time you realised you had been unclear and had to fix it.",
        ],
    },
    "ownership": {
        "label": "Ownership",
        "why": "Whether they finish things or hand them over half-done.",
        "questions": [
            "Tell me about something you owned end to end. What did that involve?",
            "Describe a time something you built broke. What did you do?",
            "Tell me about a deadline you missed. What happened next?",
            "Describe something you improved that nobody was measuring.",
            "Tell me about a time you inherited something that was in a bad state.",
            "Walk me through the last thing you shipped, from start to finish.",
        ],
    },
    "learning": {
        "label": "Learning",
        "why": "Whether they get better at things or repeat the same year.",
        "questions": [
            "Tell me about something you learned recently because the work needed it.",
            "Describe a mistake you made twice before you fixed the cause.",
            "Tell me about a tool or approach you changed your mind about.",
            "Walk me through how you got up to speed the last time you joined something.",
            "Describe something you are currently not good at and are working on.",
            "Tell me about a time somebody taught you something that stuck.",
        ],
    },
}

TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "slug": "general-practice",
        "name": "General practice interview",
        "kind": InterviewKind.AI_TEXT,
        "question_pools": GENERAL_POOLS,
    },
    {
        "slug": "general-screen",
        "name": "General screening interview",
        "kind": InterviewKind.AI_TEXT,
        "question_pools": GENERAL_POOLS,
    },
    {
        "slug": "live-conversation",
        "name": "Live interview",
        "kind": InterviewKind.LIVE,
        # No pools: a live interview's questions are the interviewer's. What the
        # template carries is the rubric the scorecard is written against.
        "question_pools": {
            key: {"label": pool["label"], "why": pool["why"], "questions": []}
            for key, pool in GENERAL_POOLS.items()
        },
    },
)


class Command(BaseCommand):
    help = "Create or update the platform interview templates. Idempotent."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--quiet-summary", action="store_true")

    def handle(self, *args, **options) -> None:
        created = updated = 0

        for spec in TEMPLATES:
            _, was_created = InterviewTemplate.objects.update_or_create(
                company=None,
                slug=spec["slug"],
                defaults={
                    "name": spec["name"],
                    "kind": spec["kind"],
                    "question_pools": spec["question_pools"],
                    "is_active": True,
                },
            )
            created += int(was_created)
            updated += int(not was_created)

        if not options.get("quiet_summary"):
            self.stdout.write(f"interview templates: {created} created, {updated} updated")
