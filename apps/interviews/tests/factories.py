"""
Interview fixtures.

Plain functions, per DEVELOPER.md §8.2. `fake_ai` is here rather than in each
test file because four test modules need the same routing and four copies of it
would drift into four slightly different definitions of "the model is available".
"""

from __future__ import annotations

import datetime as dt
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from apps.interviews.models import InterviewKind, InterviewTemplate
from config import ai as ai_config

FAKE_PROVIDER = ai_config.ProviderSpec(
    key="fake", adapter="apps.ai.providers.fake.FakeAdapter", base_url="", timeout_seconds=5
)
FAKE_MODEL = ai_config.ModelSpec(
    provider="fake",
    name="fake-1",
    input_cost_per_million=Decimal("1.00"),
    output_cost_per_million=Decimal("2.00"),
)

#: What `interview.plan` returns when the fake answers it.
PLAN_OUTPUT = {
    "competencies": [
        {
            "key": "problem_solving",
            "label": "Problem solving",
            "why": "Whether they break a hard problem down.",
            "questions": [
                "Tell me about the hardest problem you solved.",
                "Describe a time you were stuck for a day.",
                "Walk me through a decision with incomplete information.",
            ],
        },
        {
            "key": "ownership",
            "label": "Ownership",
            "why": "Whether they finish things.",
            "questions": [
                "Tell me about something you owned end to end.",
                "Describe a time something you built broke.",
                "Tell me about a deadline you missed.",
            ],
        },
    ]
}

FOLLOWUP_OUTPUT = {"question": "What would you do differently now?", "reason": "Probing depth."}
EVALUATION_OUTPUT = {
    "answers": [{"n": 1, "addresses": "yes", "quote": "cut it from four hours"}],
    "score": 4,
    "rationale": "Explains the decision and what resulted; quotes 'cut it from four hours'.",
    "evidence": "cut it from four hours",
}


@contextmanager
def fake_ai():
    """Point every AI task at the fake provider."""
    tasks = {
        key: ai_config.TaskSpec(key=key, models=("primary",), cache_hours=0)
        for key in ai_config.TASKS
    }
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict(ai_config.PROVIDERS, {"fake": FAKE_PROVIDER}, clear=True)
        )
        stack.enter_context(mock.patch.dict(ai_config.MODELS, {"primary": FAKE_MODEL}, clear=True))
        stack.enter_context(mock.patch.dict(ai_config.TASKS, tasks, clear=True))
        yield


def practice_template(**overrides) -> InterviewTemplate:
    """A platform template with real pools, so no plan call is needed."""
    fields = {
        "company": None,
        "slug": "test-practice",
        "name": "Test practice",
        "kind": InterviewKind.AI_TEXT,
        "question_pools": {
            "problem_solving": {
                "label": "Problem solving",
                "why": "Whether they break a hard problem down.",
                "questions": [
                    "Tell me about the hardest problem you solved.",
                    "Describe a time you were stuck for a day.",
                    "Walk me through a decision with incomplete information.",
                    "Tell me about something that turned out to be the wrong solution.",
                ],
            },
            "ownership": {
                "label": "Ownership",
                "why": "Whether they finish things.",
                "questions": [
                    "Tell me about something you owned end to end.",
                    "Describe a time something you built broke.",
                    "Tell me about a deadline you missed.",
                    "Describe something you improved that nobody measured.",
                ],
            },
        },
    }
    fields.update(overrides)
    return InterviewTemplate.objects.create(**fields)


def live_template(**overrides) -> InterviewTemplate:
    fields = {
        "company": None,
        "slug": "test-live",
        "name": "Test live",
        "kind": InterviewKind.LIVE,
        "question_pools": {
            "problem_solving": {"label": "Problem solving", "why": "", "questions": []},
        },
    }
    fields.update(overrides)
    return InterviewTemplate.objects.create(**fields)


def window(*, hours_ahead: int = 24, minutes: int = 45) -> tuple[dt.datetime, dt.datetime]:
    """A bookable window, always in the future so `publish_slots` accepts it."""
    starts = timezone.now() + dt.timedelta(hours=hours_ahead)
    return starts, starts + dt.timedelta(minutes=minutes)
