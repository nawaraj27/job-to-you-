from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.common import views as health

urlpatterns = [
    # Operational endpoints first: they must answer even when a product URL
    # below them is misconfigured.
    path("healthz", health.healthz, name="healthz"),
    path("readyz", health.readyz, name="readyz"),
    path("admin/", admin.site.urls),
    # The three purpose-built staff surfaces (ADR-A5). Separate from the admin
    # because they are workflow rather than CRUD, and because one URL prefix
    # must not mean two different authorities.
    path("staff/", include("portals.staff.urls")),
    # The only route to a private file. Never MEDIA_URL (SRS §14.2).
    path("files/", include("apps.files.urls")),
    path("billing/", include("apps.billing.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("cv/", include("portals.candidate.resume_urls")),
    # Candidate job browse and search on real postings (P7). Namespace unchanged.
    path("jobs/", include("portals.candidate.job_urls")),
    # The candidate's pipeline, on real applications (P8).
    path("applications/", include("portals.candidate.application_urls")),
    path("letters/", include("portals.candidate.letter_urls")),
    # Messaging (P9). The candidate half; the employer half is company-scoped.
    path("messages/", include("portals.candidate.message_urls")),
    path("interviews/", include("portals.candidate.interview_urls")),
    # Candidate screens on real records (P3). Namespaces unchanged.
    path("dashboard/", include("portals.candidate.urls")),
    # Real, company-scoped employer portal (ADR-A18).
    path("employer/", include("portals.employer.urls")),
    path("settings/", include("portals.candidate.settings_urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("", include("apps.core.urls")),
]

if settings.DEBUG:
    # Public media only. MEDIA_ROOT is var/public; private attachments live in
    # PRIVATE_STORAGE_ROOT, outside it, and are unreachable from here.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
