"""
Rewrite the match notes on existing applications so both readers can use them.

The component notes on a match are a rendering of the score, and they were
written for one audience: "In your city.", "this is not counted against you."
The employer's applicant screen shows the same breakdown, so a hiring manager
was reading a sentence addressed to somebody else about somebody else.

Only the wording moves. Every number — points, weight, percent, the score
itself — is left exactly as it was computed on the day, because that is the
part of the snapshot the immutability rule is about (ADR-A19). A note is how a
figure was explained, and explaining it to the wrong person was a defect.
"""

from __future__ import annotations

from django.db import migrations

#: Old wording to new, matched exactly. Anything not in this table is left
#: alone rather than guessed at.
REWORDED = {
    "Not enough industry history to compare — this is not counted against you.":
        "Not enough industry history to compare, so it is not counted either way.",
    "You have worked in this industry.": "Has worked in this industry.",
    "A different industry from the ones on your profile.":
        "A different industry from the ones on the profile.",
    "Remote, which you are open to.": "Remote, which this candidate is open to.",
    "Remote, which your profile does not list as something you want.":
        "Remote, which the profile does not list as something wanted.",
    "In your city.": "Same city.",
    "Your profile does not say where you are.": "The profile does not say where they are.",
    "Hybrid, in a different city — you would need to be there some days.":
        "Hybrid, in a different city — some days on site would mean moving.",
    "The range reaches what you are asking for.":
        "The range reaches what the candidate is asking for.",
    "The top of this range is below what you are asking for.":
        "The top of this range is below what the candidate is asking for.",
    "No pay range to compare.": "No pay range to compare on one side or the other.",
}


def reword(apps, schema_editor):
    Application = apps.get_model("applications", "Application")

    for application in Application.objects.exclude(match_snapshot={}).iterator():
        snapshot = application.match_snapshot or {}
        components = snapshot.get("components")
        if not isinstance(components, list):
            continue

        changed = False
        for component in components:
            replacement = REWORDED.get(component.get("note"))
            if replacement:
                component["note"] = replacement
                changed = True

        if changed:
            application.match_snapshot = snapshot
            application.save(update_fields=["match_snapshot"])


def unreword(apps, schema_editor):
    """Reversible, because a data migration that cannot be undone is a decision
    somebody has to make at 3am rather than a command they can run."""
    Application = apps.get_model("applications", "Application")
    backwards = {new: old for old, new in REWORDED.items()}

    for application in Application.objects.exclude(match_snapshot={}).iterator():
        snapshot = application.match_snapshot or {}
        components = snapshot.get("components")
        if not isinstance(components, list):
            continue

        changed = False
        for component in components:
            replacement = backwards.get(component.get("note"))
            if replacement:
                component["note"] = replacement
                changed = True

        if changed:
            application.match_snapshot = snapshot
            application.save(update_fields=["match_snapshot"])


class Migration(migrations.Migration):
    dependencies = [
        ("applications", "0002_coverletter_ai_run_coverletter_public_id_and_more"),
    ]

    operations = [migrations.RunPython(reword, unreword)]
