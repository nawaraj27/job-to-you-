"""
The cover letter screens (P14), and the job page's apply section.

Two things asserted here that are easy to get wrong and invisible when wrong:

  * **a draft is a POST.** It calls a model and creates a row; a GET a refresh
    repeats would spend somebody's budget twice for one click;
  * **another candidate's letter is a 404**, not a 403. A 403 confirms the
    letter exists, and what a named person wrote to a named employer is exactly
    what should not be confirmable by URL.

The last class covers a defect this phase found: the job page had told
candidates that applying "is not switched on yet" ever since P8 shipped it.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.ai.providers.base import ProviderFailure
from apps.ai.providers.fake import FakeAdapter
from apps.ai.services import prompts
from apps.applications.models import CoverLetter, LetterSource
from apps.applications.services import letters as letter_service
from apps.candidates.models import CandidateSkill
from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import create_company
from apps.jobs.models import JobSkill
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
    "paragraphs": ["I build payment systems.", "Your posting asks for exactly that."],
    # Deliberately wrong, and deliberately still here: the model used to be
    # asked for the gaps and the editor printed whatever came back. It named a
    # skill the candidate had listed, so the screen contradicted the matcher on
    # the same profile. The gap is computed now, and `gaps` is ignored.
    "gaps": ["The posting asks for Django, which your profile does not list."],
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


class LetterScreenFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        prompts.load_all()

    def setUp(self):
        FakeAdapter.reset()
        self.profile = create_profile(headline="Backend engineer")
        self.actor = self.profile.user
        CandidateSkill.objects.create(
            profile=self.profile, skill=Skill.objects.create(name="Django", slug="django")
        )

        self.company = create_company()
        self.job = publish(create_job(self.company))
        # One skill the posting wants and the candidate has not listed, which is
        # what a computed gap is made of.
        JobSkill.objects.create(
            job=self.job, skill=Skill.objects.create(name="Kubernetes", slug="kubernetes")
        )

        self.client.force_login(self.actor)
        self.list_url = reverse("letters:list")
        self.act_url = reverse("letters:act")


class LibraryScreenTests(LetterScreenFixture):
    def test_the_list_renders_when_empty(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No cover letters yet")

    def test_a_letter_can_be_created_from_the_screen(self):
        response = self.client.post(
            self.act_url, {"command": "create", "title": "For the Nexa role"}
        )
        letter = CoverLetter.objects.get(profile=self.profile)

        self.assertRedirects(
            response, reverse("letters:detail", kwargs={"public_id": letter.public_id})
        )
        self.assertEqual(letter.title, "For the Nexa role")

    def test_the_list_shows_where_the_words_came_from(self):
        letter_service.save(profile=self.profile, actor=self.actor, content="Mine", title="Mine")
        response = self.client.get(self.list_url)
        self.assertContains(response, "Written by the candidate")

    def test_a_letter_can_be_edited(self):
        letter = letter_service.save(
            profile=self.profile, actor=self.actor, content="First", title="T"
        )
        url = reverse("letters:detail", kwargs={"public_id": letter.public_id})

        response = self.client.post(url, {"content": "Second", "title": "T"})

        letter.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(letter.content, "Second")

    def test_a_letter_can_be_duplicated_and_deleted(self):
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="Words")

        self.client.post(self.act_url, {"command": "duplicate", "letter": letter.public_id})
        self.assertEqual(CoverLetter.objects.filter(profile=self.profile).count(), 2)

        self.client.post(self.act_url, {"command": "delete", "letter": letter.public_id})
        self.assertFalse(CoverLetter.objects.filter(pk=letter.pk).exists())

    def test_the_limit_is_reported_rather_than_crashing(self):
        for index in range(letter_service.MAX_LETTERS):
            letter_service.save(
                profile=self.profile, actor=self.actor, content="Words", title=str(index)
            )
        source = CoverLetter.objects.filter(profile=self.profile).first()

        response = self.client.post(
            self.act_url, {"command": "duplicate", "letter": source.public_id}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "which is the limit")

    def test_a_sent_letter_is_read_only_on_screen(self):
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="Sent words")
        application_for(self.profile, self.job, cover_letter=letter)

        response = self.client.get(
            reverse("letters:detail", kwargs={"public_id": letter.public_id})
        )
        self.assertContains(response, "stays as it was read")
        self.assertNotContains(response, 'name="content"')

    def test_another_candidates_letter_is_a_404(self):
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="Private")
        self.client.force_login(create_profile().user)

        response = self.client.get(
            reverse("letters:detail", kwargs={"public_id": letter.public_id})
        )
        self.assertEqual(response.status_code, 404)

    def test_another_candidate_cannot_delete_my_letter(self):
        letter = letter_service.save(profile=self.profile, actor=self.actor, content="Private")
        self.client.force_login(create_profile().user)

        response = self.client.post(self.act_url, {"command": "delete", "letter": letter.public_id})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(CoverLetter.objects.filter(pk=letter.pk).exists())

    def test_signing_out_closes_the_screen(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.list_url).status_code, 302)

    def test_an_unknown_command_is_a_404(self):
        self.assertEqual(self.client.post(self.act_url, {"command": "explode"}).status_code, 404)


class DraftScreenTests(LetterScreenFixture):
    def test_a_draft_is_created_and_the_candidate_lands_in_the_editor(self):
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            response = self.client.post(
                self.act_url, {"command": "draft", "job": self.job.slug, "tone": "warm"}
            )

        letter = CoverLetter.objects.get(profile=self.profile)
        self.assertRedirects(
            response, reverse("letters:detail", kwargs={"public_id": letter.public_id})
        )
        self.assertEqual(letter.source, LetterSource.AI_DRAFT)

    def test_the_editor_tells_the_candidate_to_read_it(self):
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            self.client.post(self.act_url, {"command": "draft", "job": self.job.slug})

        letter = CoverLetter.objects.get(profile=self.profile)
        response = self.client.get(
            reverse("letters:detail", kwargs={"public_id": letter.public_id})
        )
        self.assertContains(response, "Read it before you send it")

    def test_the_editor_shows_what_the_draft_could_not_claim(self):
        # Computed, not asked for: the posting's skills minus the candidate's.
        # Knowing what the letter could not claim is more useful than a letter
        # that claims it anyway.
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            self.client.post(self.act_url, {"command": "draft", "job": self.job.slug})

        letter = CoverLetter.objects.get(profile=self.profile)
        response = self.client.get(
            reverse("letters:detail", kwargs={"public_id": letter.public_id})
        )
        self.assertContains(response, "Kubernetes")

    def test_a_gap_the_model_invented_is_not_shown(self):
        # `DRAFT_OUTPUT` names Django, which this candidate has listed. The
        # screen must not tell somebody they are missing a skill the matcher
        # scores as matched.
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            self.client.post(self.act_url, {"command": "draft", "job": self.job.slug})

        letter = CoverLetter.objects.get(profile=self.profile)
        response = self.client.get(
            reverse("letters:detail", kwargs={"public_id": letter.public_id})
        )
        self.assertNotContains(response, "which your profile does not list")

    def test_a_draft_is_a_whole_letter(self):
        # The system prompt tells the model not to write a greeting or a
        # sign-off because "the platform adds those". The platform has to.
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            self.client.post(self.act_url, {"command": "draft", "job": self.job.slug})

        letter = CoverLetter.objects.get(profile=self.profile)
        self.assertTrue(letter.content.startswith(letter_service.SALUTATION))
        self.assertIn(letter_service.SIGN_OFF, letter.content)
        self.assertTrue(letter.content.rstrip().endswith(self.profile.user.full_name))

    def test_an_unavailable_draft_offers_the_editor_rather_than_an_error(self):
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            response = self.client.post(self.act_url, {"command": "draft", "job": self.job.slug})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Write one yourself")
        self.assertFalse(CoverLetter.objects.filter(profile=self.profile).exists())

    def test_the_failure_reason_is_not_shown_to_the_candidate(self):
        # "quota_exceeded" and "provider_error" mean nothing to a candidate and
        # both have the same answer.
        with fake_ai():
            FakeAdapter.queue(
                ProviderFailure("down"), ProviderFailure("down"), ProviderFailure("down")
            )
            response = self.client.post(self.act_url, {"command": "draft", "job": self.job.slug})

        self.assertNotContains(response, "provider_error")

    def test_a_draft_for_an_unpublished_job_is_a_404(self):
        draft_job = create_job(self.company)
        with fake_ai():
            response = self.client.post(self.act_url, {"command": "draft", "job": draft_job.slug})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(FakeAdapter.calls), 0)


class JobPageApplyTests(LetterScreenFixture):
    """The defect P14 found: the job page said applying was unavailable.

    P8 shipped applying. This section went on telling candidates it was "not
    switched on yet" for six phases — INV-6 inverted, hiding a feature that
    works rather than one that does not.
    """

    def test_the_job_page_offers_the_apply_screen(self):
        response = self.client.get(self.job.get_absolute_url())

        self.assertContains(response, reverse("jobs:apply", kwargs={"slug": self.job.slug}))
        self.assertNotContains(response, "not switched on yet")

    def test_the_job_page_offers_a_draft_to_a_signed_in_candidate(self):
        response = self.client.get(self.job.get_absolute_url())
        self.assertContains(response, "Draft a letter")

    def test_an_anonymous_reader_is_not_offered_a_draft(self):
        # Drafting needs a profile to write from, and asking somebody to sign in
        # after they have typed their notes loses the notes.
        self.client.logout()
        response = self.client.get(self.job.get_absolute_url())
        self.assertNotContains(response, "Draft a letter")

    def test_the_draft_form_says_where_the_letter_goes(self):
        response = self.client.get(self.job.get_absolute_url())
        self.assertContains(response, "not to this employer")

    def test_an_external_posting_still_links_out(self):
        external = publish(
            create_job(
                self.company, apply_mode="external", external_url="https://example.test/jobs/1"
            )
        )
        response = self.client.get(external.get_absolute_url())

        self.assertContains(response, "https://example.test/jobs/1")
        self.assertNotContains(response, "Draft a letter")

    def test_applications_switched_off_says_so_instead_of_linking(self):
        import datetime as dt

        from django.utils import timezone

        from apps.core import features
        from apps.core.models import FeatureFlag

        FeatureFlag.objects.create(
            key="applications",
            is_enabled=False,
            expires_on=timezone.localdate() + dt.timedelta(days=7),
        )
        features.invalidate_cache()
        self.addCleanup(features.invalidate_cache)

        response = self.client.get(self.job.get_absolute_url())
        self.assertContains(response, "switched off just now")


class ApplyFormTests(LetterScreenFixture):
    def test_a_saved_letter_can_be_chosen_by_public_id(self):
        letter = letter_service.save(
            profile=self.profile, actor=self.actor, content="Reuse me", title="Reusable"
        )
        response = self.client.get(reverse("jobs:apply", kwargs={"slug": self.job.slug}))

        self.assertContains(response, str(letter.public_id))
        self.assertNotContains(response, f'value="{letter.pk}"')

    def test_the_apply_form_links_to_the_library(self):
        letter_service.save(profile=self.profile, actor=self.actor, content="Reuse me")
        response = self.client.get(reverse("jobs:apply", kwargs={"slug": self.job.slug}))
        self.assertContains(response, reverse("letters:list"))

    def test_a_drafted_letter_is_marked_in_the_chooser(self):
        with fake_ai():
            FakeAdapter.queue(DRAFT_OUTPUT)
            letter_service.draft(profile=self.profile, actor=self.actor, job=self.job)

        response = self.client.get(reverse("jobs:apply", kwargs={"slug": self.job.slug}))
        self.assertContains(response, "drafted")
