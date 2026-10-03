"""
Taxonomy curation in Django admin (ADR-A5).

This is the one part of the platform where staff genuinely maintain data by
hand, so the admin has to support the operations curation actually consists of —
merging duplicates, retiring obsolete skills, fixing aliases — rather than
leaving staff to edit `status` and `replaced_by` by hand and hope the pair stays
coherent.

Two rules shape everything here:

  * **Curation goes through services.** Merging repoints aliases, translations,
    external ids and relations, and writes an audit entry. Doing it by hand in
    the change form gets the first part wrong and skips the second.
  * **Referenced rows cannot be deleted.** A skill with aliases, relations or
    external ids is load-bearing: deleting it silently discards those rows and
    orphans every record that pointed at it. Retiring is always available and
    is always the right answer.
"""

from __future__ import annotations

from collections.abc import Mapping

from django import forms
from django.contrib import admin, messages
from django.template.response import TemplateResponse
from django.utils.html import format_html, format_html_join

from apps.common.errors import DomainError

from .models import (
    FxRate,
    ImportRun,
    Industry,
    JobCategory,
    Language,
    Location,
    Provenance,
    Skill,
    SkillAlias,
    SkillCategory,
    SkillExternalId,
    SkillRelation,
    SkillTranslation,
    UnresolvedTerm,
)
from .services import skills as skill_service
from .services import terms as term_service


class SkillAliasInline(admin.TabularInline):
    model = SkillAlias
    extra = 1
    # `normalised` is derived in SkillAlias.save(); typing it by hand produces
    # aliases that resolve to nothing at all.
    fields = ("alias", "locale")


class SkillTranslationInline(admin.TabularInline):
    model = SkillTranslation
    extra = 0
    fields = ("locale", "name", "description")


class SkillExternalIdInline(admin.TabularInline):
    model = SkillExternalId
    extra = 0
    fields = ("source", "external_id", "uri")


class SkillRelationInline(admin.TabularInline):
    model = SkillRelation
    fk_name = "from_skill"
    extra = 0
    fields = ("kind", "to_skill", "weight")
    autocomplete_fields = ("to_skill",)
    verbose_name_plural = "relations from this skill"


class MergeForm(forms.Form):
    """Which of the selected skills survives."""

    target = forms.ModelChoiceField(queryset=Skill.objects.none(), label="Keep this skill")

    def __init__(self, *args, queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target"].queryset = queryset if queryset is not None else Skill.objects.none()


class DeprecateForm(forms.Form):
    reason = forms.CharField(
        max_length=255,
        label="Why is this skill being retired?",
        help_text="Recorded on the skill and in the audit log.",
    )
    replacement_slug = forms.CharField(
        required=False,
        label="Replacement skill slug",
        help_text="Optional. Resolution will forward to this skill; leave blank if there is none.",
    )

    def clean_replacement_slug(self):
        slug = self.cleaned_data["replacement_slug"].strip()
        if not slug:
            return ""
        if not Skill.objects.filter(slug=slug).exists():
            raise forms.ValidationError(f"No skill with slug {slug!r}.")
        return slug


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "kind", "category", "status", "replaced_by", "usage_count")
    list_filter = ("status", "kind", "category")
    search_fields = ("name", "slug", "aliases__alias")
    inlines = [
        SkillAliasInline,
        SkillTranslationInline,
        SkillExternalIdInline,
        SkillRelationInline,
    ]
    autocomplete_fields = ("category",)
    readonly_fields = (
        "usage_count",
        "status",
        "replaced_by",
        "retired_reason",
        "retired_at",
        "relationship_map",
    )
    actions = ["merge_skills", "deprecate_skills", "reinstate_skills"]

    # --- lifecycle is service-owned ---------------------------------------

    def has_delete_permission(self, request, obj=None) -> bool:
        """Refuse deletion of anything another row depends on.

        Deleting a referenced skill cascades its aliases, translations,
        external ids and relations away, and (from P3) orphans every profile
        that claimed it. Deprecating keeps the history and forwards resolution,
        which is what "this skill should go away" almost always means.
        """
        if obj is None:
            return super().has_delete_permission(request, obj)
        return not _references(obj) and super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        # Bulk delete cannot show *why* a particular row is protected, so it is
        # removed entirely. Deletion stays available one row at a time, and only
        # for rows nothing references.
        actions.pop("delete_selected", None)
        return actions

    @admin.display(description="Relationships")
    def relationship_map(self, obj: Skill) -> str:
        """Read-only view of what this skill implies, sits under and relates to."""
        if obj.pk is None:
            return "—"

        sections = [
            ("Implies", _named(skill_service.implications(obj))),
            ("Broader", _named(skill_service.ancestors(obj))),
            ("Narrower", _named(skill_service.descendants(obj))),
            ("Related", [skill.name for skill in skill_service.related(obj)]),
        ]
        rows = [(label, ", ".join(values) or "—") for label, values in sections]
        return format_html_join("", "<p><strong>{}:</strong> {}</p>", rows)

    # --- curation actions --------------------------------------------------

    @admin.action(description="Merge duplicates into one skill")
    def merge_skills(self, request, queryset):
        if queryset.count() < 2:
            self.message_user(
                request, "Select the duplicates and the skill to keep.", messages.WARNING
            )
            return None

        form = MergeForm(request.POST if "confirmed" in request.POST else None, queryset=queryset)
        if form.is_valid():
            target = form.cleaned_data["target"]
            merged = 0
            for source in queryset.exclude(pk=target.pk):
                try:
                    skill_service.merge(source=source, target=target, actor=request.user)
                    merged += 1
                except DomainError as exc:
                    self.message_user(request, f"{source.name}: {exc}", messages.ERROR)
            self.message_user(request, f"Merged {merged} skill(s) into {target.name}.")
            return None

        return self._confirm(
            request,
            queryset,
            form=form,
            action="merge_skills",
            title="Merge skills",
            intro=(
                "The skills below become one. Aliases, translations, external ids and "
                "relations move to the survivor; the duplicates stay as merged rows so "
                "anything already pointing at them keeps resolving."
            ),
            submit_label="Merge",
        )

    @admin.action(description="Deprecate (retire) selected skills")
    def deprecate_skills(self, request, queryset):
        form = DeprecateForm(request.POST if "confirmed" in request.POST else None)
        if form.is_valid():
            replacement = None
            slug = form.cleaned_data["replacement_slug"]
            if slug:
                replacement = Skill.objects.get(slug=slug)

            retired = 0
            for skill in queryset:
                try:
                    skill_service.deprecate(
                        skill=skill,
                        reason=form.cleaned_data["reason"],
                        replacement=replacement,
                        actor=request.user,
                    )
                    retired += 1
                except DomainError as exc:
                    self.message_user(request, f"{skill.name}: {exc}", messages.ERROR)
            self.message_user(request, f"Deprecated {retired} skill(s).")
            return None

        return self._confirm(
            request,
            queryset,
            form=form,
            action="deprecate_skills",
            title="Deprecate skills",
            intro=(
                "Retired skills stop being offered for new records. Existing records "
                "keep them, and resolution forwards to the replacement if you name one."
            ),
            submit_label="Deprecate",
        )

    @admin.action(description="Reinstate deprecated skills")
    def reinstate_skills(self, request, queryset):
        restored = 0
        for skill in queryset:
            try:
                skill_service.reinstate(skill=skill, actor=request.user)
                restored += 1
            except DomainError as exc:
                self.message_user(request, f"{skill.name}: {exc}", messages.ERROR)
        self.message_user(request, f"Reinstated {restored} skill(s).")

    def _confirm(self, request, queryset, *, form, action, title, intro, submit_label):
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "intro": intro,
            "form": form,
            "queryset": queryset,
            "action": action,
            "submit_label": submit_label,
            "opts": self.model._meta,
        }
        return TemplateResponse(request, "admin/taxonomy/curation_action.html", context)


