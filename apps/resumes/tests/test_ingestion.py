"""
Upload, dedupe, guest storage, claim, parse, propose (P4).

The exit criterion for this phase is a negative: **a guest upload cannot
trigger a single AI call.** There is no AI to call yet, so the test that
matters is the one below it — an unclaimed upload is never *read at all*, by
anything, and the refusal comes from the service rather than from callers
remembering.
"""

from __future__ import annotations

import datetime as dt

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.candidates.models import CandidateSkill
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState, NotFound
from apps.files.constants import ScanStatus
from apps.files.models import Attachment
from apps.resumes import policies, selectors
from apps.resumes.models import (
    ParseStatus,
    ProposalKind,
    ProposalState,
    ResumeImportProposal,
    ResumeUpload,
)
from apps.resumes.parsing import ENGINE_VERSION
from apps.resumes.services import claim as claim_service
from apps.resumes.services import parse as parse_service
from apps.resumes.services import proposals as proposal_service
from apps.resumes.services import upload as upload_service

from .factories import SAMPLE_CV, uploaded_docx, uploaded_pdf


def _clean(upload: ResumeUpload) -> ResumeUpload:
    """Mark the stored file scanned-clean, as the scanner would."""
    Attachment.objects.filter(pk=upload.attachment_id).update(
        scan_status=ScanStatus.CLEAN, scanned_at=timezone.now()
    )
    upload.attachment.refresh_from_db()
    return upload


class UploadTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_an_upload_is_stored_and_queued_for_parsing(self):
        upload, created = upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_pdf()
        )

        self.assertTrue(created)
        self.assertEqual(upload.parse_status, ParseStatus.PENDING)
        self.assertEqual(upload.mime, "application/pdf")
        self.assertTrue(upload.is_claimed)

    def test_the_same_file_twice_is_one_upload(self):
        first, _ = upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_pdf()
        )
        second, created = upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_pdf()
        )

        self.assertEqual(first.pk, second.pk)
        self.assertFalse(created)
        self.assertEqual(ResumeUpload.objects.count(), 1)

    def test_two_candidates_may_hold_the_same_file(self):
        other = create_profile()

        upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_pdf()
        )
        upload_service.upload_for_profile(
            profile=other, actor=other.user, uploaded_file=uploaded_pdf()
        )

        self.assertEqual(ResumeUpload.objects.count(), 2)

    def test_a_format_that_is_not_a_document_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.files.services import UnsupportedFileType

        png = SimpleUploadedFile("cv.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")

        with self.assertRaises(UnsupportedFileType):
            upload_service.upload_for_profile(
                profile=self.profile, actor=self.actor, uploaded_file=png
            )

    def test_uploading_is_announced_and_audited(self):
        from apps.audit.models import AuditLog
        from apps.notifications.models import OutboxEvent

        upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_pdf()
        )

        self.assertTrue(OutboxEvent.objects.filter(type="resume.uploaded").exists())
        self.assertTrue(AuditLog.objects.filter(verb="resume.uploaded").exists())

    def test_deleting_an_upload_removes_it_and_the_file(self):
        upload, _ = upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_pdf()
        )
        attachment_id = upload.attachment_id

        upload_service.delete_upload(upload=upload, actor=self.actor)

        self.assertFalse(ResumeUpload.objects.exists())
        self.assertIsNotNone(Attachment.objects.get(pk=attachment_id).deleted_at)


