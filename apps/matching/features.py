"""
The feature allow-list (FR-8.9, SRS §10.4).

**This module is the fairness control.** Everything the scorer is allowed to
look at is named here, and `check_features` fails when a feature vector carries
anything else. The test that calls it fails the build — which is the whole
mechanism: an allow-list nothing checks is a comment.

Why an allow-list rather than a block-list of protected attributes: a block-list
has to anticipate every proxy, and it never does. Postcode is not "address", the
name of a school is not "ethnicity", and a photograph is not "age" — but each
one carries the attribute anyway. Naming what may be used inverts the burden:
adding a feature is a decision somebody has to write down here, in a file whose
diff a reviewer reads for exactly this reason.

Permanently excluded, and not by omission — by this list, which the test also
asserts stays excluded: name, photograph, age or date of birth, gender, marital
status, address precision below city, and institution prestige.

The last one deserves its own sentence, because it is the one that looks
harmless. "Which university" is a proxy for class, caste and city in this market
specifically. A ranking that rewards it is a ranking that reproduces the
existing distribution of opportunity while appearing to measure merit. What is
scored instead is what the education *is* — level and field — which is a fact
about the qualification and not about the queue the candidate started in.
"""

from __future__ import annotations

from typing import Any

#: Every feature the scorer may read, mapped to what it is and why it is fair
#: to use. A feature not in this mapping cannot enter the vector.
ALLOWED_FEATURES: dict[str, str] = {
    # --- skills -----------------------------------------------------------
    "skill_ids": "Which skills the candidate has confirmed. The thing being hired for.",
    "skill_years": "How long each skill has been used. Stated by the candidate.",
    "skill_levels": "Self-reported proficiency per skill.",
    # --- experience -------------------------------------------------------
    "experience_months": "Total months of work history, from dated roles.",
    "seniority": "Level implied by titles held. Derived from the work, not the person.",
    # --- domain -----------------------------------------------------------
    #
    # There is no candidate-side category here, deliberately. A profile carries
    # no job family, and the two ways to invent one — infer it from what they
    # have applied to, or from what the feed showed them — both make the feed a
    # function of itself. Industry is read from work history, which is a fact.
    "industry_ids": "Industries the candidate has worked in, from their employers.",
    # --- location and arrangement ----------------------------------------
    "location_city_id": "City only. Never a street, a ward, or a postcode.",
    "open_to": "Work arrangements the candidate said they will accept.",
    "willing_remote": "Whether the candidate accepts remote work.",
    # --- compensation -----------------------------------------------------
    "salary_min_base": "Stated minimum, normalised to the base currency and period.",
    "salary_max_base": "Stated maximum, normalised.",
    # --- freshness --------------------------------------------------------
    "profile_updated_days": "How recently the candidate maintained their profile.",
    "last_active_days": "How recently the candidate used the platform.",
    # --- the job side -----------------------------------------------------
    "job_skill_ids": "Skills the posting asks for.",
    "job_required_skill_ids": "Which of those the posting marks required.",
    "job_skill_weights": "How much the employer weighted each skill.",
    "job_skill_min_years": "Minimum years the posting asks for, per skill.",
    "job_experience_level": "The band the posting states.",
    "job_category_id": "The posting's job family.",
    "job_industry_id": "The hiring company's industry.",
    "job_location_city_id": "The posting's city.",
    "job_workplace": "On-site, hybrid or remote.",
    "job_remote_scope": "Remote from where.",
    "job_salary_min_base": "The posting's minimum, normalised.",
    "job_salary_max_base": "The posting's maximum, normalised.",
    "job_published_days": "How long the posting has been open.",
}

#: Named so the test can assert they stay out, rather than trusting that nobody
#: adds them. A block-list is not the control — this is documentation of what
#: the control is for.
NEVER_ALLOWED: frozenset[str] = frozenset(
    {
        "name",
        "first_name",
        "last_name",
        "photo",
        "avatar",
        "age",
        "date_of_birth",
        "gender",
        "marital_status",
        "address",
        "street",
        "postcode",
        "ward",
        "institution",
        "institution_rank",
        "university",
        "school",
        "ethnicity",
        "caste",
        "religion",
        "nationality",
        "disability",
        "email",
        "phone",
    }
)


class DisallowedFeature(Exception):
    """A feature vector carried something the scorer may not look at.

    Its own exception, not a `DomainError`: this is not a user-facing condition
    and it is not recoverable at runtime. It means the code is wrong, and the
    only correct response is to fail loudly wherever it is noticed.
    """


def check_features(vector: dict[str, Any]) -> None:
    """Raise unless every key in `vector` is on the allow-list.

    Called by the scorer on every score in DEBUG and by the test suite always.
    The cost is a set difference over about twenty keys; the thing it prevents
    is a protected attribute reaching a ranking, which is not a bug anybody
    finds by reading a dashboard.
    """
    unknown = set(vector) - set(ALLOWED_FEATURES)
    if not unknown:
        return

    forbidden = sorted(unknown & NEVER_ALLOWED)
    if forbidden:
        raise DisallowedFeature(
            f"{', '.join(forbidden)} may never enter the scorer (SRS §10.4). "
            "This is not an allow-list omission; these are excluded permanently."
        )
    raise DisallowedFeature(
        f"{', '.join(sorted(unknown))} is not on the scorer's allow-list. "
        "Add it to ALLOWED_FEATURES with a one-line justification, or do not use it."
    )
