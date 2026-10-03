"""
Asking for evidence before writing anything down (ASC-01).

Manual testing found the assistant too easy to satisfy: "I have Python skills"
was enough for it to consider Python dealt with. A skill on its own is a claim,
not evidence — an employer reading "Python" learns nothing about whether the
candidate has used it for anything — and the assistant's job is to get the
evidence, a question at a time, with as few questions as that takes.

So before any model is asked, one deterministic step decides whether the
candidate has said enough:

  1. **A bare claim** ("I know Python", "I have Excel experience") gets
     *where* — at a job, on a project, while studying?
  2. **Where, but nothing done** gets *what* — what did you build or work on?
  3. **Something done, no outcome** gets *result* — what came of it?

Each question is asked once, and the candidate can stop the probe at any point
("skip", "that's all"). What they said across the probe is then handed to the
model as one account, so the bullets it proposes are grounded in all of it.

Two more questions this step may ask, both only when they are relevant:

  * **A gap in a target job.** When the candidate is working towards a specific
    posting (chosen on the analysis report), each skill that posting asks for
    and the CV does not mention is asked about, one at a time: "The Backend
    Developer role asks for Django. Have you used it — and if so, where?"
    Asked, not assumed: a "no" is an answer and the gap stays a gap.
  * **Location.** If the CV has no location, the candidate is asked for it
    once. Never inferred, never guessed from an employer or a phone number.

Nothing here writes to the CV. Questions are transcript turns; the only path
to the draft is still a suggestion the candidate accepts (INV-3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Where the probe keeps its place, inside the assistant session's step_state.
PROBE_KEY = "probe"
TARGET_KEY = "target"
LOCATION_ASKED_KEY = "location_asked"

#: "I have Python skills", "I know Django", "I'm good at Excel", "experience
#: with SQL", "familiar with Figma", "I used Python", "I have worked with
#: SQL". The skill is the captured group, and it stops at the first word that
#: starts a new clause — "I know Python and Django from my job" is a claim
#: about Python, not about a skill called "Python and Django from my job"
#: (EVD-02).
_SKILL = (
    r"[\w+#.-]+(?:\s+(?!(?:and|at|for|in|on|to|with|from|while|when|since|during|as|but|"
    r"so|which|that|there|here)\b)[\w+#.-]+){0,3}"
)
_CLAIM = re.compile(
    r"\b(?:i\s+(?:have|got)\s+(?:some\s+|a\s+lot\s+of\s+|good\s+|strong\s+|basic\s+)?"
    rf"(?P<a>{_SKILL}?)\s+(?:skills?|experience|knowledge)"
    r"|i\s+(?:know|can\s+use|use|used|have\s+used|'ve\s+used|am\s+good\s+at|'m\s+good\s+at|"
    r"(?:have\s+)?worked\s+with|'ve\s+worked\s+with)\s+"
    rf"(?P<b>{_SKILL})"
    rf"|(?:experience|skilled|familiar)\s+(?:with|in)\s+(?P<c>{_SKILL}))",
    re.IGNORECASE,
)

#: Words that say *where* something happened. The "at Phewa Stays" branch is
#: case-sensitive on the capital: under IGNORECASE it also took "for a while"
#: and "at all" as places (EVD-03).
_WHERE = re.compile(
    r"\b(?:at|for)\s+(?-i:[A-Z])[\w&.-]+|\b(?:job|work(?:ed|ing)?|company|employer|client|"
    r"internship|intern|freelance|project|course|university|college|school|degree|"
    r"thesis|volunteer(?:ed|ing)?|startup|team)\b",
    re.IGNORECASE,
)

#: Words that say something was *done*.
_DID = re.compile(
    r"\b(?:built|build|developed|created|made|wrote|designed|implemented|ran|run|"
    r"managed|led|launched|automated|analy[sz]ed|migrated|maintained|shipped|"
    r"deployed|set\s+up|organi[sz]ed|coordinated|taught|trained|planned|delivered)\b",
    re.IGNORECASE,
)

#: Words that say what *came of* it. A number counts — "12 hotels", "from 4
#: hours to 30 minutes" — but not a year or a length of time using something:
#: "for 3 years" says how long, not what came of it (EVD-04).
_RESULT = re.compile(
    r"(?<!\d)(?!(?:19|20)\d{2}(?!\d))\d+(?!\d)(?!\s*\+?\s*(?:years?|yrs?|months?)\b)|%|"
    r"\b(?:result(?:ed)?|so\s+that|which\s+(?:meant|let|helped|cut|saved)|reduc|"
    r"increas|improv|saved|cut|grew|faster|fewer|more\s+\w+\s+than|used\s+by|won|"
    r"launched|adopted|replaced)\w*",
    re.IGNORECASE,
)

_STOP = re.compile(
    r"^\s*(?:skip|that'?s\s+(?:all|it)|nothing\s+(?:more|else)|no\s+more|i\s+don'?t\s+know|"
    r"not\s+sure|move\s+on|next)\b",
    re.IGNORECASE,
)

_NO = re.compile(r"^\s*(?:no|nope|not\s+really|never|i\s+haven'?t|i\s+have\s+not)\b", re.I)
#: A yes to "Have you used Django?" that names nothing else: the account that
#: follows is about the skill that was asked about.
_YES = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|i\s+have|i\s+did|i\s+do|a\s+little|a\s+bit|some|sure|of\s+course)\b",
    re.I,
)
#: "No problem, I used it at …" and "No, but I used Flask" are not a no.
_NOT_A_NO = re.compile(r"^\s*no\s+problem\b|\bbut\b", re.I)

QUESTIONS = {
    "where": (
        "Where have you used {skill}: at a job, on a project, or while studying? "
        "Tell me which one, and its name."
    ),
    "what": "What did you build or work on with {skill} there?",
    "result": (
        "What came of it? Did it get used, save time, or fix a problem? "
        "Only what actually happened; it is fine if there was no number."
    ),
}


@dataclass(frozen=True)
class Step:
    """What the probe decided about one turn."""

    #: A question to ask now instead of calling the model; empty to go on.
    question: str = ""
    #: What to give the model as the candidate's latest words: the whole
    #: probe's answers joined, so the account is complete.
    latest: str = ""
    state: dict[str, Any] | None = None
    #: A target-job skill the candidate just said they do not have. Nothing
    #: is drafted from a "no"; the caller moves on to the next gap.
    declined: str = ""


def claimed_skill(text: str) -> str:
    """The skill a message is about, or ''.

    Only a message phrased as a claim is one — "I know …", "I used …", "I
    have … experience". An account of work ("I moved 12 batch jobs onto
    Celery, which cut…") names a skill too, and is exactly what the probe is
    trying to get; asking it "where?" would be asking for what it already is.

    Within a claim, skills the taxonomy knows come first, in the order the
    message names them, so "I used Python at Phewa Stays" is about Python
    (EVD-01). Two at most: "Python and Django" is one account, and a round of
    questions for each would be a questionnaire. A claim about a skill the
    taxonomy has never heard of still counts, read off the sentence.
    """
    match = _CLAIM.search(text or "")
    if not match:
        return ""
    named = skills_named(text)
    if named:
        return " and ".join(named[:2])
    skill = (match.group("a") or match.group("b") or match.group("c") or "").strip(" .,")
    # "I have experience" with nothing named is not a claim about a skill.
    if not skill or skill.lower() in {"some", "a lot", "good", "experience", "work", "it", "them"}:
        return ""
    return skill[:40]


def skills_named(text: str) -> list[str]:
    """Taxonomy skills the message mentions, in the order it mentions them."""
    from ..analysis import target as target_rules

    lowered = (text or "").lower()
    if not lowered.strip():
        return []
    found: dict[str, int] = {}
    for pattern, canonical in target_rules._vocabulary():
        match = pattern.search(lowered)
        if match and (canonical not in found or match.start() < found[canonical]):
            found[canonical] = match.start()
    return sorted(found, key=found.__getitem__)


def next_step(state: dict[str, Any], text: str) -> Step:
    """Decide whether this turn needs a follow-up before anything is drafted."""
    state = dict(state or {})
    probe = dict(state.get(PROBE_KEY) or {})

    # This turn answers a target-job gap question (EVD-05). A "no" is an
    # answer: the gap stays a gap and nothing is drafted from it. Anything
    # else is evidence about that skill and is probed like any other claim —
    # a bare "Yes" still needs where, what, and what came of it.
    target = dict(state.get(TARGET_KEY) or {})
    pending = target.pop("pending", "")
    if pending:
        state[TARGET_KEY] = target
        if answered_no(text):
            target["declined"] = [*target.get("declined", []), pending]
            state[TARGET_KEY] = target
            return Step(
                question=f"Understood. {pending} stays off your CV.",
                state=state,
                declined=pending,
            )
        if _STOP.match(text or ""):
            # "skip" at a gap question moves on, like "skip" inside a probe;
            # it used to be sent to the model as an account of work (SKIP-01).
            return Step(
                question=f"Skipped. Nothing about {pending} goes on your CV.",
                state=state,
                declined=pending,
            )
        if not probe and not claimed_skill(text) and _YES.match(text or ""):
            stage = _stage_for(text)
            if stage is not None:
                state[PROBE_KEY] = {"skill": pending, "answers": [text], "asked": [stage]}
                return Step(question=QUESTIONS[stage].format(skill=pending), state=state)

    if probe:
        answers = [*probe.get("answers", []), text]
        if _STOP.match(text or ""):
            state.pop(PROBE_KEY, None)
            return Step(latest=" ".join(answers[:-1]) or text, state=state)
        # The answer to a question counts as that piece of evidence, whether
        # or not its wording is one the patterns know ("I containerised the
        # booking API" answers "what"); the next piece still missing is asked.
        stage = _stage_for(" ".join(answers), answered=probe.get("asked", []))
        if stage is None:
            state.pop(PROBE_KEY, None)
            return Step(latest=" ".join(answers), state=state)
        probe.update(answers=answers, asked=[*probe.get("asked", []), stage])
        state[PROBE_KEY] = probe
        return Step(question=QUESTIONS[stage].format(skill=probe["skill"]), state=state)

    skill = claimed_skill(text)
    if skill:
        stage = _stage_for(text)
        if stage is not None:
            state[PROBE_KEY] = {"skill": skill, "answers": [text], "asked": [stage]}
            return Step(question=QUESTIONS[stage].format(skill=skill), state=state)
    return Step(latest=text, state=state)


def _stage_for(said: str, answered: list[str] | tuple[str, ...] = ()) -> str | None:
    """The first piece of evidence still missing, or None when there is enough.

    A stage already asked has been answered — by the next turn — so it is
    never asked twice.
    """
    for stage, pattern in (("where", _WHERE), ("what", _DID), ("result", _RESULT)):
        if stage not in answered and not pattern.search(said):
            return stage
    return None


# --- target job and location ----------------------------------------------


def set_target(state: dict[str, Any], *, title: str, missing: list[str]) -> dict[str, Any]:
    """Point the conversation at a posting and the evidence it lacks."""
    state = dict(state or {})
    state[TARGET_KEY] = {"title": title, "missing": list(missing)[:12], "asked": []}
    return state


def target_context(state: dict[str, Any]) -> str:
    """The target, as the model reads it. Empty when there is none."""
    target = (state or {}).get(TARGET_KEY) or {}
    if not target:
        return ""
    missing = [skill for skill in target.get("missing", []) if skill]
    line = f"They are working towards: {target.get('title', '')}."
    if missing:
        line += (
            " That posting asks for these, which their CV does not yet mention: "
            + ", ".join(missing)
            + ". If what they say touches one of them, ask about it — never assume they have it."
        )
    return line


def gap_question(state: dict[str, Any], cv_text: str) -> tuple[str, dict[str, Any]]:
    """The next target-job gap to ask about, if any is still unasked.

    A skill that has since appeared in the CV is no longer a gap and is not
    asked about.
    """
    state = dict(state or {})
    target = dict(state.get(TARGET_KEY) or {})
    if not target:
        return "", state
    lowered = (cv_text or "").lower()
    for skill in target.get("missing", []):
        if skill in target.get("asked", []) or skill.lower() in lowered:
            continue
        target["asked"] = [*target.get("asked", []), skill]
        # The next candidate turn is read as the answer to this.
        target["pending"] = skill
        state[TARGET_KEY] = target
        question = (
            f"The {target.get('title', 'role')} role asks for {skill}. Have you used {skill}? "
            "If so, where, and what did you do with it?"
        )
        return question, state
    return "", state


def answered_no(text: str) -> bool:
    return bool(_NO.match(text or "")) and not _NOT_A_NO.search(text or "")


# --- what the CV itself is missing (ASC-02) -------------------------------

CV_ASKED_KEY = "cv_asked"


#: "at Phewa Stays", "for Nabil Bank" — a place the candidate named.
_NAMED_PLACE = re.compile(r"\b(?i:at|for)\s+([A-Z][\w&.'-]+(?:\s+[A-Z][\w&.'-]+){0,3})")


def named_place(said: str) -> str:
    """The last employer or place the candidate named in the conversation."""
    found = _NAMED_PLACE.findall(said or "")
    return found[-1].strip(" .,") if found else ""


def cv_gap_question(
    state: dict[str, Any], content: dict[str, Any], *, said: str = ""
) -> tuple[str, dict]:
    """The next thing the CV lacks, asked once, or '' when it is sufficient.

    Manual testing: the assistant asked too little — it answered what it was
    told and never noticed that the CV had no dates, no results, no projects
    or no location. These are read off the draft itself, in the order that
    matters most to a reader, and each is asked at most once per conversation.
    Asking is all this does: an answer becomes a suggestion like any other,
    and nothing is ever filled in from a guess.
    """
    state = dict(state or {})
    asked = list(state.get(CV_ASKED_KEY) or [])
    targeted = bool(state.get(TARGET_KEY))
    place = named_place(said)
    for key, question in _cv_gaps(content or {}, place=place):
        if key in asked or (targeted and key == "target_role"):
            continue
        state[CV_ASKED_KEY] = [*asked, key]
        return question, state
    return "", state


def _cv_gaps(content: dict[str, Any], *, place: str = "") -> list[tuple[str, str]]:
    from .. import content as content_rules

    gaps: list[tuple[str, str]] = []
    experience = content_rules.section_by_key(content, "experience") or {}
    entries = [
        entry for entry in experience.get("entries") or [] if entry.get("title") or entry.get("org")
    ]
    if not entries:
        # Where it was is not asked again when the conversation already named
        # it ("At Phewa Stays, my current job…"): only what is still missing.
        gaps.append(
            (
                "experience",
                f"What was your title at {place}, and roughly when did you start there?"
                if place
                else "Tell me about your most recent job or internship: where it was, your "
                "title, and roughly when.",
            )
        )
    else:
        undated = [entry for entry in entries if not (entry.get("dates") or "").strip()]
        if undated:
            where = undated[0].get("org") or undated[0].get("title") or "your last role"
            gaps.append(
                ("dates", f"When did you work at {where}? A start and end month is enough.")
            )
        bullets = [bullet for entry in entries for bullet in entry.get("bullets") or []]
        if bullets and not any(re.search(r"\d", str(bullet)) for bullet in bullets):
            gaps.append(
                (
                    "achievements",
                    "Is there a result from your work you can put a number on, such as time saved, "
                    "people served, money, volume? Only what really happened.",
                )
            )
    contact = content.get("contact") or {}
    if not (contact.get("headline") or "").strip():
        gaps.append(
            (
                "target_role",
                "Which role are you aiming for next? I will ask about what that kind of job "
                "looks for.",
            )
        )
    projects = content_rules.section_by_key(content, "projects") or {}
    if not [entry for entry in projects.get("entries") or [] if entry.get("title")]:
        gaps.append(
            (
                "projects",
                "Have you built anything outside a job, such as a project, coursework or volunteering, "
                "worth listing? What was it, and what did it do?",
            )
        )
    skills = content_rules.section_by_key(content, "skills") or {}
    if len([item for item in skills.get("items") or [] if str(item).strip()]) < 5:
        gaps.append(
            (
                "skills",
                "Which tools or skills do you use most in your work? Name the ones you could "
                "talk about in an interview.",
            )
        )
    if not (contact.get("location") or "").strip():
        gaps.append(("location", "Where are you based? A city is enough for the top of your CV."))
    return gaps


def location_question(state: dict[str, Any], content: dict[str, Any]) -> tuple[str, dict]:
    """Ask once for a location when the CV has none. Never inferred."""
    state = dict(state or {})
    location = ((content or {}).get("contact") or {}).get("location", "")
    if location or state.get(LOCATION_ASKED_KEY):
        return "", state
    state[LOCATION_ASKED_KEY] = True
    return (
        "One more thing for the top of your CV: where are you based? A city is enough.",
        state,
    )
