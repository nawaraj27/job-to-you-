"""`{% demo_ad "jobs" %}` — the demo ad for one slot, or nothing (`core.ads`)."""

from __future__ import annotations

from django import template

from apps.core import ads

register = template.Library()


@register.inclusion_tag("components/demo_ad.html", takes_context=True)
def demo_ad(context, slot: str, variant: str = "card") -> dict:
    request = context.get("request")
    return {
        "ad": ads.for_slot(request, slot) if request is not None else None,
        "slot": slot,
        "variant": variant,
        "request": request,
        "csrf_token": context.get("csrf_token"),
    }


@register.simple_tag(takes_context=True)
def demo_ad_for(context, slot: str):
    """The `DemoAd` for a slot, for a page that wraps it: `{% demo_ad_for "jobs" as ad %}`."""
    request = context.get("request")
    return ads.for_slot(request, slot) if request is not None else None
