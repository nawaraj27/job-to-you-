"""
The deterministic analysis engine (FR-4.2, FR-4.3, FR-4.4, FR-4.5, FR-4.7).

`SimpleTestCase` throughout, and that is the point being made: every module
under `apps.resumes.analysis` is a pure function of a content dict. No database,
no clock beyond the date passed in, no model call. If any test here needed a
database, something in the engine had reached for state it should not have.

The scoring tests pin behaviour rather than exact totals wherever a total would
turn every future weight change into a test rewrite — except `WEIGHTS` summing
to 100 and the empty-CV floor, which are the two facts everything else assumes.
"""

from __future__ import annotations

import datetime as dt

from django.test import SimpleTestCase

from apps.resumes.analysis import checks, findings, insight, scoring
from apps.resumes.analysis import text as reader

TODAY = dt.date(2026, 9, 1)


def cv(**overrides) -> dict:
    """A reasonable CV, which individual tests degrade one part at a time."""
    document = {
        "schema_version": 1,
        "contact": {
            "name": "Priya Sharma",
            "headline": "Backend engineer",
            "email": "priya@example.com",
            "phone": "+977 9812345678",
            "location": "Kathmandu",
        },
        "sections": [
            {
                "id": "sec-summary",
                "key": "summary",
                "title": "Summary",
                "kind": "text",
                "visible": True,
                "order": 0,
                "text": (
                    "Backend engineer with four years building payment systems in Django, "
                    "looking for a role with more ownership of architecture and on-call."
                ),
            },
            {
                "id": "sec-exp",
                "key": "experience",
                "title": "Experience",
                "kind": "entries",
                "visible": True,
                "order": 1,
                "entries": [
                    {
                        "id": "entry-1",
                        "title": "Senior Backend Engineer",
                        "org": "Nexa Payments",
                        "dates": "Aug 2023 - Present",
                        "bullets": [
                            "Built the settlement service handling 40,000 transactions a day.",
                            "Cut p95 latency from 800ms to 120ms by reworking the Django query layer.",
                        ],
                        "source_ref": "",
                        "sync_state": "detached",
                    },
                    {
                        "id": "entry-2",
                        "title": "Junior Developer",
                        "org": "Himal Software",
                        "dates": "Jun 2021 - Jul 2023",
                        "bullets": [
                            "Maintained the reporting API used by 30 internal analysts.",
                        ],
                        "source_ref": "",
                        "sync_state": "detached",
                    },
                ],
            },
            {
                "id": "sec-skills",
                "key": "skills",
                "title": "Skills",
                "kind": "list",
                "visible": True,
                "order": 2,
                "items": ["Python", "Django", "PostgreSQL"],
            },
            {
                "id": "sec-edu",
                "key": "education",
                "title": "Education",
                "kind": "entries",
                "visible": True,
                "order": 3,
                "entries": [
                    {
                        "id": "entry-3",
                        "title": "BSc Computer Science",
                        "org": "Tribhuvan University",
                        "dates": "2017 - 2021",
                        "bullets": [],
                        "source_ref": "",
                        "sync_state": "detached",
                    }
                ],
            },
        ],
    }
    document.update(overrides)
    return document


def entry(document: dict, entry_id: str) -> dict:
    for section in document["sections"]:
        for row in section.get("entries", []):
            if row["id"] == entry_id:
                return row
    raise AssertionError(f"no entry {entry_id}")


