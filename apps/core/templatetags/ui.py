"""Small template helpers that keep presentation logic out of templates."""

from django import template
from django.http import QueryDict

register = template.Library()


@register.filter
def widget_type(field) -> str:
    """Lowercased widget class name, e.g. 'textinput', 'textarea', 'radioselect'.

    Django templates cannot do isinstance checks, and `input_type` is missing on
    Textarea/Select-style widgets — this gives form_field.html one reliable switch.
    """
    try:
        return field.field.widget.__class__.__name__.lower()
    except AttributeError:
        return ""


@register.simple_tag(takes_context=True)
def query_replace(context, **changes) -> str:
    """The current query string with some parameters replaced.

        <a href="{% query_replace page=3 %}">

    Pagination links were written as `?page={{ n }}`, which discards every
    other parameter — so paging through a filtered search silently cleared the
    search and the filters, and the results changed for a reason the user could
    not see. This keeps everything else and swaps only what is named. Passing
    None drops a key.
    """
    request = context.get("request")
    params = request.GET.copy() if request else QueryDict(mutable=True)
    for key, value in changes.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"


@register.filter
def control_class(field) -> str:
    """The component class a widget should wear: input / select / textarea.

    Lets form_field.html apply the ARIA wiring once instead of once per widget
    type — the previous version branched on widget first, which meant the
    describedby/invalid attributes had to be written out three times and only
    ever got written on one of them.
    """
    kind = widget_type(field)
    if kind == "select":
        return "select"
    if kind == "textarea":
        return "textarea"
    return "input"


@register.filter
def in_list(value, collection) -> bool:
    """`{{ job.slug|in_list:saved_slugs }}` — membership as a filter.

    Django's `{% if x in y %}` works, but not inside `{% with %}` where the
    result needs to be bound to a name and reused three times in one component.
    """
    try:
        return value in collection
    except TypeError:
        return False


@register.filter
def times(count):
    """`{% for i in 6|times %}` — repeat a block N times.

    Django has no loop-N-times tag, and the usual workaround (`""|ljust:6`)
    reads as a typo. Used by the skeleton component, where the repeat count is
    the number of placeholder rows to draw.
    """
    try:
        return range(int(count))
    except (TypeError, ValueError):
        return range(0)


@register.filter
def lookup(mapping, key):
    """`{{ counts|lookup:link.url_name }}` — dictionary access by a variable key.

    Django templates can only do `dict.literal_key`, which is useless when the
    key comes from the loop. Returns None (renders as nothing) when absent.
    """
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def event_label(value: str) -> str:
    """An event type as something a person would read.

    `application.stage_changed` is an identifier the system uses to route a
    notification. It was rendered verbatim on the candidate's own feed, both as
    a filter chip and beside every timestamp — a machine key in a list of
    sentences.

    Derived rather than tabulated: a mapping would need a line per event and a
    new one every time somebody registers an event, and the line they forgot is
    the one that shows the key again.
    """
    words = str(value or "").replace(".", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else ""


@register.filter
def split(value: str, separator: str = ","):
    """Split a literal string into a list. Handy for small demo lists.

    Never use the result to build CSS class names — Tailwind scans source text
    and will not generate CSS for a class assembled at render time.
    """
    return [part.strip() for part in str(value).split(separator) if part.strip()]


@register.simple_tag
def asset(path: str) -> str:
    """`{% asset 'js/app.js' %}` — a static URL that changes when the file does.

    In development the browser held onto previous builds of the stylesheet and
    of app.js after a rebuild, often enough that an edit appeared to do nothing
    and the next half hour went into debugging code that was already correct.
    Each asset carries its own mtime, so a changed file cannot be served from
    cache and an unchanged one still is.

    In production the storage backend hashes filenames already, so the suffix
    is omitted and the URL is left exactly as `{% static %}` produced it.
    """
    from pathlib import Path

    from django.conf import settings as django_settings
    from django.templatetags.static import static as static_url

    url = static_url(path)
    if not django_settings.DEBUG:
        return url

    source = Path(django_settings.BASE_DIR) / "static" / path
    try:
        return f"{url}?v={int(source.stat().st_mtime)}"
    except OSError:
        return url


@register.filter
def money(amount, currency="NPR"):
    """`{{ invoice.total|money:invoice.currency }}` -> "NPR 1,700"."""
    from apps.billing.money import format_money

    return format_money(amount, currency)
