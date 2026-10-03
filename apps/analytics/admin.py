"""
Analytics in Django admin: read-only throughout.

Every table here is either a recorded fact or a computed number. Editing a
rollup by hand would produce a dashboard that disagrees with the data it claims
to summarise, and the fix for a wrong rollup is `backfill_rollups`, which
recomputes it from the source.

Raw event props are not searchable and not on any changelist. They carry only
counts and identifiers by construction (`capture._clean`), and a staff screen
that invited searching them would be a screen people use to look up individuals.
"""

from __future__ import annotations

from django.contrib import admin

from .models import (
    AICostDaily,
    AnalyticsEvent,
    CompanyDailyStat,
    FunnelSnapshot,
    JobDailyStat,
    SteeringSnapshot,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(AnalyticsEvent)
class AnalyticsEventAdmin(ReadOnlyAdmin):
    list_display = ("name", "company", "created_at")
    list_filter = ("name",)
    readonly_fields = tuple(field.name for field in AnalyticsEvent._meta.fields)
    raw_id_fields = ("actor", "company")

    def has_delete_permission(self, request, obj=None) -> bool:
        # Pruning goes through `prune_events`, which is bounded. A staff member
        # deleting a year of rows from a changelist takes a lock nobody planned.
        return False


@admin.register(JobDailyStat)
class JobDailyStatAdmin(ReadOnlyAdmin):
    list_display = ("job", "day", "views", "applications", "shortlisted")
    list_filter = ("day",)
    readonly_fields = tuple(field.name for field in JobDailyStat._meta.fields)
    raw_id_fields = ("job",)


@admin.register(CompanyDailyStat)
class CompanyDailyStatAdmin(ReadOnlyAdmin):
    list_display = ("company", "day", "applications", "responded", "interviews", "hires")
    list_filter = ("day",)
    readonly_fields = tuple(field.name for field in CompanyDailyStat._meta.fields)
    raw_id_fields = ("company",)


@admin.register(FunnelSnapshot)
class FunnelSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("company", "day", "stage_key", "count", "entered", "exited")
    list_filter = ("day", "stage_key")
    readonly_fields = tuple(field.name for field in FunnelSnapshot._meta.fields)
    raw_id_fields = ("company",)


@admin.register(AICostDaily)
class AICostDailyAdmin(ReadOnlyAdmin):
    list_display = ("day", "task_key", "runs", "failures", "cached", "cost")
    list_filter = ("day", "task_key")
    readonly_fields = tuple(field.name for field in AICostDaily._meta.fields)


@admin.register(SteeringSnapshot)
class SteeringSnapshotAdmin(ReadOnlyAdmin):
    """The four measurements (§16.3).

    `is_meaningful` is in the list because a value read without its sample size
    is how somebody acts on eleven applications.
    """

    list_display = ("day", "metric", "value", "sample_size", "meaningful")
    list_filter = ("metric",)
    readonly_fields = tuple(field.name for field in SteeringSnapshot._meta.fields)

    @admin.display(boolean=True, description="Enough data")
    def meaningful(self, obj: SteeringSnapshot) -> bool:
        return obj.is_meaningful