class ReaderTests(SimpleTestCase):
    def test_entries_come_back_in_render_order(self):
        rows = reader.entries(cv())
        self.assertEqual([row.id for row in rows], ["entry-1", "entry-2", "entry-3"])

    def test_a_hidden_section_is_not_read(self):
        document = cv()
        document["sections"][1]["visible"] = False
        self.assertEqual(reader.entries_in(document, "experience"), [])

    def test_a_summary_is_found_in_the_section_or_the_top_level_field(self):
        # Both are legal in the content shape. A reader that knew only one would
        # call half the CVs in the system summary-less.
        section_form = cv()
        self.assertIn("payment systems", reader.summary_text(section_form))

        top_level = cv(summary="Backend engineer.")
        top_level["sections"][0]["text"] = ""
        self.assertEqual(reader.summary_text(top_level), "Backend engineer.")

    def test_a_number_in_any_shape_counts_as_a_measure(self):
        for bullet in [
            "Cut latency by 40%",
            "Handled 40,000 transactions",
            "Saved NPR 2,000,000",
            "Led 6 people",
            "Reduced build to 90 seconds",
        ]:
            with self.subTest(bullet=bullet):
                self.assertTrue(reader.has_measure(bullet))

    def test_prose_with_no_quantity_is_not_a_measure(self):
        self.assertFalse(reader.has_measure("Improved the reliability of the service"))

    def test_duty_openers_are_recognised(self):
        self.assertTrue(reader.opens_with_filler("Responsible for the billing service"))
        self.assertTrue(reader.opens_with_filler("Worked on the API"))
        self.assertFalse(reader.opens_with_filler("Built the settlement service"))

    def test_date_ranges_parse_in_the_forms_people_write(self):
        for value, months in [
            ("Aug 2023 - Present", 37),
            ("Jun 2021 – Jul 2023", 25),
            ("2019 to 2021", 35),
            ("January 2020 - March 2020", 2),
        ]:
            with self.subTest(value=value):
                period = reader.parse_period(value, today=TODAY)
                self.assertIsNotNone(period)
                self.assertEqual(period.months, months)

    def test_an_unreadable_range_is_none_rather_than_a_guess(self):
        # None is a real answer, and the caller reports it as a finding. A
        # silently wrong total is worse than an admitted gap.
        self.assertIsNone(reader.parse_period("since forever", today=TODAY))
        self.assertIsNone(reader.parse_period("", today=TODAY))

    def test_a_reversed_range_is_refused(self):
        self.assertIsNone(reader.parse_period("Mar 2024 - Mar 2020", today=TODAY))