class GuestUploadTests(TestCase):
    """ADR-A32: stored and scanned, and nothing else."""

    def test_a_guest_upload_has_no_owner_and_is_blocked(self):
        upload, token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())

        self.assertIsNone(upload.profile_id)
        self.assertEqual(upload.parse_status, ParseStatus.BLOCKED)
        self.assertTrue(token)

    def test_the_token_is_stored_hashed(self):
        upload, token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())

        self.assertNotEqual(upload.guest_token, token)
        self.assertEqual(len(upload.guest_token), 64)

    def test_the_token_finds_the_upload(self):
        upload, token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())

        self.assertEqual(upload_service.find_guest_upload(token).pk, upload.pk)

    def test_a_wrong_token_finds_nothing(self):
        upload_service.upload_as_guest(uploaded_file=uploaded_pdf())

        self.assertIsNone(upload_service.find_guest_upload("not-the-token"))
        self.assertIsNone(upload_service.find_guest_upload(""))

    def test_an_unclaimed_upload_is_never_read(self):
        # The P4 exit criterion. Enforced in the service, not by callers
        # remembering — there is no code path that reads an unclaimed file.
        upload, _token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())
        _clean(upload)

        with self.assertRaises(InvalidState):
            parse_service.parse_upload(upload=upload)

        self.assertFalse(hasattr(upload, "parse"))

    def test_the_policy_refuses_an_unclaimed_upload_too(self):
        upload, _token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())

        with self.assertRaises(InvalidState):
            policies.require_processable(upload)

    def test_a_guest_upload_announces_nothing_about_its_contents(self):
        from apps.notifications.models import OutboxEvent

        upload_service.upload_as_guest(uploaded_file=uploaded_pdf())

        event = OutboxEvent.objects.get(type="resume.guest_uploaded")
        self.assertNotIn("profile_public_id", event.payload)
        self.assertNotIn("raw_text", event.payload)


class ClaimTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_claiming_attaches_the_upload_and_unblocks_parsing(self):
        upload, token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())

        claimed = claim_service.claim(token=token, profile=self.profile, actor=self.actor)

        self.assertEqual(claimed.profile_id, self.profile.pk)
        self.assertEqual(claimed.parse_status, ParseStatus.PENDING)
        self.assertIsNotNone(claimed.claimed_at)
        self.assertEqual(claimed.guest_token, "")

    def test_a_claimed_token_cannot_be_used_again(self):
        _upload, token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())
        claim_service.claim(token=token, profile=self.profile, actor=self.actor)

        with self.assertRaises(NotFound):
            claim_service.claim(token=token, profile=create_profile(), actor=self.actor)

    def test_claiming_a_file_the_profile_already_has_keeps_one_copy(self):
        upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_pdf()
        )
        _guest, token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())

        claim_service.claim(token=token, profile=self.profile, actor=self.actor)

        self.assertEqual(ResumeUpload.objects.filter(profile=self.profile).count(), 1)

    def test_claiming_is_announced_and_audited(self):
        from apps.audit.models import AuditLog
        from apps.notifications.models import OutboxEvent

        _upload, token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())
        claim_service.claim(token=token, profile=self.profile, actor=self.actor)

        self.assertTrue(OutboxEvent.objects.filter(type="resume.claimed").exists())
        self.assertTrue(AuditLog.objects.filter(verb="resume.claimed").exists())

    def test_claiming_several_tokens_survives_one_bad_one(self):
        _a, first = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())
        _b, second = upload_service.upload_as_guest(
            uploaded_file=uploaded_docx(text="Different content entirely")
        )

        claimed = claim_service.claim_all_for(
            tokens=[first, "expired", second], profile=self.profile, actor=self.actor
        )

        self.assertEqual(len(claimed), 2)


class SweepTests(TestCase):
    def test_unclaimed_uploads_are_deleted_after_the_window(self):
        upload, _token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())
        ResumeUpload.objects.filter(pk=upload.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=25)
        )

        removed = claim_service.sweep_unclaimed()

        self.assertEqual(removed, 1)
        self.assertFalse(ResumeUpload.objects.exists())

    def test_a_recent_unclaimed_upload_survives(self):
        upload_service.upload_as_guest(uploaded_file=uploaded_pdf())

        self.assertEqual(claim_service.sweep_unclaimed(), 0)

    def test_a_claimed_upload_is_never_swept(self):
        profile = create_profile()
        upload, _ = upload_service.upload_for_profile(
            profile=profile, actor=profile.user, uploaded_file=uploaded_pdf()
        )
        ResumeUpload.objects.filter(pk=upload.pk).update(
            created_at=timezone.now() - dt.timedelta(days=30)
        )

        self.assertEqual(claim_service.sweep_unclaimed(), 0)

    def test_the_command_runs_the_sweep(self):
        upload, _token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())
        ResumeUpload.objects.filter(pk=upload.pk).update(
            created_at=timezone.now() - dt.timedelta(hours=25)
        )

        call_command("sweep_resume_uploads")

        self.assertFalse(ResumeUpload.objects.exists())


class ParseTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        self.upload, _ = upload_service.upload_for_profile(
            profile=self.profile,
            actor=self.actor,
            uploaded_file=uploaded_docx(text=SAMPLE_CV),
        )

    def test_an_unscanned_upload_is_not_read(self):
        with self.assertRaises(InvalidState):
            parse_service.parse_upload(upload=self.upload)

    def test_a_clean_claimed_upload_parses(self):
        _clean(self.upload)

        parse = parse_service.parse_upload(upload=self.upload, actor=self.actor)
        self.upload.refresh_from_db()

        self.assertEqual(self.upload.parse_status, ParseStatus.PARSED)
        self.assertIn("Priya Sharma", parse.raw_text)
        self.assertIn("Python", parse.structured["skills"])
        self.assertEqual(parse.engine_version, ENGINE_VERSION)

    def test_re_parsing_replaces_rather_than_accumulates(self):
        _clean(self.upload)

        parse_service.parse_upload(upload=self.upload)
        parse_service.parse_upload(upload=self.upload)

        from apps.resumes.models import ResumeParse

        self.assertEqual(ResumeParse.objects.filter(upload=self.upload).count(), 1)

    def test_an_unreadable_file_records_why_and_does_not_crash(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.files import services as file_services
        from apps.resumes.models import ResumeUpload
        from apps.resumes.services.validation import CvRejected

        broken = SimpleUploadedFile("broken.pdf", b"%PDF-1.4\n" + b"junk" * 40, "application/pdf")
        # Refused at the door now (services.validation)...
        with self.assertRaises(CvRejected):
            upload_service.upload_for_profile(
                profile=self.profile, actor=self.actor, uploaded_file=broken
            )
        # ...but a file can still fail to read later (a reader upgrade, a
        # storage fault), and the parse must record why rather than crash.
        broken.seek(0)
        attachment = file_services.store(
            uploaded_file=broken,
            accepted_types=upload_service.ACCEPTED_TYPES,
            uploaded_by=self.actor,
            scan=False,
        )
        upload = ResumeUpload.objects.create(
            profile=self.profile,
            attachment=attachment,
            original_filename="broken.pdf",
            mime=attachment.mime,
            size=attachment.size,
            sha256=attachment.sha256,
            parse_status=ParseStatus.PENDING,
        )
        _clean(upload)

        with self.assertRaises(InvalidState):
            parse_service.parse_upload(upload=upload)

        upload.refresh_from_db()
        self.assertEqual(upload.parse_status, ParseStatus.FAILED)
        self.assertTrue(upload.parse_error)

    def test_a_deleted_file_is_not_read(self):
        from apps.files import services as file_services

        _clean(self.upload)
        file_services.soft_delete(attachment=self.upload.attachment, actor=self.actor)
        self.upload.refresh_from_db()

        with self.assertRaises(InvalidState):
            parse_service.parse_upload(upload=self.upload)

    def test_the_parse_event_carries_counts_not_content(self):
        from apps.notifications.models import OutboxEvent

        _clean(self.upload)
        parse_service.parse_upload(upload=self.upload)

        event = OutboxEvent.objects.get(type="resume.parsed")
        self.assertIn("skills_found", event.payload)
        self.assertNotIn("raw_text", event.payload)
        self.assertNotIn("Priya", str(event.payload))


class ProposalTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        upload, _ = upload_service.upload_for_profile(
            profile=self.profile,
            actor=self.actor,
            uploaded_file=uploaded_docx(text=SAMPLE_CV),
        )
        _clean(upload)
        self.parse = parse_service.parse_upload(upload=upload)
        self.proposals = proposal_service.build_from_parse(parse=self.parse, profile=self.profile)

    def test_a_parse_writes_nothing_to_the_profile(self):
        # The whole point of proposals: extraction proposes, the candidate
        # decides, and the profile stays canon (§5.5.3).
        self.assertEqual(self.profile.skills.count(), 0)
        self.assertEqual(self.profile.experiences.count(), 0)
        self.assertTrue(self.proposals)

    def test_skills_are_proposed_with_their_resolved_name(self):
        skills = [p for p in self.proposals if p.kind == ProposalKind.SKILL]

        self.assertIn("Python", [p.label for p in skills])
        self.assertIn("PostgreSQL", [p.label for p in skills])

    def test_accepting_a_skill_goes_through_taxonomy_resolution(self):
        proposal = next(p for p in self.proposals if p.label == "Python")

        proposal_service.accept(proposal=proposal, actor=self.actor)

        record = CandidateSkill.objects.get(profile=self.profile)
        self.assertEqual(record.skill.slug, "python")
        self.assertEqual(record.source, "resume")

    def test_accepting_an_experience_writes_a_dated_row(self):
        proposal = next(p for p in self.proposals if p.kind == ProposalKind.EXPERIENCE)

        proposal_service.accept(proposal=proposal, actor=self.actor)

        entry = self.profile.experiences.first()
        self.assertTrue(entry.title)
        self.assertIsNotNone(entry.start_date)

    def test_rejecting_leaves_the_profile_alone(self):
        proposal = self.proposals[0]

        proposal_service.reject(proposal=proposal, actor=self.actor)
        proposal.refresh_from_db()

        self.assertEqual(proposal.state, ProposalState.REJECTED)
        self.assertEqual(self.profile.skills.count(), 0)

    def test_a_decided_proposal_cannot_be_decided_twice(self):
        proposal = self.proposals[0]
        proposal_service.reject(proposal=proposal, actor=self.actor)

        with self.assertRaises(InvalidState):
            proposal_service.accept(proposal=proposal, actor=self.actor)

    def test_accept_all_reports_what_failed_rather_than_stopping(self):
        result = proposal_service.accept_all(profile=self.profile, actor=self.actor)

        self.assertGreater(result["applied"], 0)
        self.assertEqual(
            ResumeImportProposal.objects.filter(
                profile=self.profile, state=ProposalState.OFFERED
            ).count(),
            0,
        )

    def test_a_skill_already_on_the_profile_is_not_proposed_again(self):
        from apps.candidates.services import skills as candidate_skills

        candidate_skills.add_skill(profile=self.profile, actor=self.actor, text="Python")
        rebuilt = proposal_service.build_from_parse(parse=self.parse, profile=self.profile)

        self.assertNotIn("Python", [p.label for p in rebuilt])

    def test_rebuilding_supersedes_the_previous_offers(self):
        before = {
            proposal_service.identity(row.kind, row.label, row.payload)
            for row in ResumeImportProposal.objects.filter(state=ProposalState.OFFERED)
        }

        proposal_service.build_from_parse(parse=self.parse, profile=self.profile)

        after = list(ResumeImportProposal.objects.filter(state=ProposalState.OFFERED))
        keys = [proposal_service.identity(row.kind, row.label, row.payload) for row in after]

        # One open offer per fact, and the same facts as before: re-reading the
        # same file must replace its offers, not add a second generation of
        # them. A parse is one-to-one on its upload and re-reading updates that
        # row in place, so the guard that excluded "offers from another parse"
        # excluded all of them and left both generations on screen.
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), before)
        self.assertTrue(
            ResumeImportProposal.objects.filter(state=ProposalState.SUPERSEDED).exists()
        )

    def test_a_second_cv_does_not_offer_the_same_things_twice(self):
        """What a candidate with two CVs on file actually saw.

        Most of a second CV is the first one: the same phone number, the same
        degree, the same eight skills. Each parse built its own offers and the
        import list showed every one of them, so the screen was two
        near-identical copies of the same twenty decisions — and accepting one
        copy left the other still sitting there.
        """
        second, _ = upload_service.upload_for_profile(
            profile=self.profile,
            actor=self.actor,
            uploaded_file=uploaded_pdf(text=SAMPLE_CV),
        )
        _clean(second)
        second_parse = parse_service.parse_upload(upload=second)
        proposal_service.build_from_parse(parse=second_parse, profile=self.profile)

        open_offers = ResumeImportProposal.objects.filter(
            profile=self.profile, state=ProposalState.OFFERED
        )
        keys = [proposal_service.identity(row.kind, row.label, row.payload) for row in open_offers]

        self.assertEqual(len(keys), len(set(keys)))

    def test_a_second_cv_still_offers_what_is_genuinely_new(self):
        second, _ = upload_service.upload_for_profile(
            profile=self.profile,
            actor=self.actor,
            uploaded_file=uploaded_pdf(
                text="Priya Sharma\npriya@example.com\n\nSKILLS\nKubernetes, Terraform\n"
            ),
        )
        _clean(second)
        second_parse = parse_service.parse_upload(upload=second)
        built = proposal_service.build_from_parse(parse=second_parse, profile=self.profile)

        self.assertIn("Kubernetes", [row.label for row in built])

    def test_an_unknown_skill_term_lands_in_the_curation_queue(self):
        from apps.taxonomy.models import UnresolvedTerm

        upload, _ = upload_service.upload_for_profile(
            profile=self.profile,
            actor=self.actor,
            uploaded_file=uploaded_docx(text="Skills\nKubeflow Pipelines\n"),
        )
        _clean(upload)
        parse = parse_service.parse_upload(upload=upload)
        proposals = proposal_service.build_from_parse(parse=parse, profile=self.profile)

        unknown = next(p for p in proposals if "Kubeflow" in p.label)
        with self.assertRaises(InvalidState):
            proposal_service.accept(proposal=unknown, actor=self.actor)

        self.assertTrue(UnresolvedTerm.objects.filter(normalised__contains="kubeflow").exists())

    def test_the_phone_number_is_proposed_and_never_applied_silently(self):
        contact = [p for p in self.proposals if p.kind == ProposalKind.CONTACT]

        self.assertTrue(contact)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.phone, "")

        proposal_service.accept(proposal=contact[0], actor=self.actor)
        self.profile.refresh_from_db()
        self.assertIn("9812345678", self.profile.phone)

    def test_an_email_is_never_proposed(self):
        # Email is the account's identity, changed through the verified-email
        # flow, not by a line in a document.
        self.assertFalse(any("example.com" in p.label for p in self.proposals))


class SelectorAndPolicyTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        self.upload, _ = upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_pdf()
        )

    def test_uploads_are_listed_for_their_owner(self):
        self.assertIn(self.upload, selectors.uploads_for(self.profile))

    def test_another_profile_sees_nothing(self):
        other = create_profile()

        self.assertEqual(list(selectors.uploads_for(other)), [])
        self.assertIsNone(selectors.upload_for(other, str(self.upload.public_id)))

    def test_the_owner_policy_refuses_a_stranger(self):
        other = create_profile()

        with self.assertRaises(NotFound):
            policies.require_owner(self.upload, other)

    def test_the_owner_policy_refuses_nobody(self):
        with self.assertRaises(NotFound):
            policies.require_owner(self.upload, None)

    def test_the_owner_passes(self):
        self.assertEqual(policies.require_owner(self.upload, self.profile), self.upload)


class TaskTests(TestCase):
    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user

    def test_the_task_parses_and_builds_proposals(self):
        call_command("seed_taxonomy", quiet_summary=True)
        from apps.resumes.tasks import parse_upload as task

        upload, _ = upload_service.upload_for_profile(
            profile=self.profile, actor=self.actor, uploaded_file=uploaded_docx()
        )
        _clean(upload)

        self.assertEqual(task(upload.pk), "parsed")
        self.assertTrue(ResumeImportProposal.objects.filter(profile=self.profile).exists())

    def test_the_task_refuses_an_unclaimed_upload_without_retrying(self):
        from apps.resumes.tasks import parse_upload as task

        upload, _token = upload_service.upload_as_guest(uploaded_file=uploaded_pdf())
        _clean(upload)

        self.assertEqual(task(upload.pk), "refused")

    def test_a_deleted_upload_is_not_an_error(self):
        from apps.resumes.tasks import parse_upload as task

        self.assertEqual(task(999999), "gone")
