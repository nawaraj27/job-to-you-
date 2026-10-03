"""
The Django admin, as a whole (docs/ADMIN.md).

  * **Every model is accounted for**: registered, or edited as an inline of its
    parent. A new model fails here until somebody decides how staff see it.
  * **A superuser can open every page**: list, search and a detail page.
  * **`is_staff` alone opens nothing**. Model pages need model permissions
    (decision D-01), and a view permission grants viewing, not editing.
  * **Records are records**. A view-only admin refuses add, change and delete
    on the server, not only by hiding the buttons.
  * **Secrets and private words never render**: message bodies, notes, cover
    letters, webhook secrets, session keys, invite token hashes, TOTP keys.
"""

from __future__ import annotations

from django.apps import apps
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_staff, create_user
from apps.candidates.tests.factories import create_profile
from apps.common.admin_tools import RecordAdmin
from apps.companies.tests.factories import company_with_owner

SECRET_WORDS = (
    "SECRET-MESSAGE-BODY",
    "SECRET-NOTE-BODY",
    "SECRET-COVER-LETTER",
    "whsec-SECRET-SIGNING",
    "SECRETSESSIONKEY0123456789",
    "SECRET-TOKEN-HASH",
    "SECRET-PHONE-9800000000",
)


#: Kept out of the admin on purpose (decision D-01): their contents are read only
#: through the staff portal's audited screens. Pinned by
#: apps/files/tests/test_admin_privacy.py.
EXCLUDED = {"messaging.Message", "companies.CompanyVerificationDocument"}


def project_models():
    return [m for m in apps.get_models() if m.__module__.startswith("apps.")]


def admin_url(model, view, *args):
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{view}", args=args)


class InventoryTests(TestCase):
    def test_every_model_is_registered_or_an_inline(self):
        registry = admin.site._registry
        inlined = {
            inline.model for model_admin in registry.values() for inline in model_admin.inlines
        }
        missing = [
            m._meta.label
            for m in project_models()
            if m not in registry and m not in inlined and m._meta.label not in EXCLUDED
        ]
        self.assertEqual(missing, [])

    def test_no_record_admin_searches_a_private_field(self):
        for model, model_admin in admin.site._registry.items():
            if not isinstance(model_admin, RecordAdmin):
                continue
            for field in model_admin.search_fields:
                with self.subTest(model=model._meta.label, field=field):
                    self.assertNotIn(field.split("__")[0], model_admin.private_fields)

    def test_the_mfa_secret_is_hidden_by_django_otp_too(self):
        from django.conf import settings

        self.assertTrue(settings.OTP_ADMIN_HIDE_SENSITIVE_DATA)


class PrivateDataFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.accounts.models import UserSession
        from apps.applications.models import CompanyCandidateNote, CoverLetter
        from apps.candidates.models import CandidateProfile
        from apps.companies.models import CompanyInvite
        from apps.messaging.models import Conversation, Message
        from apps.notifications.models import WebhookEndpoint

        cls.owner = create_profile().user
        cls.company, _ = company_with_owner(cls.owner)
        cls.profile = create_profile()
        CandidateProfile.objects.filter(pk=cls.profile.pk).update(
            summary="SECRET-COVER-LETTER summary"
        )
        cls.conversation = Conversation.objects.create(
            company=cls.company, candidate=cls.profile, opened_by=cls.owner, subject="Hello"
        )
        cls.message = Message.objects.create(
            conversation=cls.conversation,
            sender=cls.owner,
            sender_kind="company",
            body="SECRET-MESSAGE-BODY please call",
        )
        cls.note = CompanyCandidateNote.objects.create(
            company=cls.company, candidate=cls.profile, author=cls.owner, body="SECRET-NOTE-BODY"
        )
        cls.letter = CoverLetter.objects.create(
            profile=cls.profile,
            title="SECRET-COVER-LETTER title",
            content="SECRET-COVER-LETTER text",
        )
        cls.endpoint = WebhookEndpoint.objects.create(
            company=cls.company,
            url="https://hooks.example.com/x",
            secret="whsec-SECRET-SIGNING",
            created_by=cls.owner,
        )
        cls.session = UserSession.objects.create(
            user=cls.owner, session_key="SECRETSESSIONKEY0123456789", ip="127.0.0.1"
        )
        cls.invite = CompanyInvite.objects.create(
            company=cls.company,
            email="new@example.com",
            role="recruiter",
            token_hash="SECRET-TOKEN-HASH",
            expires_at=timezone.now(),
            invited_by=cls.owner,
        )
        cls.objects = [
            cls.note,
            cls.letter,
            cls.endpoint,
            cls.session,
            cls.invite,
            cls.conversation,
            cls.profile,
        ]

    def assert_no_secrets(self, response, where):
        body = response.content.decode()
        for word in SECRET_WORDS:
            self.assertNotIn(word, body, f"{word} rendered on {where}")


