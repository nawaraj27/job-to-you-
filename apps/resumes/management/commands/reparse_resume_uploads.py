"""
Re-read uploads that an older engine parsed.

    python manage.py reparse_resume_uploads --dry-run
    python manage.py reparse_resume_uploads

A parse is a pure function of (file, engine version), and `ResumeParse` stores
the version that produced it precisely so that this is possible. Without it an
improvement to the reader reaches nobody who already uploaded: their CV keeps
the structure the old engine found, the analysis keeps scoring that structure,
and the import list keeps offering whatever it mis-read — which is the state
every existing candidate is in the moment extraction changes.

Bounded on purpose. Re-parsing is CPU work on private files, so it runs in
batches with a ceiling rather than sweeping a whole table in one transaction.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.common.errors import DomainError
from apps.files.constants import ScanStatus
from apps.resumes.models import ResumeUpload
from apps.resumes.parsing import ENGINE, ENGINE_VERSION
from apps.resumes.services import parse as parse_service
from apps.resumes.services import proposals as proposal_service


class Command(BaseCommand):
    help = "Re-parse owned uploads whose stored parse came from an older engine."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="List, change nothing.")
        parser.add_argument("--limit", type=int, default=500, help="Most uploads to touch.")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Re-parse every owned upload, not only the out-of-date ones.",
        )

    def handle(self, *args, **options) -> None:
        queryset = (
            ResumeUpload.objects.owned()
            .filter(attachment__scan_status=ScanStatus.CLEAN, attachment__deleted_at__isnull=True)
            .select_related("attachment", "profile")
            .order_by("pk")
        )
        if not options["all"]:
            queryset = queryset.exclude(parse__engine=ENGINE, parse__engine_version=ENGINE_VERSION)

        uploads = list(queryset[: options["limit"]])
        if not uploads:
            self.stdout.write(f"Nothing to do — every parse is on {ENGINE} {ENGINE_VERSION}.")
            return

        if options["dry_run"]:
            for upload in uploads:
                current = getattr(upload, "parse", None)
                was = f"{current.engine} {current.engine_version}" if current else "never parsed"
                self.stdout.write(f"{upload.public_id}  {upload.original_filename}  ({was})")
            self.stdout.write(f"{len(uploads)} upload(s) would be re-parsed.")
            return

        done = failed = 0
        for upload in uploads:
            try:
                parse = parse_service.parse_upload(upload=upload)
            except DomainError as exc:
                # An unreadable file is a result, not a reason to stop: the
                # other four hundred uploads are still waiting.
                self.stderr.write(f"{upload.public_id}: {exc.message}")
                failed += 1
                continue

            if upload.profile is not None:
                proposal_service.build_from_parse(parse=parse, profile=upload.profile)
            done += 1

        self.stdout.write(f"re-parsed {done}, failed {failed}, engine {ENGINE} {ENGINE_VERSION}")