class AtsCheckTests(SimpleTestCase):
    def run_checks(self, document: dict, **kwargs) -> dict[str, checks.Check]:
        return {check.key: check for check in checks.ats_checks(document, today=TODAY, **kwargs)}

    def test_a_good_cv_passes_the_checks_that_matter(self):
        results = self.run_checks(cv())
        for key in ("extraction", "contact", "headings", "dates"):
            with self.subTest(key=key):
                self.assertEqual(results[key].status, checks.PASS, results[key].note)

    def test_a_missing_email_fails_rather_than_warns(self):
        # It is the one that costs the application outright: nothing else on the
        # page matters if nobody can reply.
        document = cv()
        document["contact"]["email"] = ""
        result = self.run_checks(document)["contact"]
        self.assertEqual(result.status, checks.FAIL)
        self.assertIn("email", result.note)

    def test_a_missing_phone_is_only_a_warning(self):
        document = cv()
        document["contact"]["phone"] = ""
        self.assertEqual(self.run_checks(document)["contact"].status, checks.WARN)

    def test_parse_warnings_become_an_extraction_warning(self):
        result = self.run_checks(cv(), parse_warnings=["page 2 had no text layer"])
        self.assertEqual(result["extraction"].status, checks.WARN)
        self.assertIn("no text layer", result["extraction"].note)

    def test_a_builder_written_cv_passes_extraction_without_an_upload(self):
        # No parse warnings because there is no file. The check must not invent
        # a problem out of the absence of one.
        self.assertEqual(self.run_checks(cv())["extraction"].status, checks.PASS)

    def test_an_invented_heading_is_flagged(self):
        document = cv()
        document["sections"][1]["key"] = "adventures"
        document["sections"][1]["title"] = "My adventures so far"
        result = self.run_checks(document)["headings"]
        self.assertEqual(result.status, checks.WARN)
        self.assertIn("adventures", result.note)

    def test_an_empty_section_with_an_odd_heading_is_not_flagged(self):
        # A hidden or empty section is not on the page, so it cannot confuse a
        # parser that never sees it.
        document = cv()
        document["sections"].append(
            {
                "id": "x",
                "key": "vibes",
                "title": "Vibes",
                "kind": "list",
                "visible": True,
                "order": 9,
                "items": [],
            }
        )
        self.assertEqual(self.run_checks(document)["headings"].status, checks.PASS)

    def test_unreadable_dates_are_reported_with_a_count(self):
        # Counted over every dated entry, not only the roles: an applicant
        # tracking system reads the dates on a degree exactly as it reads the
        # ones on a job.
        document = cv()
        entry(document, "entry-1")["dates"] = "for a while now"
        result = self.run_checks(document)["dates"]
        self.assertEqual(result.status, checks.WARN)
        self.assertIn("1 of 3", result.note)

    def test_a_cv_with_no_dates_anywhere_is_not_penalised_for_it(self):
        # A student CV has dated education and no employment. Checking only the
        # experience section warned that the dates could not be checked and
        # charged machine-readability points for it — marks lost for never
        # having had a job, in the one part of the report that is meant to be
        # purely about the file.
        document = cv()
        for section in document["sections"]:
            for row in section.get("entries", []) or []:
                row["dates"] = ""
        self.assertEqual(self.run_checks(document)["dates"].status, checks.PASS)

    def test_a_thin_cv_warns_on_length(self):
        document = cv()
        document["sections"][1]["entries"] = []
        document["sections"][0]["text"] = "Engineer."
        self.assertEqual(self.run_checks(document)["length"].status, checks.WARN)

    def test_unmeasured_bullets_warn_on_outcomes(self):
        document = cv()
        for row in document["sections"][1]["entries"]:
            row["bullets"] = ["Improved the service", "Helped the team"]
        self.assertEqual(self.run_checks(document)["outcomes"].status, checks.WARN)

    def test_a_cv_with_no_bullets_fails_outcomes(self):
        document = cv()
        for row in document["sections"][1]["entries"]:
            row["bullets"] = []
        self.assertEqual(self.run_checks(document)["outcomes"].status, checks.FAIL)

    def test_a_generic_filename_is_flagged(self):
        self.assertEqual(self.run_checks(cv(), filename="cv.pdf")["filename"].status, checks.WARN)

    def test_a_named_file_passes(self):
        result = self.run_checks(cv(), filename="Priya Sharma - Backend.pdf")["filename"]
        self.assertEqual(result.status, checks.PASS)

    def test_no_filename_passes_because_exports_are_named_for_you(self):
        self.assertEqual(self.run_checks(cv())["filename"].status, checks.PASS)

    def test_every_check_that_is_not_a_pass_carries_a_note(self):
        # A warning nobody can act on is a warning that trains people to ignore
        # the report.
        document = cv()
        document["contact"] = {"name": "", "email": "", "phone": ""}
        document["sections"][1]["entries"] = []
        for check in checks.ats_checks(document, filename="cv.pdf", today=TODAY):
            with self.subTest(key=check.key):
                if check.status != checks.PASS:
                    self.assertTrue(check.note.strip(), f"{check.key} has no note")