class SuperuserTests(PrivateDataFixture):
    def setUp(self):
        self.client.force_login(create_staff(is_superuser=True))

    def test_every_list_page_opens_and_searches(self):
        for model, model_admin in admin.site._registry.items():
            if not model.__module__.startswith("apps."):
                continue
            with self.subTest(model=model._meta.label):
                url = admin_url(model, "changelist")
                self.assertEqual(self.client.get(url).status_code, 200)
                if model_admin.search_fields:
                    self.assertEqual(self.client.get(url, {"q": "x"}).status_code, 200)

    def test_private_records_open_without_their_contents(self):
        for obj in self.objects:
            model = type(obj)
            with self.subTest(model=model._meta.label):
                listing = self.client.get(admin_url(model, "changelist"))
                self.assertEqual(listing.status_code, 200)
                self.assert_no_secrets(listing, f"{model.__name__} list")
                detail = self.client.get(admin_url(model, "change", obj.pk))
                self.assertEqual(detail.status_code, 200)
                self.assert_no_secrets(detail, f"{model.__name__} detail")
                history = self.client.get(admin_url(model, "history", obj.pk))
                self.assert_no_secrets(history, f"{model.__name__} history")

    def test_records_cannot_be_changed_added_or_deleted(self):
        from apps.applications.models import CompanyCandidateNote as Note

        self.assertEqual(self.client.get(admin_url(Note, "add")).status_code, 403)
        response = self.client.post(admin_url(Note, "change", self.note.pk), {"body": "edited"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(admin_url(Note, "delete", self.note.pk), {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.note.refresh_from_db()
        self.assertEqual(self.note.body, "SECRET-NOTE-BODY")

    def test_message_text_and_verification_documents_stay_out_of_the_admin(self):
        from apps.companies.models import CompanyVerificationDocument
        from apps.messaging.models import Message

        self.assertNotIn(Message, admin.site._registry)
        self.assertNotIn(CompanyVerificationDocument, admin.site._registry)
        page = self.client.get(admin_url(type(self.conversation), "change", self.conversation.pk))
        self.assert_no_secrets(page, "conversation detail")

    def test_the_mfa_device_key_is_not_shown(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        device = TOTPDevice.objects.create(user=self.owner, name="phone", key="ab" * 20)
        page = self.client.get(admin_url(TOTPDevice, "change", device.pk))
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("ab" * 20, page.content.decode())
        self.assertNotIn("qrcode", page.content.decode())


class StaffPermissionTests(PrivateDataFixture):
    def test_is_staff_alone_opens_no_model_page(self):
        self.client.force_login(create_staff())
        for model in admin.site._registry:
            if not model.__module__.startswith("apps."):
                continue
            with self.subTest(model=model._meta.label):
                self.assertEqual(self.client.get(admin_url(model, "changelist")).status_code, 403)

    def test_a_view_permission_views_and_nothing_more(self):
        from apps.applications.models import CompanyCandidateNote as Note

        staff = create_staff()
        staff.user_permissions.add(Permission.objects.get(codename="view_companycandidatenote"))
        self.client.force_login(staff)
        listing = self.client.get(admin_url(Note, "changelist"))
        self.assertEqual(listing.status_code, 200)
        self.assert_no_secrets(listing, "note list (view permission)")
        self.assertEqual(self.client.get(admin_url(Note, "add")).status_code, 403)
        self.assertEqual(
            self.client.post(admin_url(Note, "delete", self.note.pk), {"post": "yes"}).status_code,
            403,
        )

    def test_a_signed_in_candidate_is_sent_to_the_admin_login(self):
        self.client.force_login(create_user())
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
