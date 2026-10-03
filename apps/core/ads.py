"""
Demo advertisements: how a sponsored slot would sit in the product.

**Demonstration only.** Every advertiser here is fictional and says so; there
is no ad network, no tracking pixel, no profiling, no targeting and no
advertising cookie. What an ad knows about the viewer is nothing: the slot it
appears in is chosen by the page, and the only state is the list of demo ads a
visitor has hidden, kept in their session.

The rules the component keeps, because an ad that damages the product costs
more than it earns:

  * **Few, fixed places** (`SLOTS`): job browsing, the candidate dashboard's
    side column, and the bottom of the CV analysis — never inside a task. Not
    in an interview, the CV editor or builder, an upload, sign-in, billing or
    an employer's job form.
  * **Server-rendered in reserved space**, so nothing moves after the page
    loads; no pop-up, no overlay, no autoplay, no sound, no animation.
  * **Always labelled** "Advertisement · Demo", and "Learn more" goes to a page
    on this site that says the ad is a demonstration — never to a third party.

Replacing the demo data with a real sponsorship source later means replacing
`DEMO_ADS` and `for_slot`; the templates only ever see a `DemoAd`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.urls import reverse

from apps.core.branding import site_name

#: The places a page may ask for an ad. A slot not listed here never shows one.
SLOTS = frozenset({"jobs", "dashboard", "analysis"})

#: Where a visitor's hidden demo ads are remembered. Session only.
DISMISSED_KEY = "demo_ads_hidden"


@dataclass(frozen=True)
class DemoAd:
    key: str
    advertiser: str
    title: str
    text: str
    cta: str
    #: Two letters drawn as the logo: no third-party artwork is used.
    monogram: str
    slots: frozenset[str] = field(default_factory=frozenset)
    active: bool = True
    label: str = "Advertisement · Demo"

    @property
    def url(self) -> str:
        return reverse("core:demo_ad", args=[self.key])


DEMO_ADS: tuple[DemoAd, ...] = (
    DemoAd(
        key="example-learning",
        advertiser="Example Learning Platform",
        title="Short evening courses in data and design",
        text=(
            "A fictional course provider, shown to demonstrate a sponsored slot. "
            "Nothing is on sale and no partner is involved."
        ),
        cta="Learn more",
        monogram="EL",
        slots=frozenset({"jobs", "dashboard"}),
    ),
    DemoAd(
        key="sample-certification",
        advertiser="Sample Certification Program",
        title="A certificate for the skills you already use",
        text=(
            f"Sample content for a career-services advert. {site_name()} has no "
            "commercial relationship with any certification body."
        ),
        cta="Learn more",
        monogram="SC",
        slots=frozenset({"analysis", "dashboard"}),
    ),
)


def by_key(key: str) -> DemoAd | None:
    return next((ad for ad in DEMO_ADS if ad.key == key and ad.active), None)


def hidden(request: Any) -> list[str]:
    session = getattr(request, "session", None)
    return list(session.get(DISMISSED_KEY, [])) if session is not None else []


def for_slot(request: Any, slot: str) -> DemoAd | None:
    """The demo ad for this slot, or None.

    None when the slot is not one of `SLOTS`, the `demo_ads` feature is off,
    or the visitor has hidden every ad that fits it.
    """
    if slot not in SLOTS:
        return None
    from .features import is_enabled

    if not is_enabled("demo_ads"):
        return None
    gone = set(hidden(request))
    return next(
        (ad for ad in DEMO_ADS if ad.active and slot in ad.slots and ad.key not in gone), None
    )


def hide(request: Any, key: str) -> bool:
    """Remember that this visitor hid this demo ad. Returns whether it exists."""
    if by_key(key) is None:
        return False
    gone = hidden(request)
    if key not in gone:
        request.session[DISMISSED_KEY] = [*gone, key][: len(DEMO_ADS)]
    return True
