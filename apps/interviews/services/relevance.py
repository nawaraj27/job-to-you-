"""
Whether an interview question is about *this* job, and whether a kit covers it.

The first rule — "the question shares a word of four letters or more with the
posting" — was measured against hand labels and was wrong in both directions:

  * **false negatives** on equivalent wording: "hire for a position in a store"
    did not match a posting about staffing "twenty stores";
  * **false positives** on incidental words: "What strategies do you use to
    ensure your communication is clear" matched a video posting;
  * and it could not see a **leak between kits**: a freight operations analyst
    was asked about "the nightly close", a term from a payments posting.

The rule here, and why each part:

  * **Role terms, compared on stems.** Skills, the title and the content words
    of the description, reduced to a crude stem so "stores"/"store" and
    "reconciling"/"reconciliation" meet. A question naming one is specific.
  * **Meaning, for equivalent wording.** Each question is compared by local
    embedding (nomic-embed-text via `runner.embed`) with the posting's
    responsibility sentences. Above a threshold calibrated on the hand labels,
    it is specific even with no shared word.
  * **Borrowed terms disqualify.** A distinctive term that appears in another
    posting's vocabulary and not in this one's marks the question as a leak.

Not every competency must be anchored to a noun from the posting. A
communication question set in the role's own situation ("explain a late
shipment to a customer") is specific by meaning; one that is not ("strategies
for clear communication") is generic, and a kit is allowed some — it is the
kit-level measures below that say whether it has too many.

Kit-level measures are reported separately because they trade against each
other:

  * **relevance** — share of served questions that are specific;
  * **coverage** — share of the posting's responsibility sentences that at least
    one served question addresses;
  * **clarity** — one question (at most one follow-on clause), 8-45 words;
  * **redundancy** — near-duplicate pairs within a competency;
  * **variety** — questions per competency, and the expected overlap between two
    candidates' sessions.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

#: Words that carry no signal about the role.
_STOP = frozenset(
    [
        "a",
        "about",
        "above",
        "across",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "describe",
        "tell",
        "walk",
        "example",
        "give",
        "time",
        "share",
        "instance",
        "situation",
        "scenario",
        "experience",
        "process",
        "steps",
        "step",
        "specific",
        "approach",
        "project",
        "projects",
        "role",
        "roles",
        "team",
        "teams",
        "work",
        "worked",
        "working",
        "company",
        "business",
        "people",
        "person",
        "thing",
        "things",
        "way",
        "ways",
        "able",
        "ability",
        "ensure",
        "make",
        "made",
        "using",
        "used",
        "use",
        "new",
        "high",
        "well",
        "good",
        "great",
        "strong",
        "within",
        "across",
        "including",
        "include",
        "includes",
        "every",
        "each",
        "day",
        "days",
        "year",
        "years",
        "first",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "twenty",
        "forty",
        "hundred",
        "thousand",
        "part",
        "parts",
        "help",
        "helped",
        "how",
        "handle",
        "handled",
        "issue",
        "issues",
        "problem",
        "problems",
        "challenge",
        "challenges",
        "system",
        "systems",
        "data",
        "product",
        "products",
        "customer",
        "customers",
        "client",
        "clients",
        "manager",
        "managers",
        "job",
        "jobs",
    ]
)

_SUFFIXES = (
    "ations",
    "ation",
    "itions",
    "ition",
    "ments",
    "ment",
    "ities",
    "ity",
    "ings",
    "ing",
    "ions",
    "ion",
    "ers",
    "er",
    "ies",
    "ied",
    "es",
    "ed",
    "ly",
    "s",
)


def stem(word: str) -> str:
    """A crude stem, enough for word forms to meet.

    After the suffix goes, a trailing "e" or "i" goes too, so "reconcile",
    "reconciling" and "reconciliation" all become "reconcil", and "store" meets
    "stores".
    """
    word = word.lower().strip(".-'")
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    if len(word) > 4 and word[-1] in "ei":
        word = word[:-1]
    return word


def content_stems(text: str) -> set[str]:
    words = [w.strip(".-") for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.-]*", text or "")]
    # Punctuation is stripped before the stop-word check: "project." at the end
    # of a sentence was getting past it.
    return {stem(w) for w in words if len(w) >= 3 and w.lower() not in _STOP}


@dataclass
class Posting:
    """The parts of a job the rule reads. Built from a `Job` or a benchmark fixture."""

    title: str
    description: str
    skills: list[str] = field(default_factory=list)
    level: str = ""
    #: The employer's own statements of the job ("Maintain the product design
    #: system"). Read for the things this employer names as its own.
    duties: list[str] = field(default_factory=list)

    @classmethod
    def from_job(cls, job: Any) -> Posting:
        return cls(
            title=job.title,
            description=job.description or "",
            skills=[row.skill.name for row in job.skills.all() if row.skill_id],
            level=job.get_experience_level_display()
            if hasattr(job, "get_experience_level_display")
            else "",
            duties=[str(row) for row in (getattr(job, "responsibilities", None) or [])],
        )

    def terms(self) -> set[str]:
        terms = content_stems(self.title) | content_stems(self.description)
        for skill in self.skills:
            terms |= content_stems(skill) | {skill.lower()}
        return terms

    def phrases(self) -> set[tuple[str, str]]:
        """Two-word stemmed phrases from the posting ("nightly close")."""
        return phrases(" ".join([self.title, self.description, " ".join(self.skills)]))

    def responsibilities(self) -> list[str]:
        """The posting's statements of what the job is, one per sentence."""
        sentences = [
            " ".join(part.split())
            for part in re.split(r"(?<=[.!?])\s+|\n+|;\s+", self.description or "")
        ]
        rows = [s for s in sentences if len(s.split()) >= 4]
        if self.skills:
            rows.append("Skills used in this role: " + ", ".join(self.skills[:20]) + ".")
        return rows[:15] or [self.title]