class InsightTests(SimpleTestCase):
    def test_total_experience_is_measured_from_the_dates(self):
        summary = insight.experience_summary(cv(), today=TODAY)
        self.assertEqual(summary["roles"], 2)
        self.assertEqual(summary["undated_roles"], 0)
        self.assertEqual(summary["total_months"], 62)
        self.assertTrue(summary["current"])

    def test_overlapping_roles_are_counted_once(self):
        # Freelancing while employed is not two careers, and a total that says
        # so is a total a recruiter will not trust.
        document = cv()
        entry(document, "entry-2")["dates"] = "Aug 2023 - Present"
        summary = insight.experience_summary(document, today=TODAY)
        self.assertEqual(summary["total_months"], 37)

    def test_an_undated_role_is_counted_but_not_totalled(self):
        document = cv()
        entry(document, "entry-2")["dates"] = "ages ago"
        summary = insight.experience_summary(document, today=TODAY)
        self.assertEqual(summary["undated_roles"], 1)
        self.assertEqual(summary["dated_roles"], 1)

    def test_a_gap_between_roles_is_found(self):
        document = cv()
        entry(document, "entry-2")["dates"] = "Jun 2019 - Jan 2021"
        gaps = insight.experience_summary(document, today=TODAY)["gaps"]
        self.assertEqual(len(gaps), 1)
        self.assertGreaterEqual(gaps[0]["months"], 4)

    def test_a_short_gap_between_jobs_is_not_reported(self):
        document = cv()
        entry(document, "entry-2")["dates"] = "Jun 2021 - Jun 2023"
        self.assertEqual(insight.experience_summary(document, today=TODAY)["gaps"], [])

    def test_a_trailing_gap_after_the_last_role_is_reported(self):
        document = cv()
        entry(document, "entry-1")["dates"] = "Aug 2023 - Dec 2024"
        gaps = insight.experience_summary(document, today=TODAY)["gaps"]
        self.assertTrue(any(gap["to"] == TODAY.isoformat() for gap in gaps))

    def test_levels_come_from_bands_not_a_formula(self):
        self.assertEqual(insight._level(0), "entry")
        self.assertEqual(insight._level(30), "junior")
        self.assertEqual(insight._level(70), "mid")
        self.assertEqual(insight._level(120), "senior")
        self.assertEqual(insight._level(200), "lead")

    def test_rising_titles_are_recognised(self):
        self.assertEqual(insight.experience_summary(cv(), today=TODAY)["progression"], "rising")

    def test_progression_is_unknown_with_one_role(self):
        document = cv()
        document["sections"][1]["entries"] = [entry(document, "entry-1")]
        self.assertEqual(
            insight.experience_summary(document, today=TODAY)["progression"], "unknown"
        )

    def test_a_skill_named_in_a_bullet_is_evidenced(self):
        evidence = insight.skill_evidence(cv())
        supported = {item["term"] for item in evidence["supported"]}
        self.assertIn("Django", supported)
        self.assertTrue(all(item["evidence"] for item in evidence["supported"]))

    def test_a_skill_that_appears_only_in_the_list_is_a_claim(self):
        evidence = insight.skill_evidence(cv())
        self.assertIn("PostgreSQL", evidence["listed_only"])

    def test_coverage_is_zero_with_nothing_listed(self):
        document = cv()
        document["sections"][2]["items"] = []
        evidence = insight.skill_evidence(document)
        self.assertEqual(evidence["coverage"], 0.0)
        self.assertEqual(evidence["listed"], 0)

    def test_readability_counts_what_the_report_states(self):
        facts = insight.readability(cv())
        self.assertEqual(facts["bullets"], 3)
        self.assertEqual(facts["measured_bullets"], 3)
        self.assertTrue(facts["has_summary"])
        self.assertEqual(facts["filler_bullets"], 0)


