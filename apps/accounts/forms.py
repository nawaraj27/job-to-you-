from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, BaseUserCreationForm

User = get_user_model()


class LoginForm(AuthenticationForm):
    """Email login. Labels are written for humans, not for the ORM."""

    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(
            attrs={"autocomplete": "email", "autofocus": True, "placeholder": "you@example.com"}
        ),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password", "placeholder": "••••••••"}
        ),
    )

    error_messages = {
        "invalid_login": "That email and password don't match an account.",
        "inactive": "This account has been deactivated.",
    }


class SignupForm(BaseUserCreationForm):
    full_name = forms.CharField(
        label="Full name",
        max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "name", "placeholder": "Jane Doe"}),
    )
    email = forms.EmailField(
        label="Work or personal email",
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "you@example.com"}),
    )
    # Named for the model field it fills. It was `role` until migration 0004
    # renamed the column to `signup_intent` and the form was not renamed with
    # it: the template asked for `form.signup_intent`, Django resolved the
    # missing attribute to an empty string, and the choice rendered as a bare
    # "(optional)" label with no radios under it. Nobody could say they were
    # here to hire, every account was created as a job seeker, and the employer
    # navigation the context processor gates on that field never appeared.
    signup_intent = forms.ChoiceField(
        label="I'm here to",
        choices=(
            (User.SignupIntent.CANDIDATE, "Find a job"),
            (User.SignupIntent.EMPLOYER, "Hire people"),
        ),
        initial=User.SignupIntent.CANDIDATE,
        widget=forms.RadioSelect(attrs={"class": "mt-0.5 accent-brand-600"}),
    )

    class Meta:
        model = User
        fields = ("full_name", "email", "signup_intent")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs.update(
            {"autocomplete": "new-password", "placeholder": "At least 8 characters"}
        )
        self.fields["password2"].widget.attrs.update(
            {"autocomplete": "new-password", "placeholder": "Repeat password"}
        )
        self.fields["password1"].label = "Password"
        self.fields["password2"].label = "Confirm password"

    def clean_email(self):
        # Decision D-12: no "an account already uses this email". Whether the
        # address is registered is not the form's to say; the view answers every
        # valid submission the same way and tells the address's owner by email.
        return self.cleaned_data["email"].lower().strip()

    def validate_unique(self):
        # The model's unique check on `email` would say the same thing in
        # Django's words ("User with this Email already exists"). The service
        # owns uniqueness; every other field is still checked.
        exclude = self._get_validation_exclusions()
        exclude.add("email")
        try:
            self.instance.validate_unique(exclude=exclude)
        except forms.ValidationError as error:
            self._update_errors(error)
