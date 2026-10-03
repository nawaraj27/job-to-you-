"""
Cover letters, written and drafted (FR-6.1, SRS §9.4).

What this file is answerable for:

  * **a draft never claims what the candidate has not stated.** The model is
    given skills and roles as data and nothing else identifying, and the test
    asserts what actually reached the provider (SRS §9.3.7);
  * **the editor works with AI off, out of budget, or the provider down.** A
    failed draft returns `None` and the candidate writes their own letter, which
    is what they did before this feature existed (SRS §9.3.6);
  * **drafting never sends anything.** There is no path from `draft` to an
    application; applying stays a separate, explicit act;
  * a sent letter cannot be edited or deleted, because it is part of what an
    employer read;
  * every letter records where its words came from, and an AI draft is linked
    to the run that wrote it (ADR-A11).

Everything runs against `FakeAdapter` through the real P11 runner. Nothing
patches `runner.run`.
"""

from __future__ import annotations

import datetime as dt
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.ai.models import AIRun, RunStatus
from apps.ai.providers.base import ProviderFailure
from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts
from apps.applications.models import CoverLetter, LetterSource, LetterTone
from apps.applications.services import letters as letter_service
from apps.candidates.models import CandidateSkill, ProfileExperience
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotFound
from apps.companies.tests.factories import create_company
from apps.core import features
from apps.core.models import FeatureFlag
from apps.jobs.models import JobSkill, Requirement
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for
from apps.taxonomy.models import Skill
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

DRAFT_OUTPUT = {
    "paragraphs": [
        "I build payment systems in Django and have run the settlement service at Nexa for two years.",
        "Your posting asks for Postgres at scale, which is most of what that service is.",
        "I would be glad to talk about it.",
    ],
    "used": ["Django", "PostgreSQL", "Backend Engineer at Nexa"],
    "gaps": ["The posting asks for Kubernetes, which your profile does not list."],
}


@contextmanager
def fake_ai():
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


class LetterFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()
        self.profile = create_profile(headline="Backend engineer")
        self.actor = self.profile.user

        self.django = Skill.objects.create(name="Django", slug="django")
        self.kubernetes = Skill.objects.create(name="Kubernetes", slug="kubernetes")
        CandidateSkill.objects.create(profile=self.profile, skill=self.django, years=Decimal("3"))
        ProfileExperience.objects.create(
            profile=self.profile,
            title="Backend Engineer",
            org_name="Nexa Payments",
            start_date=dt.date(2023, 1, 1),
            is_current=True,
        )

        self.company = create_company()
        self.job = publish(create_job(self.company))
        JobSkill.objects.create(
            job=self.job, skill=self.kubernetes, requirement=Requirement.REQUIRED
        )


class WrittenLetterTests(LetterFixture):
    def test_a_letter_the_candidate_writes_is_recorded_as_theirs(self):
        letter = letter_service.save(
            profile=self.profile, actor=self.actor, content="Dear team,", title="Mine"
        )
        self.assertEqual(letter.source, LetterSource.WRITTEN)
        self.assertIsNone(letter.ai_run)
        self.assertEqual(letter.tone, "")

    def test_an_empty_letter_is_refused(self):
        with self.assertRaises(InvalidState):
            letter_service.save(profile=self.profile, actor=self.actor, content="   ")

    def test_a_letter_gets_a_default_title_from_its_job(self):
        letter = letter_service.save(
            profile=self.profile, actor=self.actor, content="Dear team,", job=self.job
        )
        self.assertIn(self.job.title, letter.title)

    def test_content_is_capped(self):
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="x" * 40_000)
        self.assertEqual(len(letter.content), letter_service.MAX_CHARS)

    def test_the_library_is_bounded(self):
        # Past a couple of dozen nobody finds the one they want, and the count
        # is what stops "generate" filling the list.
        for index in range(letter_service.MAX_LETTERS):
            letter_service.save(
                profile=self.profile, actor=self.actor, content="Dear team,", title=str(index)
            )
        with self.assertRaises(InvalidState):
            letter_service.save(profile=self.profile, actor=self.actor, content="One more")

    def test_editing_a_letter_keeps_it_the_candidates(self):
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="First go")
        letter_service.update(letter=letter, actor=self.actor, content="Second go")

        letter.refresh_from_db()
        self.assertEqual(letter.content, "Second go")
        self.assertEqual(letter.source, LetterSource.WRITTEN)

    def test_an_edit_cannot_empty_a_letter(self):
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="Words")
        with self.assertRaises(InvalidState):
            letter_service.update(letter=letter, actor=self.actor, content="")

    def test_a_letter_is_found_by_public_id(self):
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="Words")
        found = letter_service.find(profile=self.profile, public_id=str(letter.public_id))
        self.assertEqual(found, letter)

    def test_another_candidates_letter_is_not_found(self):
        # `NotFound`, never `NotAllowed`: a 403 confirms the letter exists, and
        # what a named person wrote to a named employer is what must not be
        # confirmable.
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="Words")
        stranger = create_profile()
        with self.assertRaises(NotFound):
            letter_service.find(profile=stranger, public_id=str(letter.public_id))

    def test_a_letter_can_be_duplicated(self):
        letter = letter_service.save(
            profile=self.profile, actor=self.actor, content="Words", title="Original"
        )
        copy = letter_service.duplicate(letter=letter, actor=self.actor)

        self.assertNotEqual(copy.pk, letter.pk)
        self.assertEqual(copy.content, letter.content)
        self.assertIn("copy", copy.title)

    def test_a_copy_of_an_ai_draft_is_still_marked_as_one(self):
        # Pretending a copy of a draft was hand-written would make the field
        # useless the first time anybody duplicated anything.
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            letter, _ = letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        copy = letter_service.duplicate(letter=letter, actor=self.actor)
        self.assertEqual(copy.source, LetterSource.AI_DRAFT)
        self.assertEqual(copy.ai_run_id, letter.ai_run_id)

    def test_a_letter_can_be_deleted(self):
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="Words")
        letter_service.delete(letter=letter, actor=self.actor)
        self.assertFalse(CoverLetter.objects.filter(pk=letter.pk).exists())