def _references(skill: Skill) -> bool:
    """Is anything depending on this skill?"""
    return (
        skill.aliases.exists()
        or skill.translations.exists()
        or skill.external_ids.exists()
        or skill.relations_out.exists()
        or skill.relations_in.exists()
        or skill.replaces.exists()
        or skill.usage_count > 0
    )


def _named(credit: Mapping[int, object]) -> list[str]:
    names = Skill.objects.filter(pk__in=credit).values_list("name", flat=True)
    return sorted(names)


class ResolveTermForm(forms.Form):
    skill = forms.ModelChoiceField(queryset=Skill.objects.none(), label="This term means")
    add_alias = forms.BooleanField(
        required=False,
        initial=True,
        label="Also teach resolution this spelling",
        help_text=(
            "Leave ticked. Resolving without adding the alias means the next person "
            "who types it hits the same wall and the term returns to this queue."
        ),
    )
    note = forms.CharField(required=False, max_length=200, label="Note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["skill"].queryset = Skill.objects.active()


@admin.register(UnresolvedTerm)
class UnresolvedTermAdmin(admin.ModelAdmin):
    """The curation queue for skills the taxonomy is missing (ADR-A46).

    Ordered by demand, because the useful question is not "what did we not
    recognise" — it is "what did we fail to recognise most often".
    """

    list_display = (
        "term",
        "locale",
        "occurrences",
        "status",
        "source",
        "resolved_skill",
        "last_seen",
    )
    list_filter = ("status", "source", "locale")
    search_fields = ("term", "normalised")
    readonly_fields = (
        "term",
        "normalised",
        "locale",
        "occurrences",
        "source",
        "source_counts",
        "confidence",
        "first_seen",
        "last_seen",
        "resolved_by",
        "resolved_at",
    )
    actions = ["resolve_terms", "reject_terms", "reopen_terms"]

    def has_add_permission(self, request) -> bool:
        # Rows are written by whatever failed to resolve. A hand-made one would
        # be a gap nobody actually hit.
        return False

    @admin.action(description="Resolve to an existing skill")
    def resolve_terms(self, request, queryset):
        form = ResolveTermForm(request.POST if "confirmed" in request.POST else None)
        if form.is_valid():
            resolved = 0
            for term in queryset:
                try:
                    term_service.resolve(
                        term=term,
                        skill=form.cleaned_data["skill"],
                        actor=request.user,
                        add_alias=form.cleaned_data["add_alias"],
                        note=form.cleaned_data["note"],
                    )
                    resolved += 1
                except DomainError as exc:
                    self.message_user(request, f"{term.term}: {exc}", messages.ERROR)
            self.message_user(request, f"Resolved {resolved} term(s).")
            return None

        return _confirm(
            self,
            request,
            queryset,
            form=form,
            action="resolve_terms",
            title="Resolve unknown terms",
            intro=(
                "Map these terms onto a skill that already exists. If the term is a "
                "genuinely new skill, create the skill first, then resolve to it."
            ),
            submit_label="Resolve",
        )

    @admin.action(description="Reject — not a skill")
    def reject_terms(self, request, queryset):
        rejected = 0
        for term in queryset:
            try:
                term_service.reject(term=term, actor=request.user)
                rejected += 1
            except DomainError as exc:
                self.message_user(request, f"{term.term}: {exc}", messages.ERROR)
        self.message_user(request, f"Rejected {rejected} term(s).")

    @admin.action(description="Reopen — put back in the queue")
    def reopen_terms(self, request, queryset):
        reopened = 0
        for term in queryset:
            try:
                term_service.reopen(term=term, actor=request.user)
                reopened += 1
            except DomainError as exc:
                self.message_user(request, f"{term.term}: {exc}", messages.ERROR)
        self.message_user(request, f"Reopened {reopened} term(s).")


def _confirm(model_admin, request, queryset, *, form, action, title, intro, submit_label):
    """The shared confirmation step for actions that need a form."""
    context = {
        **model_admin.admin_site.each_context(request),
        "title": title,
        "intro": intro,
        "form": form,
        "queryset": queryset,
        "action": action,
        "submit_label": submit_label,
        "opts": model_admin.model._meta,
    }
    return TemplateResponse(request, "admin/taxonomy/curation_action.html", context)


@admin.register(SkillCategory)
class SkillCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "parent")
    search_fields = ("name", "slug")


