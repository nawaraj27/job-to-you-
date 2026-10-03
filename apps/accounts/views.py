"""
Authentication views.

Rate limiting is applied here rather than described elsewhere (ADR-A41). Both
dimensions are enforced on sign-in — per identity and per IP — because identity
alone lets an attacker lock a victim out by hammering their address, and IP
alone is defeated by any botnet.

Registration goes through `accounts.services.identity.register` so that the
primary `EmailAddress`, the audit entry and the `user.registered` event all
happen in one transaction. A view that created the user itself would produce an
account with no verifiable address and no event for onboarding to react to.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.views import LoginView, LogoutView
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from apps.common import logging as log
from apps.common import ratelimit, restrictions
from apps.common.errors import DomainError
from apps.core.branding import site_name

from .forms import LoginForm, SignupForm
from .services import identity

#: Session key for a password that was right and a code that is still owed.
MFA_PENDING = "accounts:mfa_pending"
MFA_PENDING_SECONDS = 300


class SignInView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        identifier = (request.POST.get("username") or "").strip().lower()
        try:
            ratelimit.enforce(
                [
                    (ratelimit.LOGIN_PER_IDENTITY, identifier or "anonymous"),
                    (ratelimit.LOGIN_PER_IP, ratelimit.client_ip(request)),
                ]
            )
        except ratelimit.RateLimited as exc:
            restrictions.flash(request, exc)
            return self.render_to_response(self.get_context_data(form=self.get_form()), status=429)

        return super().post(request, *args, **kwargs)

    def get_success_url(self) -> str:
        """`?next=` if there is one, otherwise this person's own home.

        `LOGIN_REDIRECT_URL` is a single static path and this product has two
        homes. It pointed at the marketing page, so signing in put a candidate
        back in front of the pitch rather than in front of their CV.
        """
        explicit = self.get_redirect_url()
        return explicit or home_for(self.request.user)

    def form_valid(self, form):
        from .services import mfa

        user = form.get_user()
        if mfa.is_enabled(user):
            # The password was right; the second factor is still owed. Nothing
            # is signed in yet: the pending user lives in the session for five
            # minutes, and only a correct code turns it into a login.
            ratelimit.reset(ratelimit.LOGIN_PER_IDENTITY, user.email.strip().lower())
            self.request.session[MFA_PENDING] = {
                "uid": user.pk,
                "next": self.get_redirect_url(),
                "at": timezone.now().timestamp(),
            }
            return redirect("accounts:mfa_challenge")

        response = super().form_valid(form)
        # The password was right, so the attempts that led here were not an
        # attack. Without this a person signing in from a phone, a laptop and a
        # browser they had cleared would spend their five attempts on five
        # successes and be refused the sixth.
        ratelimit.reset(ratelimit.LOGIN_PER_IDENTITY, form.get_user().email.strip().lower())
        self._record_session()
        return response

    def _record_session(self) -> None:
        request = self.request
        if request.session.session_key is None:
            request.session.save()

        session_key = request.session.session_key
        user = request.user
        if session_key is None or not user.is_authenticated:
            return

        identity.record_session(
            user=user,
            session_key=session_key,
            ip=ratelimit.client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
        log.bind(actor_id=request.user.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Sign in"
        # Django's LoginView adds `site` and `site_name` from
        # `get_current_site()`, and with no `django.contrib.sites` installed
        # that is a RequestSite whose name is the bare host. View context beats
        # a context processor, so it shadowed `core.context_processors.site`
        # and the sign-in page — and only the sign-in page — titled itself
        # "Sign in — careerai.app" and gave the logo the aria-label
        # "careerai.app — home". The product has one name and it is not a
        # hostname.
        context.pop("site", None)
        context.pop("site_name", None)
        return context


class SignOutView(LogoutView):
    pass


def home_for(user) -> str:
    """The signed-in home for one person.

    Nobody who has just signed in or just signed up should land on the page
    that sells the product to them — they have bought it. A new candidate went
    to the marketing homepage and had to find the dashboard themselves, and the
    dashboard is the screen that tells them what to do first.

    Membership decides it, not `signup_intent`: a candidate who later joins a
    company is a recruiter now, and the intent is an onboarding hint that was
    never meant to carry authority (SRS §2.1). The intent only breaks the tie
    for somebody who said they are here to hire and has no company yet, whose
    next step is creating one.
    """
    from apps.companies.models import CompanyMembership

    # Platform staff land where their work is. They came in through the same
    # sign-in page and were sent to a candidate dashboard.
    if getattr(user, "staff_role", None) is not None:
        return reverse("staff:index")

    has_company = CompanyMembership.objects.active().filter(user=user).exists()
    if has_company or user.signed_up_to_hire:
        return reverse("employer:index")
    return reverse("dashboard:index")


class SignUpView(View):
    template_name = "accounts/signup.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            # Submitting this while signed in created a second account and
            # silently swapped the session to it. Sign-in already did this.
            return redirect(self._next(request) or home_for(request.user))
        return self._render(request, SignupForm())

    @staticmethod
    def _next(request: HttpRequest) -> str:
        """A same-site `next`, or blank.

        An invitation link sends a signed-out person to sign in; somebody who
        has no account follows "Create an account", and the invitation was
        lost on the way — they landed on a dashboard with no idea how to get
        back to the team they were asked to join. Only local paths survive, so
        this is not an open redirect.
        """
        from django.utils.http import url_has_allowed_host_and_scheme

        target = (request.POST.get("next") or request.GET.get("next") or "").strip()
        if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return target
        return ""

    def post(self, request: HttpRequest) -> HttpResponse:
        # Validate first, then spend the limit. `SIGNUP_PER_IP` exists to stop
        # an address creating accounts in bulk, and a form that failed
        # validation created nothing — so counting it punishes the one person
        # the rule was never aimed at. Five typos used to be an hour's lockout:
        # mistype a password twice, forget the name field once, and a genuine
        # new user was locked out of signing up at all.
        if request.user.is_authenticated:
            messages.info(request, "You are already signed in. Sign out to create another account.")
            return redirect(home_for(request.user))
        form = SignupForm(request.POST)
        if not form.is_valid():
            return self._render(request, form, status=400)

        try:
            ratelimit.enforce([(ratelimit.SIGNUP_PER_IP, ratelimit.client_ip(request))])
        except ratelimit.RateLimited as exc:
            restrictions.flash(request, exc)
            return self._render(request, form, status=429)

        # Decision D-12: the same answer for an address that is new and one that
        # is already registered. Nothing is signed in here — signing a new
        # account in while refusing an existing one would be the difference an
        # enumeration script looks for. The address's owner learns which case it
        # was, by email.
        email = form.cleaned_data["email"]
        next_url = self._next(request)
        if identity.email_is_registered(email):
            self._tell_existing_owner(request, email, form.cleaned_data["password1"], next_url)
        else:
            try:
                with transaction.atomic():
                    user = identity.register(
                        email=email,
                        password=form.cleaned_data["password1"],
                        full_name=form.cleaned_data.get("full_name", ""),
                        signup_intent=form.cleaned_data.get("signup_intent", ""),
                        ip=ratelimit.client_ip(request),
                    )
            except (DomainError, IntegrityError):
                # Another sign-up for the same address won the race.
                self._tell_existing_owner(request, email, "", next_url)
            else:
                self._send_verification(request, user, next_url=next_url)

        sent = reverse("accounts:signup_sent")
        if next_url:
            sent = f"{sent}?{urlencode({'next': next_url})}"
        return redirect(sent)

    def _tell_existing_owner(
        self, request: HttpRequest, email: str, password: str, next_url: str
    ) -> None:
        """Email the owner of an address somebody tried to sign up with again.

        Hashes the submitted password first, as registering would, so the two
        answers also take about the same time. Rate-limited per address and
        silently: a refusal the page reported would be the disclosure again.
        """
        from django.contrib.auth.hashers import make_password

        from apps.notifications import email as email_service

        from .models import User

        make_password(password or None)
        try:
            ratelimit.check(ratelimit.SIGNUP_NOTICE_PER_IDENTITY, email)
        except ratelimit.RateLimited:
            return

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            return

        sign_in = reverse("accounts:login")
        if next_url:
            sign_in = f"{sign_in}?{urlencode({'next': next_url})}"
        lines = [
            f"Somebody tried to create a {site_name()} account with this email address, "
            "and it already has one.",
            "",
            f"If it was you, sign in here:\n{request.build_absolute_uri(sign_in)}",
            "",
            "Forgotten your password? Choose a new one here:\n"
            f"{request.build_absolute_uri(reverse('accounts:password_reset'))}",
        ]
        address = user.email_addresses.filter(is_primary=True).first()
        if address is not None and address.verified_at is None:
            lines += [
                "",
                "Your address is not confirmed yet. Confirm it here:\n"
                f"{self._verification_link(request, address, next_url)}",
            ]
        lines += ["", "If this was not you, ignore this email. Nothing has changed."]
        email_service.send(
            to=user.email,
            subject=f"You already have a {site_name()} account",
            body="\n".join(lines),
        )

    @staticmethod
    def _verification_link(request: HttpRequest, address, next_url: str = "") -> str:
        token = identity.issue_verification_token(address=address)
        link = reverse("accounts:verify_email", args=[token])
        if next_url:
            link = f"{link}?{urlencode({'next': next_url})}"
        return request.build_absolute_uri(link)

    def _send_verification(self, request: HttpRequest, user, *, next_url: str = "") -> None:
        from apps.notifications import email as email_service

        address = user.email_addresses.filter(is_primary=True).first()
        if address is None:
            return

        link = self._verification_link(request, address, next_url)
        email_service.send(
            to=address.email,
            subject="Confirm your email address",
            body=(
                f"Welcome to {site_name()}.\n\n"
                f"Confirm your address to finish setting up your account:\n{link}\n\n"
                "This link works once and expires in three days."
            ),
        )

    def _render(self, request: HttpRequest, form, status: int = 200) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {"form": form, "page_title": "Create your account", "next": self._next(request)},
            status=status,
        )


class SignUpSentView(View):
    """Where every accepted sign-up lands, new address or not (decision D-12)."""

    template_name = "accounts/signup_sent.html"
    SENT = (
        "Check your email. If this address can be used for a new account, the email has a "
        "link to confirm it. If it already has an account, the email explains how to sign "
        "in or reset your password."
    )

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect(home_for(request.user))
        return render(
            request,
            self.template_name,
            {
                "page_title": "Check your email",
                "message": self.SENT,
                "next": _safe_next(request, request.GET.get("next")),
            },
        )


class VerifyEmailView(View):
    """Confirm an address from an emailed, single-use link.

    Confirming does not sign anybody in: a link in an inbox is not a password.
    Somebody signed out goes to sign in, carrying where they were going — an
    invitation, usually — and lands on their own home from there.
    """

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        next_url = _safe_next(request, request.GET.get("next"))
        try:
            address = identity.verify_email(token=token, ip=ratelimit.client_ip(request))
        except DomainError as exc:
            restrictions.flash(request, exc)
            return redirect("core:home")

        messages.success(request, f"{address.email} is confirmed.")
        if request.user.is_authenticated:
            return redirect(next_url or home_for(request.user))
        sign_in = reverse("accounts:login")
        if next_url:
            sign_in = f"{sign_in}?{urlencode({'next': next_url})}"
        return redirect(sign_in)


def _safe_next(request: HttpRequest, target: str | None) -> str:
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return ""


class MfaChallengeView(View):
    """The second step of signing in, and the step-up for a protected page.

    Two callers. Somebody whose password was just accepted arrives with a
    pending user in the session and is not signed in yet. Somebody already
    signed in (before they turned two-factor on, or on a session that was never
    challenged) is sent here by `MfaRequirementMiddleware` before a staff or
    company-admin page opens.
    """

    template_name = "accounts/mfa_challenge.html"

    def _pending_user(self, request: HttpRequest):
        from .models import User

        pending = request.session.get(MFA_PENDING)
        if not pending:
            return None, ""
        age = timezone.now().timestamp() - float(pending.get("at", 0))
        if age > MFA_PENDING_SECONDS:
            request.session.pop(MFA_PENDING, None)
            return None, ""
        user = User.objects.filter(pk=pending.get("uid"), is_active=True).first()
        return user, pending.get("next") or ""

    def _subject(self, request: HttpRequest):
        user, pending_next = self._pending_user(request)
        if user is not None:
            return user, pending_next, True
        if request.user.is_authenticated:
            target = request.GET.get("next") or request.POST.get("next") or ""
            return request.user, target, False
        return None, "", False

    def get(self, request: HttpRequest) -> HttpResponse:
        user, next_url, _pending = self._subject(request)
        if user is None:
            messages.error(request, "Sign in again to continue.")
            return redirect("accounts:login")
        return self._render(request, next_url)

    def post(self, request: HttpRequest) -> HttpResponse:
        from django_otp import login as otp_login

        from .services import mfa

        user, next_url, pending = self._subject(request)
        if user is None:
            messages.error(request, "That took too long. Sign in again.")
            return redirect("accounts:login")

        try:
            ratelimit.enforce(
                [
                    (ratelimit.LOGIN_PER_IDENTITY, f"mfa:{user.pk}"),
                    (ratelimit.LOGIN_PER_IP, ratelimit.client_ip(request)),
                ]
            )
        except ratelimit.RateLimited as exc:
            restrictions.flash(request, exc)
            return self._render(request, next_url, status=429)

        device = mfa.verified_device(user=user, code=request.POST.get("code", ""))
        if device is None:
            messages.error(request, "That code is not correct. Try the newest one.")
            return self._render(request, next_url, status=400)

        ratelimit.reset(ratelimit.LOGIN_PER_IDENTITY, f"mfa:{user.pk}")
        if pending:
            request.session.pop(MFA_PENDING, None)
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            if request.session.session_key is None:
                request.session.save()
            identity.record_session(
                user=user,
                session_key=request.session.session_key or "",
                ip=ratelimit.client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
            )
        otp_login(request, device)
        return redirect(_safe_next(request, next_url) or home_for(user))

    def _render(self, request: HttpRequest, next_url: str, status: int = 200) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {"page_title": "Two-factor code", "next": _safe_next(request, next_url)},
            status=status,
        )


class PasswordResetRequestView(View):
    """FR-1.3, first half: ask for a link.

    The same answer whether or not the address has an account, so the form
    cannot be used to find out who is registered. The service and both rate
    limits existed from P1; nothing reached them, so a forgotten password was a
    lost account.
    """

    template_name = "accounts/password_reset_request.html"
    SENT = (
        "If an account uses that address, a link to choose a new password is on its "
        "way. It works once, for 24 hours."
    )

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, self.template_name, {"page_title": "Reset your password"})

    def post(self, request: HttpRequest) -> HttpResponse:
        from django.core.exceptions import ValidationError
        from django.core.validators import validate_email
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        email = (request.POST.get("email") or "").strip().lower()
        context = {"page_title": "Reset your password", "email": email}
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Enter the email address you sign in with.")
            return render(request, self.template_name, context, status=400)

        try:
            ratelimit.enforce(
                [
                    (ratelimit.PASSWORD_RESET_PER_IDENTITY, email),
                    (ratelimit.PASSWORD_RESET_PER_IP, ratelimit.client_ip(request)),
                ]
            )
        except ratelimit.RateLimited as exc:
            restrictions.flash(request, exc)
            return render(request, self.template_name, context, status=429)

        found = identity.request_password_reset(email=email)
        if found is not None:
            from apps.notifications import email as email_service

            user, token = found
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            link = request.build_absolute_uri(
                reverse("accounts:password_reset_confirm", args=[uid, token])
            )
            email_service.send(
                to=user.email,
                subject=f"Choose a new {site_name()} password",
                body=(
                    f"Somebody asked to reset the password for this {site_name()} account.\n\n"
                    f"Choose a new one here:\n{link}\n\n"
                    "The link works once and expires in 24 hours. If this was not you, "
                    "ignore this email: your password has not changed."
                ),
            )
        messages.success(request, self.SENT)
        return redirect("accounts:login")


class PasswordResetConfirmView(View):
    """FR-1.3, second half: a single-use, time-limited link sets a new password."""

    template_name = "accounts/password_reset_confirm.html"

    @staticmethod
    def _user(uidb64: str):
        from django.utils.encoding import force_str
        from django.utils.http import urlsafe_base64_decode

        from .models import User

        try:
            pk = force_str(urlsafe_base64_decode(uidb64))
        except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
            return None
        if not pk.isdigit():
            return None
        return User.objects.filter(pk=pk, is_active=True).first()

    def _render(self, request: HttpRequest, *, valid: bool, status: int = 200) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {"page_title": "Choose a new password", "valid": valid},
            status=status,
        )

    def get(self, request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
        from django.contrib.auth.tokens import default_token_generator

        user = self._user(uidb64)
        valid = user is not None and default_token_generator.check_token(user, token)
        return self._render(request, valid=valid, status=200 if valid else 400)

    def post(self, request: HttpRequest, uidb64: str, token: str) -> HttpResponse:
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError

        user = self._user(uidb64)
        if user is None:
            return self._render(request, valid=False, status=400)

        first = request.POST.get("password1") or ""
        second = request.POST.get("password2") or ""
        error = ""
        if first != second:
            error = "The two passwords do not match."
        else:
            try:
                validate_password(first, user=user)
            except ValidationError as exc:
                error = " ".join(exc.messages)
        if error:
            messages.error(request, error)
            return self._render(request, valid=True, status=400)

        try:
            identity.reset_password(
                user=user, token=token, new_password=first, ip=ratelimit.client_ip(request)
            )
        except DomainError as exc:
            restrictions.flash(request, exc)
            return self._render(request, valid=False, status=400)

        messages.success(request, "Password changed. Sign in with the new one.")
        return redirect("accounts:login")


class ResendVerificationView(View):
    """A fresh confirmation link (FR-1.2).

    Applying, posting and AI work all wait for a confirmed address, and the
    only link was the one sent at sign-up: lose that email and the account could
    never be confirmed.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        address = request.user.email_addresses.filter(is_primary=True).first()
        if address is None or address.verified_at is not None:
            messages.info(request, "Your email address is already confirmed.")
            return redirect("settings:security")
        try:
            ratelimit.enforce(
                [(ratelimit.PASSWORD_RESET_PER_IDENTITY, f"verify:{request.user.pk}")]
            )
        except ratelimit.RateLimited as exc:
            restrictions.flash(request, exc)
            return redirect("settings:security")
        SignUpView()._send_verification(request, request.user)
        messages.success(request, f"A new confirmation link is on its way to {address.email}.")
        return redirect("settings:security")
