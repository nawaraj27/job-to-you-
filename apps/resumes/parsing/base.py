"""
What a parse produces, and what it may fail with.

`ENGINE_VERSION` is part of the contract rather than decoration: a parse is
stored with the version that produced it, so a later improvement to the reader
can be re-run over old uploads selectively instead of re-parsing everything or,
worse, leaving two generations of output indistinguishable in one table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

ENGINE = "deterministic"

#: Bump on any change to extraction that would produce different output for the
#: same input. Stored on every `ResumeParse`.
ENGINE_VERSION = "1.4"

#: The structure `ResumeParse.structured` holds. Bump alongside a shape change,
#: never instead of one (ADR-A27).
STRUCTURED_SCHEMA_VERSION = 2


class ExtractionFailed(Exception):
    """The file could not be read at all — corrupt, encrypted, or truncated."""


class UnsupportedFormat(Exception):
    """The format has no reader. Not an error in the file."""


@dataclass(frozen=True)
class Experience:
    title: str = ""
    org: str = ""
    start: str = ""
    end: str = ""
    is_current: bool = False
    bullets: tuple[str, ...] = ()
    evidence: str = ""


@dataclass(frozen=True)
class Project:
    """Something the candidate built, with whatever they said about it.

    Shaped like an entry rather than a string because a project has a
    description and a CV that leads with projects — a student's, a designer's,
    a career changer's — needs that description to survive the import. It is
    the section those candidates are judged on.
    """

    name: str = ""
    bullets: tuple[str, ...] = ()
    evidence: str = ""


@dataclass(frozen=True)
class Education:
    institution: str = ""
    qualification: str = ""
    start: str = ""
    end: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class ParsedResume:
    """Everything a deterministic reader can honestly claim about a CV.

    Every field is optional because every field is genuinely missing from some
    real CV. Confidence is per field group rather than one number: the reader
    is far surer about an email address it matched than about a job title it
    inferred from a line's position.
    """

    raw_text: str = ""
    name: str = ""
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    headline: str = ""
    #: The place written beside the email or phone in the header ("Pokhara,
    #: Nepal"). Read from the CV, never guessed; empty when the header has none.
    location: str = ""
    summary: str = ""
    skill_terms: tuple[str, ...] = ()
    experience: tuple[Experience, ...] = ()
    education: tuple[Education, ...] = ()
    languages: tuple[str, ...] = ()
    #: The sections a CV built out of anything other than employment depends
    #: on. Read for a long time and carried nowhere: a student's projects, an
    #: academic's publications and a nurse's registration all reached the
    #: parse and were dropped on the way into the builder, so the candidate
    #: opened "their" CV and found the part they were proudest of missing.
    certifications: tuple[str, ...] = ()
    projects: tuple[Project, ...] = ()
    awards: tuple[str, ...] = ()
    publications: tuple[str, ...] = ()
    sections: dict[str, str] = field(default_factory=dict)
    confidence: dict[str, Decimal] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def as_structured(self) -> dict:
        """The JSON shape stored on `ResumeParse.structured`."""
        return {
            "name": self.name,
            "contact": {
                "emails": list(self.emails),
                "phones": list(self.phones),
                "links": list(self.links),
                "location": self.location,
            },
            "headline": self.headline,
            "summary": self.summary,
            "skills": list(self.skill_terms),
            "experience": [
                {
                    "title": row.title,
                    "org": row.org,
                    "start": row.start,
                    "end": row.end,
                    "is_current": row.is_current,
                    "bullets": list(row.bullets),
                }
                for row in self.experience
            ],
            "education": [
                {
                    "institution": row.institution,
                    "qualification": row.qualification,
                    "start": row.start,
                    "end": row.end,
                }
                for row in self.education
            ],
            "languages": list(self.languages),
            "certifications": list(self.certifications),
            "projects": [{"name": row.name, "bullets": list(row.bullets)} for row in self.projects],
            "awards": list(self.awards),
            "publications": list(self.publications),
            "sections": {key: value[:4000] for key, value in self.sections.items()},
        }
