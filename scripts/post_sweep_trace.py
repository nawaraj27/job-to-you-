"""Re-run each distinct POST-sweep failure with exceptions raised, and print where."""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
os.environ["DJANGO_EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"

import django  # noqa: E402

django.setup()

import post_sweep  # noqa: E402
from django.db import transaction  # noqa: E402
from django.test import Client  # noqa: E402

data = json.loads((ROOT / "var" / "benchmark" / "post-sweep.json").read_text())
payloads = dict(post_sweep.PAYLOADS)
seen = set()
for failure in data["failures"]:
    key = (failure["route"],)
    if key in seen:
        continue
    seen.add(key)
    from apps.accounts.models import User

    email = post_sweep.PERSONAS[failure["persona"]]
    client = Client(HTTP_HOST="localhost", raise_request_exception=True)
    with transaction.atomic():
        if email:
            client.force_login(User.objects.get(email=email))
        try:
            client.post(failure["url"], payloads[failure["payload"]])
            print("NO EXCEPTION NOW", failure["route"])
        except Exception:  # noqa: BLE001
            frames = traceback.format_exc().strip().splitlines()
            ours = [line for line in frames if "job to you" in line and "site-packages" not in line]
            print("==", failure["route"], failure["payload"])
            print("   ", ours[-1].strip() if ours else "")
            print("   ", frames[-1][:300])
        transaction.set_rollback(True)
