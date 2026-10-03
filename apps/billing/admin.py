"""
Staff billing tooling (SRS §3.12, §2.4).

Plans and coupons are editable here — they are content, and a price change
should not need a deploy. Subscriptions, invoices and payments are **read
only**: they are the record of money, and a record of money that can be edited
in a form is not a record. The operations staff actually need — assign a plan,
record a bank transfer — are actions that go through the services, so they are
audited and emit their events like every other write.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.http import HttpRequest

from .models import Coupon, Invoice, Payment, PaymentAttempt, Plan, Subscription, UsageCounter
from .services import checkout as checkout_service
from .services import invoices as invoice_service
from .services import payments as payment_service
from .services import subscriptions as subscription_service


class ReadOnlyAdmin(admin.ModelAdmin):
    """Visible, searchable, unwritable."""

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "audience", "price", "currency", "interval", "is_default")
    list_filter = ("audience", "interval", "is_active", "is_default")
    search_fields = ("code", "name")
    ordering = ("audience", "order")


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ("code", "percent_off", "amount_off", "redemptions", "expires_on", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code",)


@admin.register(Subscription)
class SubscriptionAdmin(ReadOnlyAdmin):
    list_display = ("__str__", "plan", "state", "current_period_end", "cancel_at", "provider")
    list_filter = ("state", "plan", "provider")
    search_fields = ("provider_ref", "company__name")
    date_hierarchy = "created_at"
    actions = ["cancel_immediately"]

    @admin.action(description="Cancel immediately (corrects a mis-assigned plan)")
    def cancel_immediately(self, request: HttpRequest, queryset: Any) -> None:
        ended = 0
        for subscription in queryset:
            if subscription.is_live:
                subscription_service.cancel(
                    subscription=subscription, actor=request.user, immediately=True
                )
                ended += 1
        messages.success(request, f"Ended {ended} subscription(s).")


@admin.register(Invoice)
class InvoiceAdmin(ReadOnlyAdmin):
    list_display = ("number", "subscription", "state", "total", "currency", "due_on", "paid_at")
    list_filter = ("state", "currency")
    search_fields = ("number",)
    date_hierarchy = "issued_on"
    actions = ["record_bank_transfer", "void_invoice"]

    @admin.action(description="Record a bank transfer for the full amount")
    def record_bank_transfer(self, request: HttpRequest, queryset: Any) -> None:
        recorded = 0
        for invoice in queryset:
            payment_service.record_manual(
                invoice=invoice,
                actor=request.user,
                reference=f"staff:{invoice.number}",
            )
            recorded += 1
        messages.success(request, f"Recorded {recorded} payment(s).")

    @admin.action(description="Void (unpaid invoices only)")
    def void_invoice(self, request: HttpRequest, queryset: Any) -> None:
        for invoice in queryset:
            invoice_service.void(invoice=invoice, actor=request.user, reason="staff action")
        messages.success(request, "Voided.")


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdmin):
    list_display = (
        "provider_ref",
        "provider",
        "invoice",
        "state",
        "amount",
        "currency",
        "received_at",
        "recorded_by",
    )
    list_filter = ("provider", "state", "currency")
    search_fields = ("provider_ref", "invoice__number")
    date_hierarchy = "created_at"
    list_select_related = ("invoice", "recorded_by")


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(ReadOnlyAdmin):
    """Every trip to a gateway (ADR-A58): what was asked, what the gateway said.

    Read only, like every record of money. The one action re-asks the gateway,
    through the same service a customer's "check again" uses.
    """

    list_display = (
        "reference",
        "provider",
        "environment",
        "invoice",
        "amount",
        "currency",
        "state",
        "gateway_status",
        "gateway_ref",
        "created_at",
        "completed_at",
    )
    list_filter = ("provider", "environment", "state", "gateway_status")
    search_fields = ("reference", "gateway_ref", "gateway_token", "invoice__number")
    date_hierarchy = "created_at"
    list_select_related = ("invoice", "payment")
    readonly_fields = (
        "public_id",
        "invoice",
        "provider",
        "environment",
        "reference",
        "gateway_token",
        "gateway_ref",
        "amount",
        "currency",
        "state",
        "gateway_status",
        "failure_reason",
        "checks",
        "last_checked_at",
        "completed_at",
        "payment",
        "initiated_by",
        "detail",
        "subscription",
        "created_at",
        "updated_at",
    )
    actions = ["recheck"]

    @admin.display(description="Subscription")
    def subscription(self, obj: PaymentAttempt) -> str:
        sub = obj.invoice.subscription
        return f"{sub} ({sub.get_state_display()})"

    def changelist_view(self, request: HttpRequest, extra_context: Any = None) -> Any:
        from .gateways import base as gateways

        rows = []
        for key, gateway in gateways.registry().items():
            cfg = gateways.config(key)
            problems = gateway.problems()
            rows.append(
                {
                    "label": gateway.label,
                    "enabled": bool(cfg.get("enabled")),
                    "configured": not problems,
                    "problems": problems,
                }
            )
        extra_context = {
            **(extra_context or {}),
            "payments_environment": gateways.environment(),
            "gateway_rows": rows,
        }
        return super().changelist_view(request, extra_context=extra_context)

    @admin.action(description="Re-check with the payment gateway")
    def recheck(self, request: HttpRequest, queryset: Any) -> None:
        checked = 0
        for attempt in queryset:
            checkout_service.reconcile(attempt)
            checked += 1
        messages.success(request, f"Re-checked {checked} attempt(s).")


@admin.register(UsageCounter)
class UsageCounterAdmin(ReadOnlyAdmin):
    list_display = ("owner_id", "owner_ct", "metric", "period", "period_start", "value")
    list_filter = ("metric", "period")