class ClicheTests(TestCase):
    """The lines a recruiter reads past.

    Pointed at rather than removed: the sentence carrying "passionate about the
    tax engine" is also the sentence carrying the tax engine.
    """

    def test_stock_phrases_are_named_with_what_to_do_about_them(self):
        found = letter_service.cliches_in(
            "I am writing to express my interest. I am passionate about ledgers "
            "and would be a great fit for this role."
        )
        phrases = [item["phrase"] for item in found]

        self.assertIn("i am writing to", phrases)
        self.assertIn("passionate about", phrases)
        self.assertIn("great fit", phrases)
        self.assertTrue(all(item["why"] for item in found))

    def test_a_letter_written_like_a_person_is_left_alone(self):
        found = letter_service.cliches_in(
            "I ran the settlement service at Nexa for two years and cut the "
            "nightly run from four hours to thirty-five minutes."
        )

        self.assertEqual(found, [])


class SentLetterTests(LetterFixture):
    """A letter an employer has read is part of the record."""

    def setUp(self):
        super().setUp()
        self.letter = letter_service.save(
            profile=self.profile, actor=self.actor, content="Dear team,", job=self.job
        )
        application_for(self.profile, self.job, cover_letter=self.letter)
        self.letter.refresh_from_db()

    def test_a_sent_letter_knows_it_is_sent(self):
        self.assertTrue(self.letter.is_sent)

    def test_a_sent_letter_cannot_be_edited(self):
        # Quietly editing a duplicate instead would leave the candidate
        # believing they had changed what the employer will read.
        with self.assertRaises(InvalidState) as caught:
            letter_service.update(letter=self.letter, actor=self.actor, content="Changed")
        self.assertIn("Duplicate", caught.exception.message)

    def test_a_sent_letter_cannot_be_deleted(self):
        with self.assertRaises(InvalidState):
            letter_service.delete(letter=self.letter, actor=self.actor)

    def test_a_sent_letter_can_be_duplicated_and_the_copy_edited(self):
        copy = letter_service.duplicate(letter=self.letter, actor=self.actor)
        letter_service.update(letter=copy, actor=self.actor, content="A revised version")

        copy.refresh_from_db()
        self.assertEqual(copy.content, "A revised version")
        self.assertFalse(copy.is_sent)

    def test_the_original_is_untouched_by_the_copy(self):
        copy = letter_service.duplicate(letter=self.letter, actor=self.actor)
        letter_service.update(letter=copy, actor=self.actor, content="Different")

        self.letter.refresh_from_db()
        self.assertEqual(self.letter.content, "Dear team,")


