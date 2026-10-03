"""
Rename `User.role` to `User.signup_intent` (SRS §2.1).

Written by hand. The autodetector produced RemoveField + AddField, which drops
the column and every value in it — on an environment with users that is silent
data loss, and the field records why each of them signed up.

`RenameField` preserves the data; the follow-up `AlterField` applies the new
choices class and help text. The rename is the point of the change: the old name
read like a capability, and authorization comes from `CompanyMembership` and
`StaffRole`, never from a column on the user (ADR-A1).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_staffrole_consent_emailaddress_usersession"),
    ]

    operations = [
        migrations.RenameField(
            model_name="user",
            old_name="role",
            new_name="signup_intent",
        ),
        migrations.AlterField(
            model_name="user",
            name="signup_intent",
            field=models.CharField(
                choices=[
                    ("candidate", "Job seeker"),
                    ("employer", "Employer"),
                    ("staff", "Staff"),
                ],
                default="candidate",
                help_text="Onboarding path only. Never consulted for authorization.",
                max_length=20,
                verbose_name="account type",
            ),
        ),
    ]
