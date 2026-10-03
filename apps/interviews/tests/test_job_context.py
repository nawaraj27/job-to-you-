"""
Interview questions come from the job they are for (INT-04).

Manual testing found a Backend Developer and a UI/UX Designer being asked the
same questions. Two causes, both asserted here:

  * with no AI kit yet — every first session, and every session on a
    deployment with no model — the engine fell back to the general pools and
    ignored the named job;
  * the kit's prompt never saw the structured posting: the employer's
    responsibility and requirement lists, or which skills were required.

Nothing here depends on one generated sentence. The AI path is asserted by
what reaches `runner.run`; the fallback by whether each posting's own terms
appear in its questions and not in the other posting's.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from apps.accounts.tests.factories import create_user
from apps.candidates.services import profile as profile_service
from apps.companies.tests.factories import company_with_owner
from apps.interviews.services import engine, job_context, planning
from apps.interviews.tests.factories import practice_template
from apps.jobs.models import JobSkill, Requirement
from apps.jobs.tests.factories import create_job, create_skill, publish


def _questions(pools) -> list[str]:
    return [question for pool in pools.values() for question in pool["questions"]]


class PostingFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Practice sessions are metered by plan (ADR-A59), so the catalogue
        # must exist for `engine.start` to resolve one.
        from django.core.management import call_command

        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        owner = create_user()
        self.company, _ = company_with_owner(owner, name="Kora Studio")
        self.backend = self._job(
            title="Backend Developer",
            level="mid",
            required=["Python", "Django", "PostgreSQL"],
            preferred=["REST APIs"],
            responsibilities=[
                "Design and maintain REST APIs for the payments service",
                "Own database migrations on PostgreSQL",
            ],
            requirements=["Three years building Django applications"],
        )
        self.designer = self._job(
            title="UI/UX Designer",
            level="senior",
            required=["Figma", "UX research"],
            preferred=["Prototyping", "Design systems"],
            responsibilities=[
                "Run usability research sessions with merchants",
                "Maintain the product design system in Figma",
            ],
            requirements=["A portfolio of shipped product design work"],
        )
        self.template = practice_template()
        user = create_user()
        self.candidate = profile_service.get_or_create_profile(user=user)
        self.user = user

    def _job(self, *, title, level, required, preferred, responsibilities, requirements):
        job = create_job(
            self.company,
            title=title,
            experience_level=level,
            description=f"{title} at Kora Studio.",
            responsibilities=responsibilities,
            requirements=requirements,
        )
        for order, name in enumerate(required):
            JobSkill.objects.create(
                job=job, skill=self._skill(name), requirement=Requirement.REQUIRED, order=order
            )
        for order, name in enumerate(preferred, start=len(required)):
            JobSkill.objects.create(
                job=job, skill=self._skill(name), requirement=Requirement.PREFERRED, order=order
            )
        return publish(job)

    def _skill(self, name):
        from apps.taxonomy.models import Skill

        existing = Skill.objects.filter(name=name).first()
        return existing or create_skill(name, slug=name.lower().replace(" ", "-").replace("/", "-"))


class ContextTests(PostingFixture):
    def test_the_structured_posting_is_read_in_full(self):
        context = job_context.from_job(self.backend)

        self.assertEqual(context.required_skills, ["Python", "Django", "PostgreSQL"])
        self.assertEqual(context.preferred_skills, ["REST APIs"])
        self.assertIn(
            "Design and maintain REST APIs for the payments service",
            context.all_responsibilities(),
        )
        self.assertEqual(context.requirements, ["Three years building Django applications"])
        self.assertEqual(context.level, "mid")


class AiPlanInputTests(PostingFixture):
    def test_the_model_receives_skills_responsibilities_and_requirements(self):
        with mock.patch.object(planning.runner, "run") as run:
            run.return_value = mock.Mock(status="error", output=None)
            planning.plan_job(job=self.backend)

        inputs = run.call_args.kwargs["inputs"]
        self.assertEqual(inputs["job_title"], "Backend Developer")
        self.assertIn("Django", inputs["required_skills"])
        self.assertIn("REST APIs", inputs["preferred_skills"])
        self.assertIn("Own database migrations on PostgreSQL", inputs["responsibilities"])
        self.assertIn("Three years building Django applications", inputs["requirements"])
        self.assertEqual(inputs["experience_level"], "Mid level")

    def test_materially_different_jobs_send_materially_different_context(self):
        sent = {}
        for job in (self.backend, self.designer):
            with mock.patch.object(planning.runner, "run") as run:
                run.return_value = mock.Mock(status="error", output=None)
                planning.plan_job(job=job)
            sent[job.title] = run.call_args.kwargs["inputs"]

        backend, designer = sent["Backend Developer"], sent["UI/UX Designer"]
        self.assertNotEqual(backend["required_skills"], designer["required_skills"])
        self.assertIn("Figma", designer["required_skills"])
        self.assertNotIn("Figma", backend["required_skills"])
        self.assertIn("usability research", designer["responsibilities"])
        self.assertNotIn("usability research", backend["responsibilities"])

    def test_the_prompt_template_places_every_new_field(self):
        from apps.ai.services import prompts

        prompts.load_all()
        template = prompts.current("interview.plan")
        _system, user = prompts.render(
            template, job_context.from_job(self.designer).as_prompt_inputs()
        )

        for expected in ("Figma", "Prototyping", "portfolio of shipped", "usability research"):
            self.assertIn(expected, user)


class FallbackTests(PostingFixture):
    def test_without_a_kit_a_named_job_gets_questions_from_that_job(self):
        pools, _run, tailored = engine._plan(
            template=self.template, job=self.backend, candidate=self.candidate
        )

        text = " ".join(_questions(pools))
        self.assertIn("Django", text)
        self.assertIn("REST APIs for the payments service", text)
        self.assertIn("Backend Developer", text)
        # Honest about it: built from the posting, not the model's kit.
        self.assertFalse(tailored)

    def test_a_session_opens_on_the_job_aware_pool(self):
        session = engine.start(
            candidate=self.candidate, template=self.template, actor=self.user, job=self.backend
        )
        first = engine.next_question(session=session)

        self.assertIsNotNone(first)

    def test_backend_and_design_fallbacks_differ_by_their_own_terms(self):
        backend = " ".join(
            _questions(
                engine._plan(template=self.template, job=self.backend, candidate=self.candidate)[0]
            )
        )
        designer = " ".join(
            _questions(
                engine._plan(template=self.template, job=self.designer, candidate=self.candidate)[0]
            )
        )

        self.assertIn("Figma", designer)
        self.assertNotIn("Figma", backend)
        self.assertIn("PostgreSQL", backend)
        self.assertNotIn("PostgreSQL", designer)

    def test_seniority_shapes_the_role_questions(self):
        senior = job_context.fallback_pools(job_context.from_job(self.designer), {})
        self.assertTrue(
            any("set the direction" in q for q in senior["role_understanding"]["questions"])
        )

    def test_a_posting_with_no_skills_gets_no_invented_skills_competency(self):
        bare = publish(create_job(self.company, title="Operations Analyst", description=""))

        pools = job_context.fallback_pools(job_context.from_job(bare), {})

        self.assertNotIn("role_skills", pools)
        self.assertIn("role_understanding", pools)

    def test_practice_without_a_job_still_uses_the_general_pools(self):
        pools = engine._plan(template=self.template, job=None, candidate=self.candidate)[0]

        self.assertEqual(set(pools), set(self.template.question_pools))

    def test_a_company_authored_framework_still_wins(self):
        own = practice_template(
            company=self.company,
            slug="company-own",
            question_pools={
                "craft": {"label": "Craft", "why": "x", "questions": ["Ours?", "A?", "B?"]}
            },
        )

        pools = engine._plan(template=own, job=self.backend, candidate=self.candidate)[0]

        self.assertEqual(set(pools), {"craft"})


class DisciplineFallbackTests(PostingFixture):
    """The fallback's questions differ in kind, not only in the nouns (INT-04b).

    Verification on 2026-09-21 found every fallback built from the same five
    sentence templates with the posting's skills and duties substituted: a
    designer was asked when Figma "did not behave the way you expected", and a
    marketing posting's company introduction ("Summit Digital Labs builds
    booking software…") was quoted back as a duty of the job.
    """

    def setUp(self):
        super().setUp()
        self.marketing = self._job(
            title="Marketing Specialist",
            level="entry",
            required=["SEO", "Content marketing"],
            preferred=["Email marketing"],
            responsibilities=[],
            requirements=["Some experience running a campaign"],
        )
        self.marketing.description = (
            "Kora Studio builds booking software for hotels. "
            "You will plan and run seasonal campaigns across email and social media. "
            "You will report on campaign performance every month."
        )
        self.marketing.save(update_fields=["description"])

    def pools(self, job):
        return job_context.fallback_pools(job_context.from_job(job), {})

    def test_each_discipline_gets_its_own_practice_questions(self):
        expected = {
            self.backend.pk: ("engineering", "Engineering practice", "test"),
            self.designer.pk: ("design", "Design practice", "research or testing"),
            self.marketing.pk: ("marketing", "Marketing practice", "measure whether it worked"),
        }
        craft = {}
        for job in (self.backend, self.designer, self.marketing):
            family, label, phrase = expected[job.pk]
            with self.subTest(job=job.title):
                context = job_context.from_job(job)
                self.assertEqual(job_context.discipline(context), family)
                pool = self.pools(job)["role_craft"]
                self.assertEqual(pool["label"], label)
                self.assertTrue(any(phrase in question for question in pool["questions"]))
                craft[job.pk] = set(pool["questions"])

        # No practice question is shared between any two of them.
        self.assertFalse(craft[self.backend.pk] & craft[self.designer.pk])
        self.assertFalse(craft[self.designer.pk] & craft[self.marketing.pk])
        self.assertFalse(craft[self.backend.pk] & craft[self.marketing.pk])

    def test_a_designer_is_not_asked_to_debug_their_tools(self):
        questions = _questions(self.pools(self.designer))

        self.assertFalse(any("did not behave" in question for question in questions))
        self.assertTrue(any("did not behave" in q for q in _questions(self.pools(self.backend))))

    def test_the_company_introduction_is_not_a_duty(self):
        duties = self.pools(self.marketing)["role_responsibilities"]["questions"]

        self.assertTrue(any("seasonal campaigns" in question for question in duties))
        self.assertFalse(any("builds booking software" in question for question in duties))

    def test_structured_responsibilities_are_quoted_and_prose_is_not_repeated(self):
        duties = self.pools(self.backend)["role_responsibilities"]["questions"]

        self.assertEqual(len(duties), 2)
        self.assertFalse(any("at Kora Studio" in question for question in duties))

    def test_a_kit_is_rewritten_when_the_responsibilities_change(self):
        before = planning._digest(self.backend)
        self.backend.responsibilities = [*self.backend.responsibilities, "Run the on-call rota"]
        self.backend.save(update_fields=["responsibilities"])

        self.assertNotEqual(planning._digest(self.backend), before)

    def test_a_kit_is_rewritten_when_a_skill_becomes_required(self):
        before = planning._digest(self.backend)
        self.backend.skills.filter(requirement=Requirement.PREFERRED).update(
            requirement=Requirement.REQUIRED
        )

        self.assertNotEqual(planning._digest(self.backend), before)


class CompanySentenceTests(TestCase):
    """A sentence about the employer is not something the role involves.

    The filter's word boundary had been written into the file as a backspace
    character, so it never matched: "We are a fast-growing startup" was quoted
    to candidates as a duty whenever a posting had no listed responsibilities.
    """

    def test_sentences_about_the_employer_are_not_duties(self):
        from apps.interviews.services.job_context import JobContext, _duties

        context = JobContext(
            title="Backend Developer",
            company="Summit Digital Labs",
            description=(
                "We are a fast-growing travel startup in Pokhara. "
                "Our team ships every week. "
                "You will design the booking API and own its database migrations."
            ),
        )

        self.assertEqual(
            _duties(context), ["You will design the booking API and own its database migrations."]
        )

    def test_a_word_that_only_starts_like_we_is_kept(self):
        from apps.interviews.services.job_context import JobContext, _duties

        context = JobContext(
            title="Web Developer",
            company="Summit Digital Labs",
            description="Website performance work is most of this role, across every page.",
        )

        self.assertEqual(len(_duties(context)), 1)
