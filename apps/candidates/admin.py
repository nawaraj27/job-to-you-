"""
Candidate profiles in the admin: metadata only (docs/ADMIN.md).

A profile is somebody's CV in structured form. The staff portal reads it
behind a `StaffRole` and an access log (decision D-01); the Django admin, which
has neither, shows who and how the profile is set up and never its contents —
no phone number, summary, salary, history or links. Read-only throughout.
"""

from __future__ import annotations

from django.contrib import admin

from apps.common.admin_tools import RecordAdmin

from .models import (
    BlockedCompany,
    CandidateProfile,
    CandidateSkill,
    ProfileAward,
    ProfileCertification,
    ProfileEducation,
    ProfileExperience,
    ProfileLanguage,
    ProfileLink,
    ProfileProject,
    ProfileView,
    SavedJob,
    SavedSearch,
)

_PROFILE_PRIVATE = (
    "phone",
    "summary",
    "headline",
    "salary_min",
    "salary_max",
    "salary_min_base",
    "salary_max_base",
    "fx_rate_id",
    "fx_rate_value",
    "fx_as_of",
    "fx_source",
    "open_to",
    "embedding",
    "embedding_model",
    "embedded_at",
    "search_vector",
    "location",
)


@admin.register(CandidateProfile)
class CandidateProfileAdmin(RecordAdmin):
    private_fields = _PROFILE_PRIVATE
    list_display = (
        "public_id",
        "user",
        "visibility",
        "seeking",
        "completeness",
        "is_searchable",
        "created_at",
    )
    list_filter = ("visibility", "seeking", "is_searchable", "allow_contact")
    search_fields = ("user__email",)
    date_hierarchy = "created_at"


class _ProfileSectionAdmin(RecordAdmin):
    """One section of a CV: whose, and when — not what it says."""

    list_display: tuple[str, ...] = ("id", "profile", "created_at", "updated_at")
    safe_name = True
    search_fields = ("profile__user__email",)


@admin.register(ProfileExperience)
class ProfileExperienceAdmin(_ProfileSectionAdmin):
    private_fields = (
        "title",
        "org_name",
        "summary",
        "bullets",
        "location",
        "company",
        "start_date",
        "end_date",
    )


@admin.register(ProfileEducation)
class ProfileEducationAdmin(_ProfileSectionAdmin):
    private_fields = (
        "institution",
        "qualification",
        "field_of_study",
        "summary",
        "start_date",
        "end_date",
    )


@admin.register(ProfileAward)
class ProfileAwardAdmin(_ProfileSectionAdmin):
    private_fields = ("name", "issuer", "summary", "awarded_on")


@admin.register(ProfileCertification)
class ProfileCertificationAdmin(_ProfileSectionAdmin):
    private_fields = (
        "name",
        "issuer",
        "credential_id",
        "credential_url",
        "file",
        "issued_on",
        "expires_on",
    )


@admin.register(ProfileProject)
class ProfileProjectAdmin(_ProfileSectionAdmin):
    private_fields = ("name", "url", "summary", "started_on", "finished_on")


@admin.register(ProfileLink)
class ProfileLinkAdmin(_ProfileSectionAdmin):
    private_fields = ("url", "label")
    list_display = ("id", "profile", "kind", "status", "last_checked_at")
    list_filter = ("kind", "status")


@admin.register(ProfileLanguage)
class ProfileLanguageAdmin(RecordAdmin):
    list_display = ("profile", "language", "proficiency")
    list_filter = ("proficiency",)


@admin.register(CandidateSkill)
class CandidateSkillAdmin(RecordAdmin):
    private_fields = ("evidence_ref",)
    list_display = ("profile", "skill", "level", "years", "source")
    list_filter = ("source", "level")
    search_fields = ("skill__name",)


@admin.register(BlockedCompany)
class BlockedCompanyAdmin(RecordAdmin):
    private_fields = ("reason",)
    list_display = ("profile", "company", "created_at")


@admin.register(ProfileView)
class ProfileViewAdmin(RecordAdmin):
    list_display = ("candidate", "company", "actor", "source", "created_at")
    list_filter = ("source",)
    date_hierarchy = "created_at"


@admin.register(SavedJob)
class SavedJobAdmin(RecordAdmin):
    private_fields = ("note",)
    list_display = ("profile", "job", "created_at")


@admin.register(SavedSearch)
class SavedSearchAdmin(RecordAdmin):
    private_fields = ("query", "name")
    list_display = ("id", "profile", "alert_frequency", "last_alerted_at", "last_run_at")
    list_filter = ("alert_frequency",)
