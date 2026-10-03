"""
Two owners removing each other at the same moment (SRS §2.6, last-owner rule).

The rule is a count followed by a write. Without a lock both requests counted
one other owner, both wrote, and the company was left with nobody able to
invite, verify or pay. `TransactionTestCase` because the defence is a row lock,
which cannot be observed inside one shared transaction; the window is widened
the same way `apps/billing/tests/test_claim.py` widens its own, because in real
use it is microseconds.
"""

from __future__ import annotations

import threading
import time
from unittest import mock

from django.db import connections
from django.test import TransactionTestCase

from apps.accounts.tests.factories import create_user
from apps.common.errors import DomainError
from apps.companies.models import CompanyMembership
from apps.companies.permissions import Role
from apps.companies.services import membership as membership_service
from apps.companies.tests.factories import add_member, company_with_owner


class ConcurrentOwnerRemovalTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.first_user = create_user()
        self.second_user = create_user()
        self.company, self.first = company_with_owner(self.first_user)
        self.second = add_member(self.company, self.second_user, Role.OWNER)

    def test_two_owners_removing_each_other_leave_one_owner(self):
        real_check = membership_service._require_another_owner

        def slow_check(**kwargs):
            real_check(**kwargs)
            time.sleep(0.4)  # both requests are now past the count

        outcomes: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def attempt(actor_user, actor_membership, target):
            try:
                barrier.wait(timeout=5)
                membership_service.remove_member(
                    actor=actor_user, actor_membership=actor_membership, target=target
                )
                outcomes.append("removed")
            except DomainError:
                outcomes.append("refused")
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)
            finally:
                connections.close_all()

        with mock.patch.object(
            membership_service, "_require_another_owner", side_effect=slow_check
        ):
            threads = [
                threading.Thread(target=attempt, args=(self.first_user, self.first, self.second)),
                threading.Thread(target=attempt, args=(self.second_user, self.second, self.first)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), ["refused", "removed"])
        self.assertEqual(
            CompanyMembership.objects.owners().filter(company=self.company).count(),
            1,
            "both removals succeeded — the company has no owner",
        )
