"""
The scorer and the fairness allow-list (FR-8.1, FR-8.2, FR-8.9, ADR-A6).

`SimpleTestCase` throughout, which is the claim: the scorer is a pure function
of two dictionaries. If any test here needed a database, the scorer had reached
for state, and reproducibility — the entire justification for scoring
arithmetically rather than asking a model — would already be gone.

Tests pin *behaviour* rather than exact totals wherever an exact total would
turn every future weight change into a test rewrite. The exceptions are the two
facts everything else rests on: the weights sum to 100, and the bands are the
ones FR-8.2 names.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.matching import scoring
from apps.matching.extract import SkillCredit
from apps.matching.features import (
    ALLOWED_FEATURES,
    NEVER_ALLOWED,
    DisallowedFeature,
    check_features,
)

PYTHON, DJANGO, POSTGRES, KUBERNETES = 1, 2, 3, 4


def candidate(**overrides) -> dict:
    vector = {
        "skill_ids": [PYTHON, DJANGO, POSTGRES],
        "skill_years": {PYTHON: 4.0, DJANGO: 3.0, POSTGRES: 2.0},
        "skill_levels": {PYTHON: 4, DJANGO: 4, POSTGRES: 3},
        "experience_months": 60,
        "seniority": "mid",
        "industry_ids": [10],
        "location_city_id": 100,
        "open_to": ["full_time", "remote"],
        "willing_remote": True,
        "salary_min_base": 900_000.0,
        "salary_max_base": 1_400_000.0,
        "profile_updated_days": 3,
        "last_active_days": 3,
    }
    vector.update(overrides)
    return vector


def job(**overrides) -> dict:
    vector = {
        "job_skill_ids": [PYTHON, DJANGO],
        "job_required_skill_ids": [PYTHON, DJANGO],
        "job_skill_weights": {PYTHON: 1.0, DJANGO: 1.0},
        "job_skill_min_years": {},
        "job_experience_level": "mid",
        "job_category_id": 7,
        "job_industry_id": 10,
        "job_location_city_id": 100,
        "job_workplace": "on_site",
        "job_remote_scope": "none",
        "job_salary_min_base": 1_000_000.0,
        "job_salary_max_base": 1_600_000.0,
        "job_published_days": 2,
    }
    vector.update(overrides)
    return vector


def credit(*skill_ids, implied: dict[int, float] | None = None) -> SkillCredit:
    return SkillCredit(
        direct=dict.fromkeys(skill_ids, 1.0),
        implied=dict(implied or {}),
    )


def component(match: scoring.Match, key: str) -> scoring.Component:
    return next(item for item in match.components if item.key == key)


class WeightTests(SimpleTestCase):
    def test_the_weights_sum_to_one_hundred(self):
        # Everything else assumes this. A scorer whose weights sum to 97
        # produces a number nobody can reason about and everybody can argue with.
        self.assertEqual(sum(scoring.WEIGHTS.values()), 100)

    def test_the_weights_are_the_ones_the_srs_states(self):
        self.assertEqual(
            scoring.WEIGHTS,
            {
                "skills": 50,
                "experience": 20,
                "domain": 10,
                "location": 8,
                "salary": 7,
                "recency": 5,
            },
        )

    def test_every_component_appears_in_every_score(self):
        match = scoring.score(candidate(), job(), credit=credit(PYTHON, DJANGO, POSTGRES))
        self.assertEqual({item.key for item in match.components}, set(scoring.WEIGHTS))

    def test_no_component_exceeds_its_weight_or_goes_negative(self):
        for name, vectors in {
            "good": (candidate(), job()),
            "empty": (candidate(skill_ids=[], skill_years={}, experience_months=0), job()),
        }.items():
            match = scoring.score(vectors[0], vectors[1], credit=credit())
            for item in match.components:
                with self.subTest(case=name, component=item.key):
                    self.assertGreaterEqual(item.points, 0)
                    self.assertLessEqual(item.points, item.weight)


class BandTests(SimpleTestCase):
    def test_the_bands_are_the_ones_fr_8_2_names(self):
        self.assertEqual(scoring.band_for(90), "strong")
        self.assertEqual(scoring.band_for(85), "strong")
        self.assertEqual(scoring.band_for(84), "good")
        self.assertEqual(scoring.band_for(70), "good")
        self.assertEqual(scoring.band_for(69), "partial")
        self.assertEqual(scoring.band_for(50), "partial")
        self.assertEqual(scoring.band_for(49), "stretch")
        self.assertEqual(scoring.band_for(0), "stretch")

    def test_every_band_has_a_label(self):
        for _threshold, band in scoring.BANDS:
            with self.subTest(band=band):
                self.assertTrue(scoring.label_for(band))
                self.assertNotEqual(scoring.label_for(band), band)

    def test_an_unknown_band_still_gets_a_label(self):
        self.assertEqual(scoring.label_for(""), "Match")


class ReproducibilityTests(SimpleTestCase):
    def test_the_same_inputs_score_the_same_twice(self):
        # The property `Application.match_snapshot` and the cache both depend
        # on. If this fails, FR-8.5 is freezing noise.
        first = scoring.score(candidate(), job(), credit=credit(PYTHON, DJANGO))
        second = scoring.score(candidate(), job(), credit=credit(PYTHON, DJANGO))
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_the_score_records_the_algorithm_version(self):
        match = scoring.score(candidate(), job(), credit=credit(PYTHON, DJANGO))
        self.assertEqual(match.algorithm_version, scoring.ALGORITHM_VERSION)
        self.assertEqual(match.as_dict()["algorithm_version"], scoring.ALGORITHM_VERSION)

    def test_the_dict_form_carries_the_breakdown(self):
        # The breakdown *is* the explanation (ADR-A6). A snapshot with a score
        # and no working would be a verdict.
        payload = scoring.score(candidate(), job(), credit=credit(PYTHON)).as_dict()
        self.assertEqual(len(payload["components"]), len(scoring.WEIGHTS))
        self.assertTrue(all(item["note"] for item in payload["components"]))


class SkillScoringTests(SimpleTestCase):
    def test_having_every_skill_scores_the_component_in_full(self):
        match = scoring.score(candidate(), job(), credit=credit(PYTHON, DJANGO))
        self.assertEqual(component(match, "skills").points, scoring.WEIGHTS["skills"])
        self.assertEqual(match.matched_skill_ids, [PYTHON, DJANGO])
        self.assertEqual(match.missing_skill_ids, [])

    def test_a_missing_skill_is_named_rather_than_only_counted(self):
        match = scoring.score(candidate(), job(), credit=credit(PYTHON))
        self.assertEqual(match.matched_skill_ids, [PYTHON])
        self.assertEqual(match.missing_skill_ids, [DJANGO])
        self.assertLess(component(match, "skills").points, scoring.WEIGHTS["skills"])

    def test_an_implied_skill_earns_partial_credit(self):
        # SRS §10.2: Django satisfies Python at 0.8, not at 0.
        match = scoring.score(
            candidate(),
            job(job_skill_ids=[PYTHON], job_required_skill_ids=[PYTHON]),
            credit=credit(DJANGO, implied={PYTHON: 0.85}),
        )
        self.assertEqual(match.partial_skill_ids, [PYTHON])
        self.assertGreater(component(match, "skills").points, 0)
        self.assertLess(component(match, "skills").points, scoring.WEIGHTS["skills"])

    def test_weak_implication_counts_as_missing_rather_than_partial(self):
        # A candidate told they "partially match" on a third-order implication
        # would rightly call that noise.
        match = scoring.score(
            candidate(),
            job(job_skill_ids=[KUBERNETES], job_required_skill_ids=[KUBERNETES]),
            credit=credit(DJANGO, implied={KUBERNETES: 0.1}),
        )
        self.assertEqual(match.missing_skill_ids, [KUBERNETES])
        self.assertEqual(match.partial_skill_ids, [])

    def test_a_required_skill_counts_for_more_than_a_preferred_one(self):
        has_required = scoring.score(
            candidate(),
            job(job_skill_ids=[PYTHON, KUBERNETES], job_required_skill_ids=[PYTHON]),
            credit=credit(PYTHON),
        )
        has_preferred = scoring.score(
            candidate(),
            job(job_skill_ids=[PYTHON, KUBERNETES], job_required_skill_ids=[PYTHON]),
            credit=credit(KUBERNETES),
        )
        self.assertGreater(
            component(has_required, "skills").points, component(has_preferred, "skills").points
        )

    def test_an_employer_weight_changes_what_a_skill_is_worth(self):
        heavy = scoring.score(
            candidate(),
            job(
                job_skill_ids=[PYTHON, KUBERNETES],
                job_required_skill_ids=[],
                job_skill_weights={PYTHON: 1.0, KUBERNETES: 3.0},
            ),
            credit=credit(PYTHON),
        )
        even = scoring.score(
            candidate(),
            job(
                job_skill_ids=[PYTHON, KUBERNETES],
                job_required_skill_ids=[],
                job_skill_weights={PYTHON: 1.0, KUBERNETES: 1.0},
            ),
            credit=credit(PYTHON),
        )
        self.assertLess(component(heavy, "skills").points, component(even, "skills").points)

    def test_too_few_years_trims_a_matched_skill_without_zeroing_it(self):
        # Having the skill and less of the history is still having the skill.
        trimmed = scoring.score(
            candidate(skill_years={PYTHON: 1.0, DJANGO: 1.0}),
            job(job_skill_min_years={PYTHON: 5.0, DJANGO: 5.0}),
            credit=credit(PYTHON, DJANGO),
        )
        full = scoring.score(candidate(), job(), credit=credit(PYTHON, DJANGO))

        self.assertLess(component(trimmed, "skills").points, component(full, "skills").points)
        self.assertGreater(component(trimmed, "skills").points, scoring.WEIGHTS["skills"] * 0.4)
        self.assertEqual(trimmed.matched_skill_ids, [PYTHON, DJANGO])

    def test_a_posting_that_lists_no_skills_scores_neutral_not_zero(self):
        # The employer did not say what they want. That is not the candidate's
        # failing, and zeroing half the score would bury every such posting.
        match = scoring.score(
            candidate(),
            job(job_skill_ids=[], job_required_skill_ids=[]),
            credit=credit(PYTHON),
        )
        points = component(match, "skills").points
        self.assertGreater(points, 0)
        self.assertLess(points, scoring.WEIGHTS["skills"])
        self.assertIn("does not list", component(match, "skills").note)

    def test_the_note_says_how_many_matched(self):
        match = scoring.score(candidate(), job(), credit=credit(PYTHON))
        self.assertIn("1 of 2", component(match, "skills").note)


class ExperienceScoringTests(SimpleTestCase):
    def test_meeting_the_band_scores_in_full(self):
        match = scoring.score(
            candidate(experience_months=48), job(job_experience_level="mid"), credit=credit()
        )
        self.assertEqual(component(match, "experience").points, scoring.WEIGHTS["experience"])

    def test_being_short_costs_proportionally(self):
        short = scoring.score(
            candidate(experience_months=12), job(job_experience_level="senior"), credit=credit()
        )
        met = scoring.score(
            candidate(experience_months=96), job(job_experience_level="senior"), credit=credit()
        )
        self.assertLess(component(short, "experience").points, component(met, "experience").points)

    def test_being_well_over_costs_a_little_but_never_disqualifies(self):
        # Treating overqualification as a disqualification is how experienced
        # candidates stop seeing anything.
        over = scoring.score(
            candidate(experience_months=240), job(job_experience_level="junior"), credit=credit()
        )
        points = component(over, "experience").points
        self.assertLess(points, scoring.WEIGHTS["experience"])
        self.assertGreater(points, scoring.WEIGHTS["experience"] * 0.7)

    def test_a_posting_with_no_stated_level_scores_neutral(self):
        match = scoring.score(
            candidate(experience_months=6), job(job_experience_level=""), credit=credit()
        )
        self.assertIn("states no experience", component(match, "experience").note)
        self.assertGreater(component(match, "experience").points, 0)


class LocationScoringTests(SimpleTestCase):
    def test_the_same_city_scores_in_full(self):
        match = scoring.score(candidate(), job(), credit=credit())
        self.assertEqual(component(match, "location").points, scoring.WEIGHTS["location"])

    def test_remote_scores_in_full_for_someone_open_to_it(self):
        match = scoring.score(
            candidate(location_city_id=999),
            job(job_workplace="remote", job_remote_scope="country"),
            credit=credit(),
        )
        self.assertEqual(component(match, "location").points, scoring.WEIGHTS["location"])

    def test_remote_scores_lower_for_someone_who_did_not_ask_for_it(self):
        match = scoring.score(
            candidate(willing_remote=False, open_to=["on_site"]),
            job(job_workplace="remote", job_remote_scope="country"),
            credit=credit(),
        )
        self.assertLess(component(match, "location").points, scoring.WEIGHTS["location"])

    def test_on_site_in_another_city_scores_lowest(self):
        elsewhere = scoring.score(
            candidate(location_city_id=999), job(job_workplace="on_site"), credit=credit()
        )
        hybrid = scoring.score(
            candidate(location_city_id=999), job(job_workplace="hybrid"), credit=credit()
        )
        self.assertLess(
            component(elsewhere, "location").points, component(hybrid, "location").points
        )

    def test_an_unstated_location_is_not_counted_against_the_candidate(self):
        match = scoring.score(
            candidate(location_city_id=None), job(job_workplace="on_site"), credit=credit()
        )
        self.assertGreater(component(match, "location").points, 0)
        self.assertIn("does not say where", component(match, "location").note)


class SalaryScoringTests(SimpleTestCase):
    def test_a_range_that_reaches_the_ask_scores_in_full(self):
        match = scoring.score(candidate(), job(), credit=credit())
        self.assertEqual(component(match, "salary").points, scoring.WEIGHTS["salary"])

    def test_a_range_below_the_ask_costs_proportionally(self):
        close = scoring.score(
            candidate(salary_min_base=1_000_000.0),
            job(job_salary_max_base=900_000.0),
            credit=credit(),
        )
        far = scoring.score(
            candidate(salary_min_base=1_000_000.0),
            job(job_salary_max_base=200_000.0),
            credit=credit(),
        )
        self.assertGreater(component(close, "salary").points, component(far, "salary").points)

    def test_a_posting_with_no_range_scores_neutral(self):
        # Most postings in this market publish nothing. Treating that as a
        # mismatch would score the majority of the corpus down for the
        # employer's choice.
        match = scoring.score(
            candidate(),
            job(job_salary_min_base=None, job_salary_max_base=None),
            credit=credit(),
        )
        self.assertIn("employer has not published", component(match, "salary").note)
        self.assertGreater(component(match, "salary").points, 0)

    def test_no_note_is_addressed_to_the_candidate(self):
        """These notes are read by recruiters too.

        The applicant screen shows the same component breakdown, so a hiring
        manager was reading "In your city" and "this is not counted against
        you" about somebody else. One set of notes, written so both readers
        can use it.
        """
        matches = [
            scoring.score(candidate(), job(), credit=credit()),
            scoring.score(
                candidate(salary_min_base=None, location_city_id=None, industry_ids=[]),
                job(),
                credit=credit(),
            ),
            scoring.score(
                candidate(),
                job(job_salary_min_base=None, job_salary_max_base=None, job_skill_ids=[]),
                credit=credit(),
            ),
        ]
        for match in matches:
            for component in match.components:
                with self.subTest(component=component.key, note=component.note):
                    words = component.note.lower().split()
                    self.assertNotIn("your", words)
                    self.assertNotIn("you", words)

    def test_a_candidate_who_stated_nothing_scores_neutral(self):
        match = scoring.score(candidate(salary_min_base=None), job(), credit=credit())
        self.assertGreater(component(match, "salary").points, 0)

    def test_the_note_says_which_side_is_silent(self):
        """ "No pay range to compare" beside a posting that prints its range.

        The same sentence covered both cases, so a candidate looking at a job
        advertising NPR 120,000–170,000 was told there was no range — which
        reads as a broken platform, and hides the one thing they could do
        about it.

        The wording is deliberately neutral rather than addressed to the
        candidate: a recruiter reads these same notes on the applicant screen,
        and "what you are asking for" is nonsense to them. The candidate's half
        — what to do about it — is added by the screen that can act on it.
        """
        theirs = scoring.score(candidate(salary_min_base=None), job(), credit=credit())
        self.assertIn("No salary expectation on the profile", component(theirs, "salary").note)

        employers = scoring.score(
            candidate(),
            job(job_salary_min_base=None, job_salary_max_base=None),
            credit=credit(),
        )
        self.assertIn("employer has not published", component(employers, "salary").note)


class RecencyScoringTests(SimpleTestCase):
    def test_a_fresh_posting_scores_above_a_stale_one(self):
        fresh = scoring.score(candidate(), job(job_published_days=1), credit=credit())
        stale = scoring.score(candidate(), job(job_published_days=60), credit=credit())
        self.assertGreater(component(fresh, "recency").points, component(stale, "recency").points)

    def test_an_old_posting_says_so(self):
        match = scoring.score(candidate(), job(job_published_days=40), credit=credit())
        self.assertIn("often already filled", component(match, "recency").note)

    def test_recency_can_never_decide_a_match_on_its_own(self):
        # 5% is a tiebreaker, not a ranking. A perfect skills match on an old
        # posting must still beat a poor one posted today.
        strong_old = scoring.score(
            candidate(), job(job_published_days=90), credit=credit(PYTHON, DJANGO)
        )
        weak_new = scoring.score(candidate(), job(job_published_days=0), credit=credit())
        self.assertGreater(strong_old.score, weak_new.score)


class DomainScoringTests(SimpleTestCase):
    def test_a_shared_industry_scores_in_full(self):
        match = scoring.score(candidate(), job(), credit=credit())
        self.assertEqual(component(match, "domain").points, scoring.WEIGHTS["domain"])

    def test_a_different_industry_scores_lower_but_not_zero(self):
        match = scoring.score(candidate(industry_ids=[99]), job(), credit=credit())
        points = component(match, "domain").points
        self.assertLess(points, scoring.WEIGHTS["domain"])
        self.assertGreater(points, 0)

    def test_no_industry_history_is_neutral_rather_than_a_mismatch(self):
        # The common case. Scoring an unknown as a mismatch would penalise every
        # candidate whose past employers are not on the platform.
        unknown = scoring.score(candidate(industry_ids=[]), job(), credit=credit())
        wrong = scoring.score(candidate(industry_ids=[99]), job(), credit=credit())
        self.assertGreater(component(unknown, "domain").points, component(wrong, "domain").points)
        self.assertIn("not counted either way", component(unknown, "domain").note)


class ProvisionalTests(SimpleTestCase):
    def test_provisional_travels_with_the_match(self):
        match = scoring.score(candidate(), job(), credit=credit(PYTHON), provisional=True)
        self.assertTrue(match.provisional)
        self.assertTrue(match.as_dict()["provisional"])

    def test_a_score_is_not_provisional_by_default(self):
        self.assertFalse(scoring.score(candidate(), job(), credit=credit(PYTHON)).provisional)


class FairnessTests(SimpleTestCase):
    """FR-8.9, SRS §10.4. The test that fails the build."""

    def test_an_unlisted_feature_is_refused(self):
        with self.assertRaises(DisallowedFeature):
            check_features({**candidate(), "favourite_colour": "blue"})

    def test_a_protected_attribute_is_refused_by_name(self):
        with self.assertRaises(DisallowedFeature) as caught:
            check_features({**candidate(), "gender": "f"})
        self.assertIn("gender", str(caught.exception))
        self.assertIn("permanently", str(caught.exception))

    def test_every_permanently_excluded_attribute_is_refused(self):
        for name in sorted(NEVER_ALLOWED):
            with self.subTest(feature=name), self.assertRaises(DisallowedFeature):
                check_features({name: "anything"})

    def test_no_permanently_excluded_attribute_is_on_the_allow_list(self):
        # The allow-list and the exclusion list must not overlap. If they ever
        # do, one of them was edited without reading the other.
        self.assertEqual(set(ALLOWED_FEATURES) & NEVER_ALLOWED, set())

    def test_the_scorer_refuses_a_candidate_vector_carrying_a_protected_attribute(self):
        with self.assertRaises(DisallowedFeature):
            scoring.score({**candidate(), "age": 31}, job(), credit=credit(PYTHON))

    def test_the_scorer_refuses_a_job_vector_carrying_one(self):
        with self.assertRaises(DisallowedFeature):
            scoring.score(candidate(), {**job(), "institution": "x"}, credit=credit(PYTHON))

    def test_the_allowed_features_all_carry_a_justification(self):
        # The mechanism is that adding a feature is a written decision. An entry
        # with an empty justification is an entry nobody had to think about.
        for name, reason in ALLOWED_FEATURES.items():
            with self.subTest(feature=name):
                self.assertTrue(reason.strip(), f"{name} has no stated justification")

    def test_the_real_vectors_only_use_allowed_features(self):
        # Belt and braces: the fixtures in this module are what the extractor
        # produces, and both must stay inside the list.
        check_features(candidate())
        check_features(job())


class EndToEndScoreTests(SimpleTestCase):
    def test_a_strong_candidate_lands_in_a_high_band(self):
        match = scoring.score(candidate(), job(), credit=credit(PYTHON, DJANGO, POSTGRES))
        self.assertGreaterEqual(match.score, 85)
        self.assertEqual(match.band, "strong")

    def test_a_candidate_with_nothing_in_common_lands_low(self):
        match = scoring.score(
            candidate(
                skill_ids=[],
                skill_years={},
                experience_months=0,
                industry_ids=[],
                location_city_id=999,
                willing_remote=False,
                open_to=["on_site"],
                salary_min_base=5_000_000.0,
                last_active_days=300,
            ),
            job(job_published_days=120),
            credit=credit(),
        )
        self.assertLess(match.score, 50)
        self.assertEqual(match.band, "stretch")

    def test_the_score_is_bounded_to_zero_and_one_hundred(self):
        for name, vectors in {
            "best": (candidate(), job()),
            "worst": (candidate(skill_ids=[], experience_months=0), job()),
        }.items():
            match = scoring.score(vectors[0], vectors[1], credit=credit(PYTHON, DJANGO))
            with self.subTest(case=name):
                self.assertGreaterEqual(match.score, 0)
                self.assertLessEqual(match.score, 100)


class TruthfulBandTests(SimpleTestCase):
    """The label never claims a fit the breakdown contradicts (MAT-01)."""

    EIGHT = list(range(1, 9))

    def posting(self, required):
        return job(
            job_skill_ids=self.EIGHT,
            job_required_skill_ids=required,
            job_skill_weights=dict.fromkeys(self.EIGHT, 1.0),
        )

    def test_one_missing_required_skill_of_three_caps_at_good(self):
        has = list(range(1, 8))
        match = scoring.score(
            candidate(skill_ids=has, skill_years=dict.fromkeys(has, 4.0)),
            self.posting(required=[6, 7, 8]),
            credit=credit(*has),
        )

        self.assertGreaterEqual(match.score, 85)  # the number is unchanged ...
        self.assertEqual(match.band, "good")  # ... the claim is not "strong"
        self.assertIn(8, match.missing_skill_ids)

    def test_missing_the_only_required_skill_is_at_best_partial(self):
        # The reproduction: 88 and "Strong match" with the one required skill
        # absent. It is half (all) of what is required.
        has = list(range(1, 8))
        match = scoring.score(
            candidate(skill_ids=has, skill_years=dict.fromkeys(has, 4.0)),
            self.posting(required=[8]),
            credit=credit(*has),
        )

        self.assertGreaterEqual(match.score, 85)
        self.assertEqual(match.band, "partial")

    def test_half_the_required_skills_missing_is_at_best_partial(self):
        has = [1, 2, 3, 4, 5, 6]
        match = scoring.score(
            candidate(skill_ids=has, skill_years=dict.fromkeys(has, 4.0)),
            self.posting(required=[5, 6, 7, 8]),
            credit=credit(*has),
        )

        self.assertIn(match.band, {"partial", "stretch"})

    def test_all_required_skills_present_keeps_the_score_band(self):
        has = self.EIGHT
        match = scoring.score(
            candidate(skill_ids=has, skill_years=dict.fromkeys(has, 4.0)),
            self.posting(required=[8]),
            credit=credit(*has),
        )

        self.assertEqual(match.band, scoring.band_for(match.score))

    def test_a_missing_preferred_skill_does_not_cap_the_band(self):
        self.assertEqual(scoring.band_for(90, missing_required=(0, 2)), "strong")