class ScoringTests(SimpleTestCase):
    def test_the_weights_sum_to_one_hundred(self):
        # Everything else assumes this. A scorer whose weights sum to 97
        # produces a number nobody can reason about.
        self.assertEqual(sum(scoring.WEIGHTS.values()), 100)

    def test_a_good_cv_scores_well(self):
        result = scoring.score_version(cv(), today=TODAY)
        self.assertGreaterEqual(result["score"], 70)
        self.assertEqual(result["algorithm_version"], scoring.ALGORITHM_VERSION)

    def test_an_empty_cv_scores_at_the_floor_rather_than_below_it(self):
        empty = {"schema_version": 1, "contact": {}, "sections": []}
        result = scoring.score_version(empty, today=TODAY)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLess(result["score"], 40)

    def test_the_same_content_scores_the_same_twice(self):
        # The property score history depends on. If this fails, FR-4.8 is
        # comparing noise.
        first = scoring.score_version(cv(), today=TODAY)
        second = scoring.score_version(cv(), today=TODAY)
        self.assertEqual(first, second)

    def test_removing_the_numbers_costs_evidence_points(self):
        weak = cv()
        for row in weak["sections"][1]["entries"]:
            row["bullets"] = ["Responsible for the settlement service", "Helped with reporting"]

        strong_points = _component(scoring.score_version(cv(), today=TODAY), "evidence_and_impact")
        weak_points = _component(scoring.score_version(weak, today=TODAY), "evidence_and_impact")
        self.assertLess(weak_points["points"], strong_points["points"])

    def test_every_component_carries_a_note(self):
        for component in scoring.score_version(cv(), today=TODAY)["components"]:
            with self.subTest(key=component["key"]):
                self.assertTrue(component["note"].strip())

    def test_a_component_never_exceeds_its_weight(self):
        for document in (cv(), {"schema_version": 1, "contact": {}, "sections": []}):
            for component in scoring.score_version(document, today=TODAY)["components"]:
                with self.subTest(key=component["key"]):
                    self.assertLessEqual(component["points"], component["weight"])
                    self.assertGreaterEqual(component["points"], 0)

    def test_a_named_target_scores_coverage_against_that_role(self):
        result = scoring.score_version(
            cv(), target_skills=["Python", "Kubernetes", "Terraform"], today=TODAY
        )
        component = _component(result, "skills_coverage")
        self.assertIn("kubernetes", component["note"].lower())
        self.assertLess(component["points"], component["weight"])

    def test_with_no_target_coverage_is_about_evidence_not_market_demand(self):
        # FR-4.6 needs matching data and arrives with P13. Guessing at demand
        # from a model's opinion of the labour market is exactly what this
        # architecture refuses to do.
        component = _component(scoring.score_version(cv(), today=TODAY), "skills_coverage")
        self.assertIn("backed up", component["note"])

    def test_the_percentage_the_report_draws_is_computed_here(self):
        for component in scoring.score_version(cv(), today=TODAY)["components"]:
            with self.subTest(key=component["key"]):
                self.assertEqual(
                    component["percent"], round(component["points"] / component["weight"] * 100)
                )


