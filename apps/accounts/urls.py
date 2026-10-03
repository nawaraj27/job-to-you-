from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.SignInView.as_view(), name="login"),
    path("logout/", views.SignOutView.as_view(), name="logout"),
    path("signup/", views.SignUpView.as_view(), name="signup"),
    path("signup/sent/", views.SignUpSentView.as_view(), name="signup_sent"),
    path("login/code/", views.MfaChallengeView.as_view(), name="mfa_challenge"),
    path("password/reset/", views.PasswordResetRequestView.as_view(), name="password_reset"),
    path(
        "password/reset/<str:uidb64>/<str:token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("verify/resend/", views.ResendVerificationView.as_view(), name="verify_resend"),
    path("verify/<str:token>/", views.VerifyEmailView.as_view(), name="verify_email"),
]