class DraftTests(LetterFixture):
    def draft(self, **kwargs):
        with fake_ai():
            FakeAdapter.queue(kwargs.pop("output", DRAFT_OUTPUT))
            return letter_service.draft(
                profile=self.profile, actor=self.actor, job=self.job, **kwargs
            )

    def test_a_draft_becomes_a_letter_the_candidate_owns(self):
        letter, run = self.draft()

        self.assertIsNotNone(letter)
        self.assertEqual(letter.profile, self.profile)
        self.assertEqual(letter.source, LetterSource.AI_DRAFT)
        self.assertEqual(run.status, RunStatus.OK)

    def test_the_draft_is_linked_to_the_run_that_wrote_it(self):
        # ADR-A11: a letter a model drafted must be traceable to the call.
        letter, run = self.draft()
        self.assertEqual(letter.ai_run, run)
        self.assertEqual(run.task_key, letter_service.TASK_KEY)

    def test_the_paragraphs_become_one_plain_text_letter(self):
        letter, _ = self.draft()
        for paragraph in DRAFT_OUTPUT["paragraphs"]:
            with self.subTest(paragraph=paragraph[:20]):
                self.assertIn(paragraph, letter.content)
        self.assertIn("\n\n", letter.content)

    def test_a_paragraph_claiming_work_at_the_hiring_company_is_dropped(self):
        """The worst single output this product can produce.

        A draft came back reading "My role at <company> has given me a deep
        understanding of..." — addressed to that company, from somebody who has
        never worked there. The model had the employer's name in front of it and
        wrote the candidate into it.
        """
        invented = f"My role at {self.job.company.name} taught me the ledger inside out."
        letter, _ = self.draft(
            output={
                "paragraphs": [
                    "I build payment systems in Django at Nexa Payments.",
                    invented,
                ]
            }
        )

        self.assertNotIn(invented, letter.content)
        self.assertIn("Nexa Payments", letter.content)

    def test_a_candidate_who_really_works_there_may_say_so(self):
        # An internal move is an ordinary thing to apply for.
        ProfileExperience.objects.create(
            profile=self.profile,
            title="Backend Engineer",
            org_name=self.job.company.name,
            start_date=dt.date(2022, 1, 1),
        )
        true_claim = f"My role at {self.job.company.name} taught me the ledger inside out."

        letter, _ = self.draft(output={"paragraphs": [true_claim]})

        self.assertIn(true_claim, letter.content)

    def test_a_paragraph_claiming_a_skill_they_have_not_listed_is_dropped(self):
        """The model reads the requirement and writes it in.

        A draft said "In my role at Nexa, I developed REST APIs" for a candidate
        whose profile does not list REST APIs, on a posting whose one missing
        skill was REST APIs. The screen tells her she is missing it on the same
        page as the letter claiming it.
        """
        missing = letter_service.missing_skills(profile=self.profile, job=self.job)
        self.assertTrue(missing, "the fixture needs a skill the candidate lacks")
        claim = f"At Nexa I worked with {missing[0]} every day."

        letter, _ = self.draft(output={"paragraphs": ["I build payment systems in Django.", claim]})

        self.assertNotIn(claim, letter.content)

    def test_the_tone_is_recorded(self):
        letter, _ = self.draft(tone=LetterTone.WARM)
        self.assertEqual(letter.tone, LetterTone.WARM)

    def test_an_invented_tone_falls_back_rather_than_erroring(self):
        letter, _ = self.draft(tone="shouty")
        self.assertEqual(letter.tone, LetterTone.DIRECT)

    def test_the_draft_goes_through_the_shared_runner(self):
        # Not a mocked call. The proof this feature has no provider knowledge of
        # its own is that an AIRun exists for it (ADR-A10).
        self.draft()
        run = AIRun.objects.get(task_key=letter_service.TASK_KEY)
        self.assertTrue(run.prompt_version)
        self.assertEqual(run.user, self.actor)

    def test_the_model_is_given_the_candidates_skills_and_roles(self):
        self.draft()
        sent = FakeAdapter.calls[0]["user"]
        self.assertIn("Django", sent)
        self.assertIn("Nexa Payments", sent)

    def test_the_model_is_not_given_contact_details(self):
        # SRS §9.3.7. A letter needs the candidate's name and nothing else
        # identifying, and the name is theirs to put on it.
        self.draft()
        sent = FakeAdapter.calls[0]["user"]
        self.assertNotIn(self.profile.user.email, sent)

    def test_the_posting_is_delimited_as_data(self):
        # A job advertisement is untrusted input that a model reads (SRS §9.7).
        self.draft()
        sent = FakeAdapter.calls[0]["user"]
        self.assertIn("POSTING", sent)
        self.assertIn("not instructions", sent.replace("\n", " "))

    def test_notes_from_the_candidate_reach_the_prompt_as_material(self):
        self.draft(notes="I built their competitor's billing system")
        sent = FakeAdapter.calls[0]["user"]
        self.assertIn("competitor's billing system", sent)
        self.assertIn("NOTES", sent)

    def test_what_the_draft_could_not_claim_is_kept_for_the_candidate(self):
        # The one place the model is allowed to say "you do not have this", and
        # more useful than a letter that claims it anyway.
        letter, run = self.draft()
        self.assertEqual(run.output["gaps"], DRAFT_OUTPUT["gaps"])
        self.assertEqual(letter.ai_run.output["gaps"], DRAFT_OUTPUT["gaps"])

    def test_drafting_sends_nothing(self):
        # There is no code path from drafting to submission.
        from apps.applications.models import Application

        self.draft()
        self.assertFalse(Application.objects.filter(candidate=self.profile).exists())

    def test_a_draft_counts_against_the_library_limit(self):
        for index in range(letter_service.MAX_LETTERS):
            letter_service.save(
                profile=self.profile, actor=self.actor, content="Dear team,", title=str(index)
            )
        with fake_ai(), self.assertRaises(InvalidState):
            FakeAdapter.queue(DRAFT_OUTPUT)
            letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)


