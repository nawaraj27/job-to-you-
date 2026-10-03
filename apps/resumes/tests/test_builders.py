"""
Suggestions, the reconcile protocol and the three builder modes (P5).

The invariant under test throughout: **nothing reaches a draft without an
explicit accept** (INV-3). P5 has no AI, so the suggestions here come from the
reconcile protocol — but the path an accept takes is the one P12's generated
wording will use, which is why it is worth testing now rather than then.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from apps.candidates.services import profile as profile_service
from apps.candidates.services import sections as section_service
from apps.candidates.services import skills as candidate_skills
from apps.candidates.tests.factories import create_profile
from apps.common.errors import InvalidState
from apps.resumes import content as content_rules
from apps.resumes.models import BuilderMode, SuggestionState
from apps.resumes.services import builders as builder_service
from apps.resumes.services import documents as document_service
from apps.resumes.services import drafts as draft_service
from apps.resumes.services import reconcile as reconcile_service
from apps.resumes.services import suggestions as suggestion_service


class BuilderFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_resume_templates", quiet_summary=True)
        call_command("seed_plans", quiet_summary=True)
        call_command("seed_taxonomy", quiet_summary=True)

    def setUp(self):
        self.profile = create_profile()
        self.actor = self.profile.user
        self.document = document_service.create_document(
            profile=self.profile, actor=self.actor, name="My CV"
        )
        self.draft = self.document.draft


class SuggestionTests(BuilderFixture):
    def test_a_suggestion_does_not_touch_the_draft_until_accepted(self):
        before = self.draft.content["summary"]

        suggestion_service.offer(
            draft=self.draft, target_path="summary", proposed="Backend engineer, three years."
        )
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content["summary"], before)

    def test_accepting_writes_through_the_normal_edit_path(self):
        suggestion = suggestion_service.offer(
            draft=self.draft, target_path="summary", proposed="Backend engineer, three years."
        )

        suggestion_service.accept(suggestion=suggestion, actor=self.actor)
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content["summary"], "Backend engineer, three years.")
        # Snapshotted like any hand edit, so it is undoable.
        self.assertTrue(self.draft.snapshots.exists())

    def test_accepting_is_undoable(self):
        suggestion = suggestion_service.offer(
            draft=self.draft, target_path="contact.headline", proposed="Backend Engineer"
        )
        suggestion_service.accept(suggestion=suggestion, actor=self.actor)

        draft_service.undo(draft=self.draft)
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content["contact"]["headline"], "")

    def test_rejecting_leaves_the_draft_alone(self):
        suggestion = suggestion_service.offer(
            draft=self.draft, target_path="summary", proposed="Something else."
        )

        suggestion_service.reject(suggestion=suggestion, actor=self.actor)
        self.draft.refresh_from_db()

        self.assertEqual(suggestion.state, SuggestionState.REJECTED)
        self.assertEqual(self.draft.content["summary"], "")

    def test_a_decided_suggestion_cannot_be_decided_again(self):
        suggestion = suggestion_service.offer(
            draft=self.draft, target_path="summary", proposed="Text."
        )
        suggestion_service.reject(suggestion=suggestion, actor=self.actor)

        with self.assertRaises(InvalidState):
            suggestion_service.accept(suggestion=suggestion, actor=self.actor)

    def test_offering_the_same_thing_twice_is_one_suggestion(self):
        first = suggestion_service.offer(
            draft=self.draft, target_path="summary", proposed="Same text."
        )
        second = suggestion_service.offer(
            draft=self.draft, target_path="summary", proposed="Same text."
        )

        self.assertEqual(first.pk, second.pk)

    def test_an_empty_proposal_is_refused(self):
        with self.assertRaises(InvalidState):
            suggestion_service.offer(draft=self.draft, target_path="summary", proposed="   ")

    def test_bullets_can_be_suggested_for_an_entry(self):
        experience = content_rules.section_by_key(self.draft.content, "experience")
        entry_id = draft_service.add_entry(
            draft=self.draft, section_id=experience["id"], title="Engineer", org="Nexa"
        )

        suggestion = suggestion_service.offer(
            draft=self.draft,
            target_path=f"entry.{entry_id}.bullets",
            proposed="Cut p95 latency from 800ms to 120ms.\nBuilt the settlement service.",
        )
        suggestion_service.accept(suggestion=suggestion, actor=self.actor)
        self.draft.refresh_from_db()

        _section, entry = content_rules.entry_by_id(self.draft.content, entry_id)
        self.assertEqual(len(entry["bullets"]), 2)

    def test_a_path_the_vocabulary_does_not_cover_is_refused(self):
        suggestion = suggestion_service.offer(
            draft=self.draft, target_path="anything.goes", proposed="x"
        )

        with self.assertRaises(InvalidState):
            suggestion_service.accept(suggestion=suggestion, actor=self.actor)

    def test_accepting_is_audited(self):
        from apps.audit.models import AuditLog

        suggestion = suggestion_service.offer(
            draft=self.draft, target_path="summary", proposed="Text."
        )
        suggestion_service.accept(suggestion=suggestion, actor=self.actor)

        self.assertTrue(AuditLog.objects.filter(verb="resume.suggestion_accepted").exists())


class ReconcileTests(BuilderFixture):
    def test_a_fresh_document_has_no_drift(self):
        result = reconcile_service.reconcile(self.document)

        self.assertFalse(result.has_drift)

    def test_a_profile_headline_the_cv_lacks_is_reported(self):
        profile_service.update_basics(
            profile=self.profile, actor=self.actor, headline="Backend Engineer"
        )
        self.document.refresh_from_db()

        result = reconcile_service.reconcile(self.document)

        paths = [drift.target_path for drift in result.profile_to_document]
        self.assertIn("contact.headline", paths)

    def test_profile_drift_becomes_a_suggestion_not_a_write(self):
        profile_service.update_basics(
            profile=self.profile, actor=self.actor, headline="Backend Engineer"
        )
        self.document.refresh_from_db()

        offered = reconcile_service.offer_suggestions(document=self.document, actor=self.actor)
        self.draft.refresh_from_db()

        self.assertGreater(offered, 0)
        # Reported, never applied: the document's wording is the candidate's.
        self.assertEqual(self.draft.content["contact"]["headline"], "")
        self.assertTrue(self.draft.suggestions.filter(state=SuggestionState.OFFERED).exists())

    def test_running_reconcile_twice_does_not_stack_duplicates(self):
        profile_service.update_basics(
            profile=self.profile, actor=self.actor, headline="Backend Engineer"
        )
        self.document.refresh_from_db()

        reconcile_service.offer_suggestions(document=self.document, actor=self.actor)
        reconcile_service.offer_suggestions(document=self.document, actor=self.actor)

        self.assertEqual(self.draft.suggestions.filter(target_path="contact.headline").count(), 1)

    def test_an_edited_entry_is_never_offered_the_profiles_wording(self):
        # An `edited` entry is the candidate's own text, and offering to
        # overwrite it would be the platform arguing with them.
        role = section_service.add_entry(
            profile=self.profile,
            actor=self.actor,
            section="experience",
            title="Backend Engineer",
            org_name="Nexa Payments",
            start_date="2023-08-01",
        )
        experience = content_rules.section_by_key(self.draft.content, "experience")
        entry_id = draft_service.add_entry(
            draft=self.draft, section_id=experience["id"], title="Senior Engineer", org="Nexa"
        )

        content = self.draft.content
        _section, entry = content_rules.entry_by_id(content, entry_id)
        entry["source_ref"] = f"experience:{role.pk}"
        entry["sync_state"] = content_rules.SyncState.EDITED
        draft_service.autosave(draft=self.draft, content=content)
        self.document.refresh_from_db()

        result = reconcile_service.reconcile(self.document)

        self.assertEqual([d for d in result.profile_to_document if d.entry_id == entry_id], [])

    def test_a_linked_entry_that_diverged_is_reported(self):
        role = section_service.add_entry(
            profile=self.profile,
            actor=self.actor,
            section="experience",
            title="Backend Engineer",
            org_name="Nexa Payments",
            start_date="2023-08-01",
        )
        experience = content_rules.section_by_key(self.draft.content, "experience")
        entry_id = draft_service.add_entry(
            draft=self.draft,
            section_id=experience["id"],
            title="Junior Engineer",
            org="Nexa Payments",
        )

        content = self.draft.content
        _section, entry = content_rules.entry_by_id(content, entry_id)
        entry["source_ref"] = f"experience:{role.pk}"
        entry["sync_state"] = content_rules.SyncState.LINKED
        draft_service.autosave(draft=self.draft, content=content)
        self.document.refresh_from_db()

        result = reconcile_service.reconcile(self.document)

        titles = [drift.proposed for drift in result.profile_to_document]
        self.assertIn("Backend Engineer", titles)

    def test_a_role_removed_from_the_profile_is_reported(self):
        role = section_service.add_entry(
            profile=self.profile,
            actor=self.actor,
            section="experience",
            title="Backend Engineer",
            org_name="Nexa",
            start_date="2023-08-01",
        )
        experience = content_rules.section_by_key(self.draft.content, "experience")
        entry_id = draft_service.add_entry(draft=self.draft, section_id=experience["id"])

        content = self.draft.content
        _section, entry = content_rules.entry_by_id(content, entry_id)
        entry["source_ref"] = f"experience:{role.pk}"
        entry["sync_state"] = content_rules.SyncState.LINKED
        draft_service.autosave(draft=self.draft, content=content)

        section_service.remove_entry(
            profile=self.profile, actor=self.actor, section="experience", entry_id=role.pk
        )
        self.document.refresh_from_db()

        result = reconcile_service.reconcile(self.document)

        self.assertTrue(
            any("no longer on your profile" in d.detail for d in result.profile_to_document)
        )

    def test_cv_skills_missing_from_the_profile_are_reported_not_added(self):
        # The "two skills on your CV are not on your profile" line, and the
        # reason it is a report: CandidateSkill is canon (ADR-A49).
        skills = content_rules.section_by_key(self.draft.content, "skills")
        draft_service.set_section_items(
            draft=self.draft, section_id=skills["id"], items=["Python", "Django"]
        )
        self.document.refresh_from_db()

        result = reconcile_service.reconcile(self.document)

        self.assertEqual(len(result.document_to_profile), 2)
        self.assertEqual(self.profile.skills.count(), 0)

    def test_a_skill_already_on_the_profile_is_not_reported(self):
        candidate_skills.add_skill(profile=self.profile, actor=self.actor, text="Python")
        skills = content_rules.section_by_key(self.draft.content, "skills")
        draft_service.set_section_items(
            draft=self.draft, section_id=skills["id"], items=["Python", "Django"]
        )
        self.document.refresh_from_db()

        result = reconcile_service.reconcile(self.document)

        self.assertEqual([d.label for d in result.document_to_profile], ["Django"])

    def test_an_unresolvable_cv_skill_is_not_reported_as_drift(self):
        skills = content_rules.section_by_key(self.draft.content, "skills")
        draft_service.set_section_items(
            draft=self.draft, section_id=skills["id"], items=["Underwater basket weaving"]
        )
        self.document.refresh_from_db()

        result = reconcile_service.reconcile(self.document)

        self.assertEqual(result.document_to_profile, [])


class BuilderModeTests(BuilderFixture):
    def test_all_three_modes_share_one_draft(self):
        document_service.set_mode(document=self.document, actor=self.actor, mode=BuilderMode.GUIDED)
        draft_service.set_contact(draft=self.draft, field="name", value="Priya")
        document_service.set_mode(document=self.document, actor=self.actor, mode=BuilderMode.EDITOR)
        self.document.refresh_from_db()

        self.assertEqual(self.document.draft.content["contact"]["name"], "Priya")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(InvalidState):
            document_service.set_mode(document=self.document, actor=self.actor, mode="telepathy")

    def test_opening_the_mode_the_document_is_already_in_writes_nothing(self):
        """Every builder screen calls this on every GET (CV-04).

        Writing the row anyway cost the page an UPDATE inside a transaction it
        did not need, and moved `updated_at` — so "last edited" became "last
        looked at" for a candidate who only opened their CV to read it.
        """
        document_service.set_mode(document=self.document, actor=self.actor, mode=BuilderMode.GUIDED)
        self.document.refresh_from_db()
        untouched_since = self.document.updated_at

        with self.assertNumQueries(0):
            document_service.set_mode(
                document=self.document, actor=self.actor, mode=BuilderMode.GUIDED
            )

        self.document.refresh_from_db()
        self.assertEqual(self.document.updated_at, untouched_since)
        self.assertEqual(self.document.builder_mode, BuilderMode.GUIDED)

    def test_the_guided_flow_reports_real_completion(self):
        state = builder_service.guided_state(document=self.document)

        self.assertEqual(state["completed"], 0)
        self.assertEqual(state["current"], "contact")

        draft_service.set_contact(draft=self.draft, field="name", value="Priya")
        draft_service.set_contact(draft=self.draft, field="email", value="p@example.com")
        self.document.refresh_from_db()

        state = builder_service.guided_state(document=self.document)
        self.assertEqual(state["completed"], 1)

    def test_completion_follows_the_draft_rather_than_a_stored_flag(self):
        draft_service.set_contact(draft=self.draft, field="name", value="Priya")
        draft_service.set_contact(draft=self.draft, field="email", value="p@example.com")
        self.document.refresh_from_db()
        self.assertEqual(builder_service.guided_state(document=self.document)["completed"], 1)

        # Emptying the section makes the step incomplete again, which a stored
        # flag would get wrong in exactly the case that matters.
        draft_service.set_contact(draft=self.draft, field="name", value="")
        draft_service.set_contact(draft=self.draft, field="email", value="")
        self.document.refresh_from_db()

        self.assertEqual(builder_service.guided_state(document=self.document)["completed"], 0)

    def test_the_current_step_survives_leaving_and_returning(self):
        builder_service.set_step(document=self.document, step="skills")

        self.assertEqual(builder_service.guided_state(document=self.document)["current"], "skills")

    def test_an_unknown_step_is_refused(self):
        with self.assertRaises(InvalidState):
            builder_service.set_step(document=self.document, step="astrology")

    def test_the_assistant_transcript_is_kept_on_the_document(self):
        builder_service.record_turn(
            document=self.document, role="candidate", text="I worked at Nexa for two years."
        )
        builder_service.record_turn(
            document=self.document, role="assistant", text="What did you build there?"
        )

        session = builder_service.session_for(document=self.document, mode=BuilderMode.ASSISTANT)
        self.assertEqual(len(session.transcript), 2)

    def test_the_transcript_is_bounded(self):
        for index in range(builder_service.MAX_TRANSCRIPT_TURNS + 10):
            builder_service.record_turn(
                document=self.document, role="candidate", text=f"Turn {index}"
            )

        session = builder_service.session_for(document=self.document, mode=BuilderMode.ASSISTANT)
        self.assertEqual(len(session.transcript), builder_service.MAX_TRANSCRIPT_TURNS)

    def test_an_empty_turn_is_refused(self):
        with self.assertRaises(InvalidState):
            builder_service.record_turn(document=self.document, role="candidate", text="  ")

    def test_a_turn_from_nobody_is_refused(self):
        with self.assertRaises(InvalidState):
            builder_service.record_turn(document=self.document, role="oracle", text="Hello")

    def test_each_mode_keeps_its_own_session(self):
        builder_service.set_step(document=self.document, step="skills")
        builder_service.record_turn(document=self.document, role="candidate", text="Hello")

        self.assertEqual(self.document.builder_sessions.count(), 2)

    def test_the_assistant_writes_nothing_to_the_draft_by_itself(self):
        # INV-3, stated as a test: a conversation is a conversation until the
        # candidate accepts something.
        before = dict(self.draft.content)
        builder_service.record_turn(
            document=self.document, role="assistant", text="I suggest: Backend engineer."
        )
        self.draft.refresh_from_db()

        self.assertEqual(self.draft.content, before)
