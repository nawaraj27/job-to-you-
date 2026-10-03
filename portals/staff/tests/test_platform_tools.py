"""
The §3.12 platform tools that live in Django admin (OPS-01).

§3.12 lists feature flags, the audit-log viewer and the AI run inspector among
the platform tools. They are admin surfaces rather than staff screens (ADR-A5),
so the properties that matter are the admin's: who can open them, that the
records of what happened cannot be rewritten through them, and that the one
thing an operator *does* change at runtime — a flag — leaves an audit entry.
It did not: a flag switched off during an incident was recorded nowhere.
"""

from __future__ import annotations

import datetime as dt

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import create_user
from apps.audit.models import AuditLog
from apps.core.models import FeatureFlag


def _admin():
    user = create_user()
    user.is_staff = True
    user.is_superuser = True
    user.save(update_fields=["is_staff", "is_superuser"])
    return user


class FeatureFlagAuditTests(TestCase):
    def setUp(self):
        self.operator = _admin()
        self.client.force_login(self.operator)
        self.expires = timezone.localdate() + dt.timedelta(days=30)
        self.flag = FeatureFlag.objects.create(
            key="jobs", is_enabled=False, expires_on=self.expires
        )
        self.change_url = reverse("admin:core_featureflag_change", args=[self.flag.pk])

    def form(self, **overrides):
        data = {
            "key": "jobs",
            "is_enabled": "on",
            "rollout_percent": 100,
            "expires_on": self.expires.isoformat(),
            "notes": "",
        }
        data.update(overrides)
        return {key: value for key, value in data.items() if value is not None}

    def test_switching_a_flag_is_audited_with_who_and_what(self):
        response = self.client.post(self.change_url, self.form())

        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(verb="core.feature_flag_changed")
        self.assertEqual(entry.actor, self.operator)
        self.assertEqual(entry.metadata["key"], "jobs")
        self.assertTrue(entry.metadata["is_enabled"])
        self.assertIn("is_enabled", entry.metadata["changed"])

    def test_the_change_cannot_be_attributed_to_somebody_else(self):
        somebody_else = create_user()

        self.client.post(self.change_url, self.form(updated_by=somebody_else.pk))

        self.flag.refresh_from_db()
        self.assertEqual(self.flag.updated_by, self.operator)
        entry = AuditLog.objects.get(verb="core.feature_flag_changed")
        self.assertNotIn("updated_by", entry.metadata["changed"])

    def test_creating_a_flag_is_audited(self):
        self.client.post(
            reverse("admin:core_featureflag_add"),
            self.form(key="new_thing", is_enabled=None, rollout_percent=25),
        )

        entry = AuditLog.objects.get(verb="core.feature_flag_created")
        self.assertEqual(entry.metadata["rollout_percent"], 25)

    def test_deleting_a_flag_is_audited_before_the_row_goes(self):
        self.client.post(
            reverse("admin:core_featureflag_delete", args=[self.flag.pk]), {"post": "yes"}
        )

        self.assertFalse(FeatureFlag.objects.filter(pk=self.flag.pk).exists())
        self.assertEqual(
            AuditLog.objects.get(verb="core.feature_flag_deleted").metadata["key"], "jobs"
        )

    def test_the_bulk_delete_action_is_audited_per_flag(self):
        other = FeatureFlag.objects.create(key="talent", expires_on=self.expires)

        self.client.post(
            reverse("admin:core_featureflag_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [self.flag.pk, other.pk],
                "post": "yes",
            },
        )

        self.assertEqual(FeatureFlag.objects.count(), 0)
        self.assertEqual(
            sorted(
                AuditLog.objects.filter(verb="core.feature_flag_deleted").values_list(
                    "metadata__key", flat=True
                )
            ),
            ["jobs", "talent"],
        )

    def test_a_flag_cannot_be_given_an_expiry_in_the_past(self):
        # §17.4: every flag has an expiry date. One already expired on save is
        # a dead branch created on purpose.
        response = self.client.post(self.change_url, self.form(expires_on="2020-01-01"))

        self.assertEqual(response.status_code, 200)
        self.flag.refresh_from_db()
        self.assertEqual(self.flag.expires_on, self.expires)
        self.assertFalse(AuditLog.objects.filter(verb="core.feature_flag_changed").exists())


class RecordInspectorTests(TestCase):
    """The audit-log viewer and the AI run inspector read; they never write."""

    def test_neither_record_can_be_added_changed_or_deleted_through_admin(self):
        self.client.force_login(_admin())

        for model in ("audit_auditlog", "ai_airun"):
            with self.subTest(model=model):
                self.assertEqual(
                    self.client.get(reverse(f"admin:{model}_changelist")).status_code, 200
                )
                self.assertEqual(self.client.get(reverse(f"admin:{model}_add")).status_code, 403)

    def test_an_audit_row_opens_read_only(self):
        self.client.force_login(_admin())
        entry = AuditLog.objects.create(verb="company.verified", target_repr="Nexa")

        response = self.client.post(
            reverse("admin:audit_auditlog_change", args=[entry.pk]), {"verb": "rewritten"}
        )

        self.assertEqual(response.status_code, 403)
        entry.refresh_from_db()
        self.assertEqual(entry.verb, "company.verified")

    def test_is_staff_without_model_permissions_opens_none_of_them(self):
        user = create_user()
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.client.force_login(user)

        for name in ("audit_auditlog", "ai_airun", "core_featureflag"):
            with self.subTest(model=name):
                self.assertEqual(
                    self.client.get(reverse(f"admin:{name}_changelist")).status_code, 403
                )

    def test_a_candidate_is_sent_to_the_admin_sign_in(self):
        self.client.force_login(create_user())

        response = self.client.get(reverse("admin:ai_airun_changelist"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])