class DeterministicFindingTests(SimpleTestCase):
    def findings_for(self, document: dict) -> dict[str, findings.Finding]:
        results = checks.ats_checks(document, today=TODAY)
        return {
            finding.code: finding
            for finding in findings.deterministic_findings(
                document, ats_results=results, today=TODAY
            )
        }

    def test_a_missing_summary_is_a_finding_that_can_be_fixed(self):
        document = cv()
        document["sections"][0]["text"] = ""
        finding = self.findings_for(document)["no_summary"]
        self.assertEqual(finding.section_ref, "summary")
        self.assertTrue(finding.action)

    def test_duty_language_names_the_entry_it_applies_to(self):
        document = cv()
        entry(document, "entry-1")["bullets"] = ["Responsible for the settlement service"]
        finding = self.findings_for(document)["duty_language"]
        self.assertEqual(finding.section_ref, "entry.entry-1.bullets")
        self.assertIn("Responsible", finding.evidence)

    def test_unmeasured_bullets_are_reported_once_per_entry(self):
        document = cv()
        entry(document, "entry-1")["bullets"] = ["Improved things", "Made it better", "Did work"]
        found = findings.deterministic_findings(
            document, ats_results=checks.ats_checks(document, today=TODAY), today=TODAY
        )
        outcomes = [finding for finding in found if finding.code == "no_outcomes"]
        self.assertEqual(len(outcomes), 1)

    def test_a_missing_email_is_critical(self):
        document = cv()
        document["contact"]["email"] = ""
        finding = self.findings_for(document)["contact_incomplete"]
        self.assertEqual(finding.severity, findings.CRITICAL)
        self.assertEqual(finding.section_ref, "contact.email")

    def test_an_empty_skills_list_is_critical_and_points_at_the_section(self):
        document = cv()
        document["sections"][2]["items"] = []
        finding = self.findings_for(document)["no_skills"]
        self.assertEqual(finding.severity, findings.CRITICAL)
        self.assertEqual(finding.section_ref, "section.sec-skills.items")

    def test_unevidenced_skills_are_named(self):
        finding = self.findings_for(cv())["unevidenced_skills"]
        self.assertIn("PostgreSQL", finding.text)

    def test_unreadable_dates_point_at_the_entry(self):
        document = cv()
        entry(document, "entry-1")["dates"] = "a long time"
        finding = self.findings_for(document)["unreadable_dates"]
        self.assertEqual(finding.section_ref, "entry.entry-1.dates")

    def test_a_good_cv_still_gets_strengths(self):
        # A report that lists only faults is a report people stop opening.
        codes = self.findings_for(cv())
        self.assertIn("measured_writing", codes)
        self.assertEqual(codes["measured_writing"].severity, findings.STRENGTH)

    def test_findings_are_ordered_worst_first(self):
        document = cv()
        document["contact"]["email"] = ""
        found = findings.deterministic_findings(
            document, ats_results=checks.ats_checks(document, today=TODAY), today=TODAY
        )
        severities = [finding.severity for finding in found]
        self.assertEqual(
            severities,
            sorted(severities, key=lambda s: ["critical", "improve", "strength"].index(s)),
        )

    def test_every_finding_has_a_code_and_a_title(self):
        for document in (cv(), {"schema_version": 1, "contact": {}, "sections": []}):
            found = findings.deterministic_findings(
                document, ats_results=checks.ats_checks(document, today=TODAY), today=TODAY
            )
            for finding in found:
                with self.subTest(code=finding.code):
                    self.assertTrue(finding.code)
                    self.assertTrue(finding.title)

    def test_an_empty_cv_reports_the_absence_of_experience(self):
        empty = {"schema_version": 1, "contact": {}, "sections": []}
        found = findings.deterministic_findings(
            empty, ats_results=checks.ats_checks(empty, today=TODAY), today=TODAY
        )
        self.assertIn("no_experience", {finding.code for finding in found})

    def test_a_student_cv_is_not_told_its_missing_jobs_are_critical(self):
        """What every student and school leaver on the platform was shown.

        A single CRITICAL finding, "There is no work history … there is nothing
        to assess", on a CV with a degree, four projects and a skills list. It
        is wrong as a fact, and wrong as advice: the only way to act on "add
        your roles" when you have never had a job is to invent one.
        """
        student = {
            "schema_version": 1,
            "contact": {"name": "Sabin", "email": "s@example.com", "phone": "9812345678"},
            "sections": [
                {
                    "id": "s1",
                    "key": "experience",
                    "title": "Experience",
                    "kind": "entries",
                    "visible": True,
                    "order": 0,
                    "entries": [],
                },
                {
                    "id": "s2",
                    "key": "education",
                    "title": "Education",
                    "kind": "entries",
                    "visible": True,
                    "order": 1,
                    "entries": [
                        {
                            "id": "e1",
                            "title": "BSc Computing",
                            "org": "Islington College",
                            "dates": "2022 - 2026",
                            "bullets": [],
                        }
                    ],
                },
                {
                    "id": "s3",
                    "key": "projects",
                    "title": "Projects",
                    "kind": "entries",
                    "visible": True,
                    "order": 2,
                    "entries": [
                        {
                            "id": "p1",
                            "title": "Bus Tracker",
                            "org": "",
                            "dates": "",
                            "bullets": ["A live map of valley routes, built with Flutter."],
                        }
                    ],
                },
            ],
        }

        found = findings.deterministic_findings(
            student, ats_results=checks.ats_checks(student, today=TODAY), today=TODAY
        )
        codes = {finding.code for finding in found}
        self.assertNotIn("no_experience", codes)
        self.assertIn("no_paid_experience", codes)

        finding = next(row for row in found if row.code == "no_paid_experience")
        self.assertEqual(finding.severity, findings.IMPROVE)
        # And the advice must never be "add experience you do not have".
        self.assertIn("projects", finding.text)

    def test_a_cv_with_nothing_on_it_at_all_is_still_critical(self):
        bare = {
            "schema_version": 1,
            "contact": {"name": "Sabin", "email": "s@example.com", "phone": "9812345678"},
            "sections": [],
        }
        found = findings.deterministic_findings(
            bare, ats_results=checks.ats_checks(bare, today=TODAY), today=TODAY
        )
        finding = next(row for row in found if row.code == "no_experience")

        self.assertEqual(finding.severity, findings.CRITICAL)


