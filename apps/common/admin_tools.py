"""
Admin building blocks for records staff may look at but not change.

Most of the platform's tables are records: an outbox event, a consent, a
message, a delivery attempt. The admin is where an operator looks one up, and
editing one in a form would make it a record nobody can trust. So the default
here is **view only**, and two more rules sit on top:

  * **Private content is never rendered.** A model lists its `private_fields`
    (a message body, a cover letter, a webhook secret, a session key, a token
    hash) and they are left out of every admin page: list, detail, search and
    the page title. Staff who need to read a candidate's own words do it in
    the staff portal, which checks a `StaffRole` and writes an access log
    (decision D-01). The Django admin does neither, so it shows neither.
  * **The object's name is not its content.** Several models' `__str__` is
    the first sixty characters of their body. The admin uses `__str__` for
    page titles, breadcrumbs and the history log, so a private model is named
    by its type and id instead (`safe_name`).

`is_staff` alone opens none of this. A model page needs the model's `view`
permission (or a superuser), exactly as for every other admin page.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest


class _SafeName:
    """Stands in for an object wherever the admin would print `str(obj)`."""

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def __str__(self) -> str:
        return f"{self._obj._meta.verbose_name} #{self._obj.pk}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._obj, name)


class RecordAdmin(admin.ModelAdmin):
    """View-only, with private fields left out entirely."""

    #: Never rendered, never searchable.
    private_fields: tuple[str, ...] = ()
    #: Name objects by type and id rather than `__str__` (which may quote content).
    safe_name: bool = False
    #: Fields too large or too internal to be worth a row on the page.
    skip_fields: tuple[str, ...] = ("search_vector", "embedding")

    def get_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        hidden = set(self.private_fields) | set(self.skip_fields)
        return [
            field.name
            for field in self.model._meta.concrete_fields
            if field.name not in hidden and field.name != "id"
        ]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        return self.get_fields(request, obj)

    def get_search_fields(self, request: HttpRequest) -> tuple[str, ...]:
        fields = tuple(super().get_search_fields(request))
        leaked = [f for f in fields if f.split("__")[0] in self.private_fields]
        if leaked:  # pragma: no cover - a configuration mistake, caught in tests
            raise ValueError(f"{self.model.__name__} searches private fields {leaked}")
        return fields

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def change_view(
        self, request: HttpRequest, object_id: str, form_url: str = "", extra_context: Any = None
    ) -> Any:
        return self._renamed(
            super().change_view(request, object_id, form_url, extra_context), "original"
        )

    def history_view(self, request: HttpRequest, object_id: str, extra_context: Any = None) -> Any:
        return self._renamed(super().history_view(request, object_id, extra_context), "object")

    def _renamed(self, response: Any, key: str) -> Any:
        """Swap the object for its safe name before the template sees it."""
        context = getattr(response, "context_data", None)
        if not self.safe_name or not context or context.get(key) is None:
            return response
        obj = context[key]
        context[key] = _SafeName(obj)
        context["title"] = f"{self.model._meta.verbose_name.capitalize()} #{obj.pk}"
        context["subtitle"] = None
        return response


def safe_label(obj: Any) -> str:
    """A related object's name, without its content."""
    return f"{obj._meta.verbose_name} #{obj.pk}" if obj is not None else "-"