@admin.register(SkillAlias)
class SkillAliasAdmin(admin.ModelAdmin):
    list_display = ("alias", "skill", "locale", "normalised", "source", "batch")
    list_filter = ("locale", "source")
    search_fields = ("alias", "normalised", "skill__name")
    autocomplete_fields = ("skill",)
    readonly_fields = ("normalised", "created_by")
    fields = ("skill", "alias", "locale", "normalised", "source", "created_reason", "created_by")

    def save_model(self, request, obj, form, change):
        # An alias typed in admin is curated by definition, and by the person
        # typing it. Making them state both would be a form nobody fills in
        # honestly (ADR-A47).
        if not change:
            obj.created_by = request.user
            obj.source = obj.source or Provenance.CURATED
        super().save_model(request, obj, form, change)


@admin.register(SkillRelation)
class SkillRelationAdmin(admin.ModelAdmin):
    list_display = ("from_skill", "kind", "to_skill", "weight", "source", "confidence", "batch")
    list_filter = ("kind", "source")
    autocomplete_fields = ("from_skill", "to_skill")
    search_fields = ("from_skill__name", "to_skill__name")


@admin.register(SkillExternalId)
class SkillExternalIdAdmin(admin.ModelAdmin):
    list_display = ("source", "external_id", "skill")
    list_filter = ("source",)
    search_fields = ("external_id", "skill__name")
    autocomplete_fields = ("skill",)


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = (
        "source",
        "state",
        "phase",
        "processed",
        "created",
        "updated",
        "linked",
        "created_at",
    )
    list_filter = ("source", "state")
    readonly_fields = tuple(
        field.name for field in ImportRun._meta.fields if field.name != "id"
    ) + ("resume_hint",)

    @admin.display(description="Resuming")
    def resume_hint(self, obj: ImportRun) -> str:
        if obj.state == ImportRun.State.COMPLETED:
            return "Finished."
        return format_html(
            "Re-run the same import command; it continues in the <em>{}</em> phase after "
            "<code>{}</code>.",
            obj.get_phase_display(),
            obj.last_key or "the beginning",
        )

    def has_add_permission(self, request) -> bool:
        # Runs are created by the importer. A hand-made row would look like a
        # checkpoint and silently skip records.
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(JobCategory)
class JobCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "parent", "onet_code")
    search_fields = ("name", "slug")


@admin.register(Industry)
class IndustryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name", "slug")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("city", "region", "country", "timezone")
    list_filter = ("country",)
    search_fields = ("city", "region")


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ("name", "code")
    search_fields = ("name", "code")


@admin.register(FxRate)
class FxRateAdmin(admin.ModelAdmin):
    list_display = ("base", "quote", "rate", "as_of", "source")
    list_filter = ("base", "quote", "source")
    date_hierarchy = "as_of"

    def has_change_permission(self, request, obj=None) -> bool:
        # Append-only: editing a rate in place rewrites every conversion that
        # used it. Supersede with a newer as_of instead.
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