class ScoreNoteTests(SimpleTestCase):
    """The breakdown is the explanation. It must not contradict the findings."""

    def _cv(self, second_role_bullets):
        return {
            "schema_version": 1,
            "contact": {"name": "Priya", "email": "p@example.com", "phone": "9841220118"},
            "sections": [
                {
                    "id": "s1",
                    "key": "experience",
                    "title": "Experience",
                    "kind": "entries",
                    "visible": True,
                    "order": 0,
                    "entries": [
                        {
                            "id": "e1",
                            "title": "Senior Backend Engineer",
                            "org": "Fonepay",
                            "dates": "2022 - Present",
                            "bullets": [
                                "Cut p99 checkout latency from 1.9s to 420ms.",
                                "Moved 40 services off a shared database.",
                            ],
                        },
                        {
                            "id": "e2",
                            "title": "Backend Engineer",
                            "org": "Himalaya Ledger Systems",
                            "dates": "2019 - 2022",
                            "bullets": second_role_bullets,
                        },
                    ],
                }
            ],
        }

    def _note(self, content):
        result = scoring.score_version(content, today=TODAY)
        return _component(result, "evidence_and_impact")

    def test_full_marks_beside_an_open_finding_explains_itself(self):
        """Both were true, and together they read as a mistake.

        "Evidence and impact 30 / 30" sat directly above "Nothing in Backend
        Engineer at Himalaya Ledger Systems is measured". The score is the
        whole CV and the finding is one role — but a candidate reading a report
        that appears to contradict itself stops trusting the half that was
        right.
        """
        component = self._note(
            self._cv(["Recorded all transactions with the double-entry ledger."])
        )

        self.assertEqual(component["points"], component["weight"])
        self.assertIn("Backend Engineer", component["note"])
        self.assertIn("still has none", component["note"])

    def test_nothing_is_added_when_every_role_is_measured(self):
        component = self._note(self._cv(["Cut reconciliation disputes by 40%."]))

        self.assertEqual(component["points"], component["weight"])
        self.assertNotIn("still has", component["note"])

    def test_nothing_is_added_when_the_cv_does_not_meet_the_bar(self):
        # The note already says "Aim for 35%". There is no contradiction to
        # reconcile, and repeating it would be noise.
        content = self._cv(["Recorded all transactions with the double-entry ledger."])
        content["sections"][0]["entries"][0]["bullets"] = ["Wrote some code.", "Went to meetings."]

        component = self._note(content)

        self.assertLess(component["points"], component["weight"])
        self.assertNotIn("still has", component["note"])


def _component(result: dict, key: str) -> dict:
    return next(component for component in result["components"] if component["key"] == key)


def full_length_cv() -> dict:
    """The fixture CV, padded past the short-CV floor.

    `cv()` is deliberately a compact document — 114 words — which is itself
    under the length threshold. Tests about *other* structure penalties need a
    CV that is not already losing points for being thin, or they cannot tell
    which penalty they measured.
    """
    document = cv()
    entry(document, "entry-1")["bullets"] = [
        "Built the settlement service handling 40,000 transactions a day.",
        "Cut p95 latency from 800ms to 120ms by reworking the Django query layer.",
        "Moved 12 batch jobs onto Celery, taking the nightly window from 4 hours to 35 minutes.",
        "Wrote the reconciliation checks that caught 3 duplicate-payment classes before release.",
        "Ran the on-call rota for 6 engineers and cut paging volume by 40%.",
    ]
    entry(document, "entry-2")["bullets"] = [
        "Maintained the reporting API used by 30 internal analysts.",
        "Added 120 regression tests to the billing importer after a bad release.",
        "Documented the 9 scheduled jobs nobody had written down.",
        "Cut the reporting query set from 40 tables to 12 without losing a column.",
        "Trained 4 analysts on the new export format over 2 sessions.",
    ]
    return document


