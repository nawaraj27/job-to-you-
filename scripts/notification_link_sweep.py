"""
Open every notification as its recipient and follow where it leads.

A notification whose link lands on a 404, a 403 or a server error is a promise
the product made and broke — usually because the thing it pointed at changed
state, or because the link was built for the wrong portal. Runs on the dev
database, rolled back.

    python scripts/notification_link_sweep.py
"""

from __future__ import annotations

import collections
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402


def main() -> None:
    from apps.notifications.models import Notification

    results = collections.Counter()
    broken = []
    with transaction.atomic():
        by_user = collections.defaultdict(list)
        for note in Notification.objects.select_related("recipient").order_by("recipient_id"):
            by_user[note.recipient].append(note)
        for user, notes in by_user.items():
            client = Client(HTTP_HOST="localhost")
            client.force_login(user)
            for note in notes:
                opened = client.get(reverse("notifications:open", args=[note.public_id]))
                final = opened.status_code
                location = opened.get("Location", "")
                if final in (301, 302) and location.startswith("/"):
                    final = client.get(location).status_code
                results[(note.kind, final)] += 1
                if final >= 400:
                    broken.append(
                        {"kind": note.kind, "user": user.email, "link": location, "status": final}
                    )
        transaction.set_rollback(True)

    out = ROOT / "var" / "benchmark" / "notification-link-sweep.json"
    rows = [{"kind": k, "status": s, "count": c} for (k, s), c in sorted(results.items())]
    out.write_text(json.dumps({"results": rows, "broken": broken}, indent=1))
    print("opened", sum(results.values()), "broken", len(broken))
    seen = set()
    for row in broken:
        key = (row["kind"], row["status"])
        if key not in seen:
            seen.add(key)
            print("BROKEN", row)


if __name__ == "__main__":
    main()
