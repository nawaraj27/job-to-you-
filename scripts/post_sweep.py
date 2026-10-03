"""
Whole-site POST sweep: every action endpoint, hostile and empty input.

For each route and persona, POSTs: nothing at all; an unknown command/action;
malformed ids (non-numeric, negative, huge, UUID-shaped garbage); an over-long
string in every commonly used field. Anything answering 5xx, or raising, is a
defect: bad input is the client's problem and must come back as a 4xx or a
message, never as a server error.

Runs on the dev database inside a transaction that is rolled back per persona,
with a savepoint per request so one failure cannot poison the next.

    python scripts/post_sweep.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

import site_sweep  # noqa: E402
from django.db import transaction  # noqa: E402
from django.test import Client  # noqa: E402

PERSONAS = {
    "anonymous": None,
    "candidate": "sita.rai@example.com",
    "owner": "hr@fonepay.example.com",
    "recruiter": "anita.gurung@example.com",
    "viewer": "loop.viewer@example.test",
    "staff": "ops@careerai.example.com",
}

LONG = "x" * 5000
FIELDS = [
    "command",
    "action",
    "id",
    "pk",
    "note_id",
    "tag_id",
    "pool_id",
    "pool",
    "member",
    "invite",
    "membership",
    "session",
    "slot",
    "stage",
    "reason",
    "message",
    "body",
    "title",
    "name",
    "email",
    "role",
    "score",
    "competency",
    "document",
    "version",
    "application",
    "job",
    "question",
    "skill",
    "weight",
    "years",
    "start",
    "minutes",
    "decision",
    "company",
    "kind",
    "format",
    "query",
    "q",
    "page",
    "tag",
    "code",
    "token",
    "upload",
    "document_id",
    "render_id",
    "public_id",
    "thread",
]

PAYLOADS = [
    ("empty", {}),
    ("unknown", {"command": "no_such_command", "action": "no_such_action"}),
    ("nonnumeric", dict.fromkeys(FIELDS, "abc") | {"command": "x", "action": "x"}),
    ("negative", dict.fromkeys(FIELDS, "-1")),
    ("huge", dict.fromkeys(FIELDS, "99999999999999999999")),
    ("uuid-junk", dict.fromkeys(FIELDS, "00000000-0000-0000-0000-000000000000")),
    ("long", dict.fromkeys(FIELDS, LONG)),
]

SKIP = {"accounts:logout"}


def post_routes():
    for pattern, name in site_sweep.routes():
        if name in SKIP or pattern.startswith("^") or name == "?":
            continue
        yield pattern, name


def commands_for(name: str) -> list[str]:
    """The real commands a handler dispatches on, read from its source."""
    return []


def main():
    from apps.accounts.models import User

    failures = []
    count = 0
    for persona, email in PERSONAS.items():
        client = Client(HTTP_HOST="localhost", raise_request_exception=False)
        with transaction.atomic():
            if email:
                client.force_login(User.objects.get(email=email))
            params = site_sweep.params_for(email)
            for pattern, name in post_routes():
                url, missing = site_sweep.fill(pattern, name, params)
                if missing:
                    continue
                for label, data in PAYLOADS:
                    sid = transaction.savepoint()
                    try:
                        response = client.post("/" + url, data)
                        status = response.status_code
                    except Exception as exc:  # noqa: BLE001
                        status = f"exception {type(exc).__name__}: {exc}"
                    count += 1
                    if not isinstance(status, int) or status >= 500:
                        failures.append(
                            {
                                "persona": persona,
                                "route": name,
                                "url": "/" + url,
                                "payload": label,
                                "status": status,
                            }
                        )
                        transaction.savepoint_rollback(sid)
                    else:
                        transaction.savepoint_commit(sid)
            transaction.set_rollback(True)

    out = ROOT / "var" / "benchmark" / "post-sweep.json"
    out.write_text(json.dumps({"requests": count, "failures": failures}, indent=1))
    print(f"requests {count}  failures {len(failures)}")
    seen = set()
    for f in failures:
        key = (f["route"], f["payload"], str(f["status"])[:60])
        if key in seen:
            continue
        seen.add(key)
        print("FAIL", f["persona"], f["route"], f["payload"], str(f["status"])[:160])


if __name__ == "__main__":
    main()