class StructureScoringTests(SimpleTestCase):
    """The structure component's penalties, one at a time.

    Each is asserted against the same CV with one thing changed, because a test
    that degrades three things at once cannot say which penalty fired.
    """

    def structure(self, document: dict) -> dict:
        return _component(scoring.score_version(document, today=TODAY), "structure_and_length")

    def test_a_well_formed_cv_loses_nothing(self):
        component = self.structure(full_length_cv())
        self.assertEqual(component["points"], component["weight"])
        self.assertIn("Well structured", component["note"])

    def test_the_compact_fixture_is_penalised_for_being_thin(self):
        # 114 words. The rule is doing its job; this pins that it is the length
        # penalty firing and not something else.
        self.assertIn("unfinished", self.structure(cv())["note"])

    def test_a_missing_summary_costs_points(self):
        document = full_length_cv()
        document["sections"][0]["text"] = ""
        component = self.structure(document)
        self.assertLess(component["points"], component["weight"])
        self.assertIn("no summary", component["note"])

    def test_a_one_line_summary_costs_less_than_no_summary(self):
        thin = full_length_cv()
        thin["sections"][0]["text"] = "Backend engineer."
        missing = full_length_cv()
        missing["sections"][0]["text"] = ""
        self.assertGreater(self.structure(thin)["points"], self.structure(missing)["points"])
        self.assertIn("too short", self.structure(thin)["note"])

    def test_an_overlong_bullet_costs_points(self):
        document = full_length_cv()
        document["sections"][1]["entries"][0]["bullets"].append(
            "Built " + "a system " * 45 + "handling 40,000."
        )
        self.assertIn("40 words", self.structure(document)["note"])

    def test_unreadable_dates_cost_points(self):
        document = full_length_cv()
        entry(document, "entry-1")["dates"] = "ages ago"
        self.assertIn("readable dates", self.structure(document)["note"])

    def test_a_gap_costs_points(self):
        document = full_length_cv()
        entry(document, "entry-2")["dates"] = "Jun 2018 - Jan 2020"
        self.assertIn("gap", self.structure(document)["note"])

    def test_a_very_long_cv_costs_points(self):
        document = cv()
        entry(document, "entry-1")["bullets"] = [
            f"Shipped release {index} covering 40,000 records." for index in range(200)
        ]
        self.assertIn("two pages", self.structure(document)["note"])

    def test_a_thin_cv_costs_points(self):
        document = cv()
        document["sections"][1]["entries"] = []
        document["sections"][0]["text"] = "Engineer."
        self.assertIn("unfinished", self.structure(document)["note"])

    def test_penalties_never_take_a_component_below_zero(self):
        document = cv()
        document["sections"][0]["text"] = ""
        document["sections"][1]["entries"] = []
        self.assertGreaterEqual(self.structure(document)["points"], 0)


class ExtractionScoringTests(SimpleTestCase):
    def test_a_failed_extraction_is_the_expensive_one(self):
        clean = _component(scoring.score_version(cv(), today=TODAY), "machine_readability")
        broken = _component(
            scoring.score_version(cv(), parse_warnings=["page 2 is an image"], today=TODAY),
            "machine_readability",
        )
        self.assertLess(broken["points"], clean["points"])
        self.assertIn("Extraction is the expensive one", broken["note"])

    def test_a_missing_email_costs_more_than_a_soft_warning(self):
        no_email = cv()
        no_email["contact"]["email"] = ""
        odd_heading = cv()
        odd_heading["sections"][2]["key"] = "stuff"
        odd_heading["sections"][2]["title"] = "Stuff I know"

        self.assertLess(
            _component(scoring.score_version(no_email, today=TODAY), "machine_readability")[
                "points"
            ],
            _component(scoring.score_version(odd_heading, today=TODAY), "machine_readability")[
                "points"
            ],
        )
