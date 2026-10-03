from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("pricing/", views.PricingView.as_view(), name="pricing"),
    path("about/", views.AboutView.as_view(), name="about"),
    path("contact/", views.ContactView.as_view(), name="contact"),
    path("privacy/", views.PrivacyView.as_view(), name="privacy"),
    path("terms/", views.TermsView.as_view(), name="terms"),
    path("for-employers/", views.EmployerSolutionsView.as_view(), name="employer_solutions"),
    path("design-system/", views.DesignSystemView.as_view(), name="design_system"),
    # Demo advertisements (`core.ads`). "hide" before the key, so it is never
    # read as one.
    path("sponsored/hide/", views.DemoAdHideView.as_view(), name="demo_ad_hide"),
    path("sponsored/<slug:key>/", views.DemoAdView.as_view(), name="demo_ad"),
]
