"""
Health endpoints (SRS §17.4).

Two endpoints with different jobs, because conflating them causes outages:

  /healthz  liveness  — is this process alive? No dependencies touched, so a
                        database blip cannot make an orchestrator kill every
                        healthy web container at once.
  /readyz   readiness — can this process serve traffic? Checks the database,
                        the cache and private storage, and is what a load
                        balancer should use to decide whether to send requests.

The probes themselves live in `apps.common.health`, because the staff
operations board reports the same three dependencies to a person and the two
surfaces must not be able to disagree.

`common` is a platform app rather than a domain app, so it may own these views
(SRS §6.1 forbids views in *domain* apps). They are infrastructure, not product.
"""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse

from . import health


def healthz(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def readyz(request: HttpRequest) -> JsonResponse:
    checks = {probe.key: status for probe, status in health.check_all()}

    ready = all(status == health.OK for status in checks.values())
    return JsonResponse(
        {"status": "ok" if ready else "degraded", "checks": checks},
        status=200 if ready else 503,
    )
