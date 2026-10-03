"""
Query budgets on the list screens (SRS NFR-1, P19 audit).

**An N+1 is not slow until it is, and then it is slow all at once.** A job list
that runs one query per row is fine with twelve postings on a laptop and
catastrophic with two hundred on a shared database — and nothing in a unit test
notices, because the assertions are about content.

So these tests assert a *shape* rather than a number: load the list, add rows,
load it again, and require that the query count did not grow. A fixed ceiling
would drift out of date and get raised; "does not scale with rows" is the
property that actually matters and it never needs tuning.

This is not theoretical. It caught one: job search ran a query per posting for
its skill chips — ten queries with ten results, forty with forty — and the fix
was one `prefetch_related` in `apps/jobs/search.py`.

The screens chosen are the ones a real session hits most and the ones whose row
counts grow without bound: job search, the employer pipeline, and the
applications list.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.candidates.tests.factories import create_profile
from apps.companies.tests.factories import company_with_owner
from apps.jobs.tests.factories import create_job, publish
from apps.matching.tests.test_matching import application_for


class QueryBudgetFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)

    def setUp(self):
        self.owner = create_profile().user
        self.company, self.membership = company_with_owner(self.owner)


class JobSearchBudgetTests(QueryBudgetFixture):
    def setUp(self):
        super().setUp()
        self.jobs = [publish(create_job(self.company, title=f"Engineer {n}")) for n in range(12)]

    def count_for(self, url: str) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self.client.get(url)
        return len(captured)

    def test_doubling_the_rows_does_not_change_the_query_count(self):
        first = self.count_for(reverse("jobs:list"))

        for n in range(12, 36):
            publish(create_job(self.company, title=f"Engineer {n}"))

        second = self.count_for(reverse("jobs:list"))
        self.assertLessEqual(
            second,
            first,
            f"Job search runs {second} queries with 36 postings and {first} with 12 — "
            "something in the page is querying per row. Before the P19 audit this was "
            "one query per posting for its skill chips.",
        )


class ApplicationListBudgetTests(QueryBudgetFixture):
    def setUp(self):
        super().setUp()
        self.job = publish(create_job(self.company))
        self.candidate = create_profile()
        self.client.force_login(self.candidate.user)

    def count_for(self, url: str) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self.client.get(url)
        return len(captured)

    def test_the_candidate_application_list_is_flat_in_its_rows(self):
        application_for(self.candidate, self.job)
        first = self.count_for(reverse("applications:index"))

        for n in range(6):
            application_for(self.candidate, publish(create_job(self.company, title=f"Role {n}")))

        second = self.count_for(reverse("applications:index"))
        self.assertLessEqual(
            second,
            first,
            f"The applications list runs {second} queries with seven applications and "
            f"{first} with one.",
        )


class EmployerPipelineBudgetTests(QueryBudgetFixture):
    def setUp(self):
        super().setUp()
        self.job = publish(create_job(self.company))
        self.client.force_login(self.owner)
        self.url = reverse("employer:applicants", args=[self.company.slug])

    def count_for(self, url: str) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self.client.get(url)
        return len(captured)

    def test_the_applicant_list_is_flat_in_its_rows(self):
        application_for(create_profile(), self.job)
        first = self.count_for(self.url)

        for _ in range(8):
            application_for(create_profile(), self.job)

        second = self.count_for(self.url)
        self.assertLessEqual(
            second,
            first,
            f"The applicant list runs {second} queries with nine applicants and {first} "
            "with one — a per-applicant query would make this the first screen to fall "
            "over on a busy posting.",
        )


class BuilderScreenBudgetTests(QueryBudgetFixture):
    """The CV builder screens against NFR-2's ceiling (CV-04).

    A ceiling here as well as the shape assertion above, because this is the
    requirement NFR-2 actually states — twenty-five queries per page — and the
    three builder screens were issuing forty-two. The cost was one taxonomy
    lookup per skill on the CV inside the reconcile pass, so it grew with the
    candidate rather than with anything the page was doing: the flatness test
    below is the one that would have caught it, and the ceiling is what the
    requirement says.

    `LIMIT` is not a tuning knob. Raising it is changing NFR-2.
    """

    LIMIT = 25

    #: Enough skills that a per-term lookup is unmistakable in the count, and
    #: real names so they resolve through the alias and canonical-name layers
    #: rather than all falling to the one trigram pass.
    SKILLS = [
        "Python",
        "Django",
        "PostgreSQL",
        "Redis",
        "Docker",
        "Git",
        "Celery",
        "AWS",
        "Kubernetes",
        "Terraform",
        "Bash",
        "Linux",
    ]

    def setUp(self):
        super().setUp()
        from apps.candidates.models import ProfileExperience
        from apps.candidates.services import profile as profile_service
        from apps.files.models import Attachment
        from apps.resumes import content as content_rules
        from apps.resumes.models import RenderFormat, ResumeRender
        from apps.resumes.services import documents as document_service
        from apps.resumes.services import suggestions as suggestion_service
        from apps.resumes.services import versions as version_service

        self.candidate = create_profile()
        self.candidate.headline = "Backend Engineer"
        self.candidate.phone = "+977 9812345678"
        self.candidate.save()
        self.client.force_login(self.candidate.user)
        profile_service.get_or_create_profile(user=self.candidate.user)

        experiences = [
            ProfileExperience.objects.create(
                profile=self.candidate,
                title=f"Engineer {n}",
                org_name=f"Company {n}",
                start_date="2020-01-01",
            )
            for n in range(6)
        ]

        self.document = document_service.create_document(
            profile=self.candidate, actor=self.candidate.user, name="Backend CV"
        )

        draft = self.document.draft
        content = draft.content
        content["contact"]["headline"] = "Senior Backend Engineer"
        content["summary"] = "Backend engineer with three years building payment systems."
        content_rules.section_by_key(content, "skills")["items"] = list(self.SKILLS)
        # Linked to the profile and deliberately out of step with it, so the
        # reconcile pass has real drift to report rather than an empty list.
        content_rules.section_by_key(content, "experience")["entries"] = [
            {
                **content_rules.new_entry(
                    title=f"Engineer {n}", org=f"Different Org {n}", dates="2020 - 2023"
                ),
                "bullets": [f"Did thing {index}" for index in range(4)],
                "source_ref": f"experience:{row.pk}",
                "sync_state": content_rules.SyncState.LINKED,
            }
            for n, row in enumerate(experiences)
        ]
        draft.content = content
        draft.save()
        self.document.refresh_from_db()

        attachment = Attachment.objects.create(
            storage_key="ab/cd/abcd.pdf",
            filename="cv.pdf",
            mime="application/pdf",
            size=10,
            sha256="b" * 64,
        )
        for n in range(10):
            live = self.document.draft
            body = live.content
            body["summary"] = f"Summary revision {n}"
            live.content = body
            live.save()
            version = version_service.finalize(
                document=self.document,
                actor=self.candidate.user,
                reason="saved",
                change_note=f"note {n}",
            )
            ResumeRender.objects.create(
                version=version,
                template=self.document.template,
                format=RenderFormat.PDF,
                attachment=attachment,
                sha256=f"{n:064d}",
                engine="test",
                engine_version="1",
                pages=2,
            )

        self.document.refresh_from_db()
        for n in range(8):
            suggestion_service.offer(
                draft=self.document.draft,
                target_path=f"contact.field{n}",
                proposed=f"Proposal {n}",
                rationale="because",
            )

        self.urls = {
            name: reverse(name, kwargs={"document_id": self.document.public_id})
            for name in ("resume:editor", "resume:guided", "resume:assistant")
        }

    def count_for(self, url: str) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def test_each_builder_screen_stays_inside_the_page_budget(self):
        for name, url in self.urls.items():
            with self.subTest(screen=name):
                # The first load of a mode creates that mode's builder session
                # and switches the document into it, so it is the expensive
                # one. The budget has to hold there too.
                first = self.count_for(url)
                self.assertLessEqual(
                    first,
                    self.LIMIT,
                    f"{name} issues {first} queries on a first visit; NFR-2 allows {self.LIMIT}.",
                )
                repeat = self.count_for(url)
                self.assertLessEqual(
                    repeat,
                    self.LIMIT,
                    f"{name} issues {repeat} queries on a reload; NFR-2 allows {self.LIMIT}.",
                )

    def test_the_screens_do_not_query_per_skill_on_the_cv(self):
        from apps.resumes import content as content_rules

        for url in self.urls.values():
            self.count_for(url)  # Settle the first-visit writes.
        before = {name: self.count_for(url) for name, url in self.urls.items()}

        draft = self.document.draft
        content = draft.content
        # Doubled, and the new half deliberately resolves to nothing: an
        # unrecognised term is the branch that falls through to the trigram
        # pass, which is where a per-term query would hide.
        content_rules.section_by_key(content, "skills")["items"] = [
            *self.SKILLS,
            *[f"Nonsense term {n}" for n in range(len(self.SKILLS))],
        ]
        draft.content = content
        draft.save()

        for name, url in self.urls.items():
            with self.subTest(screen=name):
                after = self.count_for(url)
                self.assertLessEqual(
                    after,
                    before[name] + 1,
                    f"{name} runs {after} queries with {len(self.SKILLS) * 2} skills on the "
                    f"CV and {before[name]} with {len(self.SKILLS)} — the taxonomy lookup is "
                    "back to one query per term.",
                )
                self.assertLessEqual(after, self.LIMIT)
