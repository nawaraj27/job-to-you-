from django.conf import settings
from django.http import Http404
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from . import content, legal


class HomeView(TemplateView):
    template_name = "pages/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "hero": content.hero(),
                "hero_preview": content.hero_preview(),
                "photos": content.photos(),
                "story": content.story(),
                "journey": content.journey(),
                "capabilities": content.capabilities(),
                "proof_points": content.proof_points(),
                "audiences": content.audiences(),
            }
        )
        return context


class StaticPageView(TemplateView):
    """Simple content pages that share the marketing layout."""

    page_title = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.page_title
        return context


class PricingView(StaticPageView):
    template_name = "pages/pricing.html"
    page_title = "Pricing"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["plans"] = content.pricing_plans(self.request.user)
        context["faq"] = content.pricing_faq()
        return context


class AboutView(StaticPageView):
    template_name = "pages/about.html"
    page_title = "About"


class ContactView(StaticPageView):
    template_name = "pages/contact.html"
    page_title = "Contact"


class LegalPageView(StaticPageView):
    """A legal page, rendered from the version in force (SRS §5.14).

    The text is a row rather than this template's contents, because
    `Consent.policy_version` has to be reproducible: recording that somebody
    agreed to 0.1-draft is only useful if 0.1-draft can still be read.

    A missing document renders an honest empty state rather than a 500 — a
    fresh environment has not run `seed_legal` yet, and a legal page that
    crashes is worse than one that says it is not published.
    """

    template_name = "pages/legal.html"
    slug = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        document = legal.current(self.slug)
        context["document"] = document
        context["page_title"] = document.title if document else self.page_title
        return context


class PrivacyView(LegalPageView):
    slug = "privacy"
    page_title = "Privacy"


class TermsView(LegalPageView):
    slug = "terms"
    page_title = "Terms"


class EmployerSolutionsView(TemplateView):
    """`/for-employers/` — what the platform does for a company that wants to hire.

    A marketing page, moved here from the mock employer portal when P8 deleted
    it. It always belonged with the other public pages; it lived in
    `apps/employers` only because that app happened to exist.
    """

    template_name = "pages/employer_solutions.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "For employers"
        return context


class DesignSystemView(TemplateView):
    """Internal component gallery. Never exposed outside development."""

    template_name = "pages/design_system.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.DEBUG:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from .templatetags.icons import PATHS

        context = super().get_context_data(**kwargs)
        context.update(
            {
                "page_title": "Design system",
                "icon_names": sorted(PATHS),
                "sample_pass": {"label": "Parsed cleanly by ATS", "status": "pass"},
                "sample_warn": {
                    "label": "Achievements are measurable",
                    "status": "warn",
                    "note": "6 of 11 bullets have no numbers",
                },
                "sample_fail": {
                    "label": "Consistent date formatting",
                    "status": "fail",
                    "note": "3 formats used across roles",
                },
            }
        )
        return context


class DemoAdView(StaticPageView):
    """`/sponsored/<key>/` — where a demo ad's "Learn more" goes.

    Never a third party. The page says plainly that the ad was a demonstration
    of a sponsored slot and that no advertiser or partner is involved.
    """

    template_name = "pages/demo_ad.html"
    page_title = "Demo advertisement"

    def get_context_data(self, **kwargs):
        from django.http import Http404

        from . import ads

        context = super().get_context_data(**kwargs)
        ad = ads.by_key(kwargs.get("key", ""))
        if ad is None:
            raise Http404
        context["ad"] = ad
        return context


class DemoAdHideView(View):
    """`/sponsored/hide/` — hide one demo ad for this visitor (session only)."""

    http_method_names = ["post"]

    def post(self, request):
        from django.http import Http404, HttpResponse

        from apps.common import fragments

        from . import ads

        if not ads.hide(request, request.POST.get("key", "")):
            raise Http404
        page = fragments.page_answer(
            request, message="Demo ad hidden.", fallback_url=reverse("core:home")
        )
        if page is not None:
            return page
        # htmx: the ad's own element is swapped for nothing.
        return HttpResponse("")
