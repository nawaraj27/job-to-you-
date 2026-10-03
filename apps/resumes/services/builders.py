"""
The three builder modes (SRS §5.5.2).

All three write to the same draft through the same service. What differs is
*how much they ask for at once*:

  * **editor** — the whole document, section by section. For someone who has a
    CV and wants to change it.
  * **guided** — one question at a time, in a fixed order, with a completion
    state per step. For someone starting from nothing, where an empty editor is
    the reason they give up.
  * **assistant** — a conversation that turns answers into draft edits. For
    someone who would rather describe their job than fill in fields.

The assistant's *wording* generation is P12. What P5 owns is the transcript,
the step state, and the rule that neither ever writes to the draft except
through a suggestion the candidate accepts (INV-3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from apps.common.errors import InvalidState

from .. import content as content_rules
from ..models import BuilderMode, BuilderSession, ResumeDocument

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User

#: The guided flow, in order. Each step names the section it fills and the
#: question that fills it, so the flow is data rather than a chain of views.
#:
#: `optional` is the load-bearing field, and it exists because of who this mode
#: is for. The flow used to be five required steps, one of which was "Where
#: you've worked", and a student, a graduate or a school leaver could not reach
#: the end of it without inventing a job. The progress counter said "4 of 5
#: done" at them forever, and the only action that would move it was to write
#: down employment they have never had. A builder that asks everybody for the
#: same things is a form, and a form that cannot be completed honestly is worse
#: than no form.
#:
#: So a step that does not apply to somebody can be put aside, the counter
#: counts what applies to them, and the sections an early-career CV is actually
#: made of — projects, certifications — are offered rather than hidden behind
#: the editor's "new section" control.
GUIDED_STEPS: tuple[dict[str, object], ...] = (
    {
        "key": "contact",
        "title": "Who you are",
        "prompt": "Your name, the role you want, and how an employer reaches you.",
        "section": "",
        "optional": False,
        "aside": "",
    },
    {
        "key": "summary",
        "title": "Your summary",
        "prompt": "Three lines on what you do and what you are looking for.",
        "section": "summary",
        "optional": False,
        "aside": "",
    },
    {
        "key": "experience",
        "title": "Where you've worked",
        "prompt": "Start with the most recent. Job title, employer, dates, what you did.",
        "section": "experience",
        "optional": True,
        "aside": "I haven't worked yet",
    },
    {
        "key": "projects",
        "title": "What you've built",
        "prompt": "Coursework, side projects, volunteering: anything you can show.",
        "section": "projects",
        "optional": True,
        "aside": "Nothing to add here",
    },
    {
        "key": "skills",
        "title": "What you know",
        "prompt": "The tools and skills you would be comfortable being asked about.",
        "section": "skills",
        "optional": False,
        "aside": "",
    },
    {
        "key": "education",
        "title": "Where you studied",
        "prompt": "Qualifications, institutions and years.",
        "section": "education",
        "optional": True,
        "aside": "I studied outside formal education",
    },
)

#: Where the session keeps the steps somebody has said do not apply to them.
SKIPPED_KEY = "skipped"


MAX_TRANSCRIPT_TURNS = 100


def session_for(*, document: ResumeDocument, mode: str) -> BuilderSession:
    """The saved position for one mode. Created on first use."""
    if mode not in BuilderMode.values:
        raise InvalidState("That is not a builder mode.")

    session, _created = BuilderSession.objects.get_or_create(document=document, mode=mode)
    return session


def guided_state(*, document: ResumeDocument) -> dict:
    """Every step with its real completion state.

    Completion is derived from the draft, never stored: a stored flag says
    "done" about a section the candidate has since emptied, and the guided flow
    then skips the one thing that is missing.
    """
    session = session_for(document=document, mode=BuilderMode.GUIDED)
    content = document.draft.content
    skipped = set(session.step_state.get(SKIPPED_KEY) or [])

    steps = []
    for step in GUIDED_STEPS:
        filled = _step_complete(step, content)
        # Writing into a step undoes putting it aside. Somebody who said they
        # had never worked and then adds a job has plainly changed their mind,
        # and leaving the step greyed out under their own entry is the flow
        # arguing with them.
        put_aside = step["key"] in skipped and not filled
        steps.append(
            {
                **step,
                "complete": filled,
                "skipped": put_aside,
                "settled": filled or put_aside,
            }
        )

    current = session.step_state.get("current") or next(
        (step["key"] for step in steps if not step["settled"]), steps[-1]["key"]
    )

    # The denominator is what applies to this person. A counter that says
    # "4 of 6" to somebody who has never had a job, and can only be moved by
    # their inventing one, is a counter that should not be shown to them.
    applies = [step for step in steps if not step["skipped"]]
    return {
        "steps": steps,
        "current": current,
        "completed": sum(1 for step in applies if step["complete"]),
        "total": len(applies),
        "set_aside": sum(1 for step in steps if step["skipped"]),
    }


@transaction.atomic
def set_step(*, document: ResumeDocument, step: str) -> dict:
    if step not in {row["key"] for row in GUIDED_STEPS}:
        raise InvalidState("That is not a step in the guided flow.")

    session = session_for(document=document, mode=BuilderMode.GUIDED)
    session.step_state = {**session.step_state, "current": step}
    session.save(update_fields=["step_state", "updated_at"])
    _reveal_section_for(document, step)
    return guided_state(document=document)


def _reveal_section_for(document: ResumeDocument, step: str) -> None:
    """Show the section a step fills, if the document starts with it hidden.

    Projects and certifications are off by default, which is right for a
    finished CV and wrong for the step that asks about them: opening "What
    you've built" and finding nowhere to put it is the flow asking a question
    it will not accept an answer to.

    Only an empty hidden section is revealed, and only by opening its own step,
    so this can never re-show something the candidate deliberately hid.
    """
    from . import drafts as draft_service

    definition = next((row for row in GUIDED_STEPS if row["key"] == step), None)
    if definition is None or not definition["section"]:
        return

    draft = document.draft
    section = content_rules.section_by_key(draft.content, str(definition["section"]))
    if section is None or section.get("visible", True):
        return
    if section.get("entries") or section.get("items") or (section.get("text") or "").strip():
        return

    draft_service.toggle_section(draft=draft, section_id=section["id"])


@transaction.atomic
def toggle_step_aside(*, document: ResumeDocument, step: str) -> dict:
    """Say that a step does not apply, or take that back.

    Only a step that is genuinely optional. "Who you are" is not something a CV
    can do without, and offering to skip it would be offering to produce a
    document nobody can reply to.

    Nothing is written to the draft either way: putting a step aside records
    that this person has no employment to list, and recording it as an empty
    experience section would be the platform writing a fact into their CV.
    """
    definition = next((row for row in GUIDED_STEPS if row["key"] == step), None)
    if definition is None:
        raise InvalidState("That is not a step in the guided flow.")
    if not definition["optional"]:
        raise InvalidState("That step is part of every CV.")

    session = session_for(document=document, mode=BuilderMode.GUIDED)
    skipped = set(session.step_state.get(SKIPPED_KEY) or [])
    skipped.symmetric_difference_update({step})

    session.step_state = {**session.step_state, SKIPPED_KEY: sorted(skipped)}
    session.save(update_fields=["step_state", "updated_at"])
    return guided_state(document=document)


@transaction.atomic
def record_turn(
    *, document: ResumeDocument, role: str, text: str, actor: User | None = None
) -> BuilderSession:
    """Append one turn to the assistant's transcript.

    Bounded, and stored on the document rather than in the session cookie: a
    conversation that disappears when a browser is closed is a conversation
    nobody trusts with twenty minutes of work.
    """
    if role not in {"candidate", "assistant"}:
        raise InvalidState("A turn comes from the candidate or the assistant.")
    if not text.strip():
        raise InvalidState("An empty turn says nothing.")

    session = session_for(document=document, mode=BuilderMode.ASSISTANT)
    transcript = [*session.transcript, {"role": role, "text": text.strip()[:2000]}]
    session.transcript = transcript[-MAX_TRANSCRIPT_TURNS:]
    session.save(update_fields=["transcript", "updated_at"])
    return session


def _step_complete(step: dict, content: dict) -> bool:
    if step["key"] == "contact":
        contact = content.get("contact", {})
        return bool(contact.get("name") and (contact.get("email") or contact.get("phone")))

    section = content_rules.section_by_key(content, step["section"])
    if section is None:
        return False
    if section.get("kind") == "text":
        return bool((section.get("text") or "").strip())
    if section.get("kind") == "list":
        return bool(section.get("items"))
    return bool(section.get("entries"))
