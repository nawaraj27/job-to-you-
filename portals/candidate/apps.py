from django.apps import AppConfig


class CandidatePortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "portals.candidate"
    label = "candidate_portal"
    verbose_name = "Candidate portal"