class DegradedDraftTests(LetterFixture):
    """Every failure path. The editor has to keep working through all of them."""

    def assert_unavailable(self, run) -> None:
        self.assertFalse(CoverLetter.objects.filter(profile=self.profile).exists())
        # The candidate can still write their own, which is the point.
        letter = letter_service.save(
            profile=self.profile, actor=self.actor, content="I will write it myself."
        )
        self.assertEqual(letter.source, LetterSource.WRITTEN)

    def test_a_provider_outage_returns_no_letter_and_no_exception(self):
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            letter, run = letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        self.assertIsNone(letter)
        self.assertEqual(run.status, RunStatus.PROVIDER_ERROR)
        self.assert_unavailable(run)

    def test_ai_switched_off_returns_no_letter(self):
        FeatureFlag.objects.create(
            key="ai",
            is_enabled=False,
            expires_on=timezone.localdate() + dt.timedelta(days=7),
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

        with fake_ai():
            letter, run = letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        self.assertIsNone(letter)
        self.assertEqual(run.status, RunStatus.DISABLED)
        self.assertEqual(len(FakeAdapter.calls), 0)
        self.assert_unavailable(run)

    def test_an_exhausted_budget_returns_no_letter(self):
        from apps.ai.services import quotas

        quotas.set_limit(owner=self.actor, limit=Decimal("0"))
        with fake_ai():
            letter, run = letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        self.assertIsNone(letter)
        self.assertEqual(run.status, RunStatus.QUOTA_EXCEEDED)
        self.assert_unavailable(run)

    def test_an_output_that_fails_its_schema_returns_no_letter(self):
        with fake_ai():
            FakeAdapter.queue({"nonsense": True})
            letter, run = letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        self.assertIsNone(letter)
        self.assertEqual(run.status, RunStatus.SCHEMA_ERROR)
        self.assert_unavailable(run)

    def test_an_empty_paragraph_list_produces_no_letter_rather_than_an_empty_one(self):
        # A letter with nothing in it is worse than no letter: the candidate
        # opens an editor believing something was written.
        with fake_ai():
            FakeAdapter.queue({"paragraphs": ["   ", ""]})
            letter, run = letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        self.assertIsNone(letter)

    def test_a_failed_run_is_still_recorded(self):
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        self.assertTrue(AIRun.objects.filter(task_key=letter_service.TASK_KEY).exists())


class SourceTransitionTests(LetterFixture):
    def test_editing_an_ai_draft_records_that_a_person_changed_it(self):
        # `ai_edited` is the honest middle state, and the case the product is
        # actually built for.
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            letter, _ = letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        letter_service.update(letter=letter, actor=self.actor, content="My own words now.")

        letter.refresh_from_db()
        self.assertEqual(letter.source, LetterSource.AI_EDITED)

    def test_editing_it_twice_does_not_change_the_source_again(self):
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            letter, _ = letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        letter_service.update(letter=letter, actor=self.actor, content="One")
        letter_service.update(letter=letter, actor=self.actor, content="Two")

        letter.refresh_from_db()
        self.assertEqual(letter.source, LetterSource.AI_EDITED)
        self.assertIsNotNone(letter.ai_run_id)

    def test_the_run_link_survives_editing(self):
        # The words changed; the fact that a model started them did not.
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            letter, run = letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        letter_service.update(letter=letter, actor=self.actor, content="Rewritten")
        letter.refresh_from_db()
        self.assertEqual(letter.ai_run, run)


class ApplyIntegrationTests(LetterFixture):
    def test_the_apply_service_still_stores_a_letter_through_the_moved_module(self):
        # `apply.save_cover_letter` is the name the apply screen calls; P14
        # moved the implementation and kept the name.
        from apps.applications.services import apply as apply_service

        letter = apply_service.save_cover_letter(
            profile=self.profile, actor=self.actor, content="Dear team,", job=self.job
        )
        self.assertEqual(letter.source, LetterSource.WRITTEN)
        self.assertEqual(letter.profile, self.profile)

    def test_the_moved_module_enforces_the_same_limits(self):
        # Two implementations of "store a cover letter" would have disagreed
        # about the length cap within a phase.
        from apps.applications.services import apply as apply_service

        with self.assertRaises(InvalidState):
            apply_service.save_cover_letter(profile=self.profile, actor=self.actor, content="  ")