def lexical_specific(question: str, terms: set[str]) -> bool:
    return bool(content_stems(question) & terms)


def phrases(text: str) -> set[tuple[str, str]]:
    words = [stem(w) for w in re.findall(r"[a-zA-Z][a-zA-Z0-9+#.-]*", text or "")]
    return {
        (a, b)
        for a, b in zip(words, words[1:], strict=False)
        if a not in _STOP and b not in _STOP and len(a) >= 4 and len(b) >= 4
    }


def borrowed_phrases(
    question: str,
    own_terms: set[str],
    own_phrases: set[tuple[str, str]],
    other_phrases: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Two-word phrases from other postings that are wholly foreign to this one.

    Single words were tried first and failed on this corpus: with 28 postings,
    "deploy", "manage" and "develop" each appear in only one or two, so a
    rarity rule flagged 41 of 81 labelled questions. A phrase is borrowed only
    when it occurs in another posting, not in this one, and neither of its words
    appears anywhere in this posting — "nightly close" asked of a freight
    analyst, but not "reconciliation service" asked of a payments engineer whose
    posting says "reconciliation".
    """
    return {
        phrase
        for phrase in phrases(question)
        if phrase in other_phrases
        and phrase not in own_phrases
        and phrase[0] not in own_terms
        and phrase[1] not in own_terms
    }


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Embedder:
    """Embeddings through the shared runner, cached per text for one process."""

    def __init__(self) -> None:
        self._cache: dict[str, list[float] | None] = {}

    def __call__(self, text: str) -> list[float] | None:
        if text not in self._cache:
            from django.db import transaction

            from apps.ai.models import RunStatus
            from apps.ai.services import runner

            # `runner.embed` records its run and emits an event, and events are
            # refused outside a transaction. Called from a management command or
            # a Celery task there is none, and the first embedding crashed.
            with transaction.atomic():
                result = runner.embed(text=text)  # embedder-atomic
            vector = getattr(result, "vector", None)
            status = getattr(result, "status", RunStatus.OK)
            self._cache[text] = list(vector) if vector and status == RunStatus.OK else None
        return self._cache[text]


#: Cosine similarity above which a question counts as addressing a posting
#: statement. **Not used to decide specificity by default.** Calibrated on the
#: 81 labelled questions it added nothing: stem + phrase scored F1 0.921; adding
#: embeddings scored 0.921 at thresholds of 0.575 and above and worse below, and
#: 0.866 held out across jobs, while scoring the 81 questions took 247 s on CPU.
#: Kept for the benchmark's coverage measure, where meaning is what is asked.
SEMANTIC_THRESHOLD = 0.75

#: A competency keeps at least this many questions when it has them, so two
#: sessions do not ask the same pair.
MIN_POOL = 3


@dataclass
class QuestionScore:
    question: str
    lexical: bool
    semantic: float | None
    borrowed: list[str]
    specific: bool
    clear: bool


def is_clear(question: str) -> bool:
    words = len(question.split())
    return 8 <= words <= 45 and question.count("?") <= 2 and "e.g." not in question.lower()


def score_questions(
    questions: list[str],
    posting: Posting,
    *,
    other_phrases: set[tuple[str, str]] | None = None,
    embed: Any = None,
    semantic_scores: dict[str, float] | None = None,
    threshold: float | None = None,
) -> list[QuestionScore]:
    terms = posting.terms()
    own_phrases = posting.phrases()
    responsibilities = posting.responsibilities()
    resp_vectors = [embed(r) for r in responsibilities] if embed else []
    resp_vectors = [v for v in resp_vectors if v]

    scored = []
    for question in questions:
        lexical = lexical_specific(question, terms)
        semantic = (semantic_scores or {}).get(question)
        if semantic is None and resp_vectors:
            vector = embed(question)
            if vector:
                semantic = max(cosine(vector, r) for r in resp_vectors)
        borrowed = sorted(
            " ".join(pair)
            for pair in borrowed_phrases(question, terms, own_phrases, other_phrases or set())
        )
        by_meaning = threshold is not None and semantic is not None and semantic >= threshold
        specific = (lexical or by_meaning) and not borrowed
        scored.append(
            QuestionScore(
                question=question,
                lexical=lexical,
                semantic=None if semantic is None else round(semantic, 3),
                borrowed=borrowed,
                specific=specific,
                clear=is_clear(question),
            )
        )
    return scored


def coverage(
    questions: list[str], posting: Posting, *, embed: Any, threshold: float = SEMANTIC_THRESHOLD
) -> float | None:
    """Share of responsibility sentences some question addresses (by meaning or stem)."""
    responsibilities = posting.responsibilities()
    if not responsibilities or not embed:
        return None
    q_vectors = [v for v in (embed(q) for q in questions) if v]
    covered = 0
    for sentence in responsibilities:
        vector = embed(sentence)
        by_meaning = bool(
            vector and q_vectors and max(cosine(vector, q) for q in q_vectors) >= threshold
        )
        by_words = any(content_stems(sentence) & content_stems(q) for q in questions)
        covered += by_meaning or by_words
    return round(covered / len(responsibilities), 3)


def expected_overlap(pool_sizes: list[int], asked_per_competency: int = 2) -> float | None:
    """Expected share of questions two independent sessions have in common."""
    shares = [min(asked_per_competency, n) / n for n in pool_sizes if n]
    return round(sum(shares) / len(shares), 3) if shares else None


def choose_served(scored: list[QuestionScore], *, min_pool: int = MIN_POOL) -> list[str]:
    """Specific questions first; generic ones only to keep a pool of `min_pool`.

    Borrowed-term questions are never served: they are about a different job.
    Clear specific questions rank ahead of unclear ones, then higher similarity.
    """
    usable = [s for s in scored if not s.borrowed]
    ranked = sorted(
        usable,
        key=lambda s: (not s.specific, not s.clear, -(s.semantic or 0.0)),
    )
    specific = [s.question for s in ranked if s.specific]
    if len(specific) >= min_pool:
        return specific
    rest = [s.question for s in ranked if not s.specific]
    return specific + rest[: max(min_pool - len(specific), 0)]
