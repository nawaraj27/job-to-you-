"""
The interview benchmark. Rerunnable; writes a JSON report.

    python manage.py interview_benchmark                 # question quality only, no model
    python manage.py interview_benchmark --live          # + grounding and follow-ups on the real model
    python manage.py interview_benchmark --live --plan 3 # + write 3 kits from scratch

Four sections, and what each one can and cannot show:

  * **quality** reads every kit already written and measures what a candidate
    is actually served. The non-behavioural rate of *served* questions is a
    guarantee check (the product filters them), so it is reported beside the
    rate in the *stored* model output, which the filter does not change. The
    other measures — anchoring to the posting, near-duplicates, overlap between
    unrelated roles, fairness terms, level fit — are not filtered on anywhere
    and are independent.
  * **grounding** scores adversarial transcripts through the real evaluation
    service on the configured model, inside a transaction that is rolled back.
  * **followup** asks the real model for follow-ups to strong, weak, partial,
    irrelevant and self-correcting answers.
  * **plan** writes fresh kits for unrelated roles and measures schema success,
    latency and what the model wrote before filtering.

Expectations are ranges written before running, never exact text. A failing
case is a finding about the product, not a case to edit.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

CASES = Path(__file__).resolve().parents[2] / "benchmark_cases.json"

_STOP = frozenset(
    [
        "a",
        "about",
        "after",
        "again",
        "also",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "describe",
        "did",
        "do",
        "does",
        "example",
        "for",
        "from",
        "give",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "like",
        "make",
        "me",
        "more",
        "most",
        "much",
        "my",
        "not",
        "now",
        "of",
        "on",
        "one",
        "only",
        "or",
        "our",
        "out",
        "over",
        "said",
        "same",
        "she",
        "some",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "through",
        "time",
        "to",
        "told",
        "too",
        "walk",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)
_GENERIC = frozenset(
    [
        "work",
        "team",
        "project",
        "company",
        "role",
        "people",
        "time",
        "thing",
        "things",
        "year",
        "years",
        "experience",
        "manager",
        "customer",
        "client",
        "process",
        "system",
        "product",
        "data",
        "business",
        "issue",
        "problem",
        "situation",
        "task",
        "tasks",
        "example",
    ]
)
_UNFAIR = frozenset(
    [
        "age",
        "aged",
        "married",
        "marital",
        "spouse",
        "religion",
        "religious",
        "nationality",
        "citizenship",
        "children",
        "pregnant",
        "pregnancy",
        "disability",
        "disabled",
        "caste",
        "ethnicity",
        "gender",
        "sexual",
        "health",
    ]
)
_SENIOR_ONLY = (
    "delegat",
    "mentor",
    "direct report",
    "hiring decision",
    "hired ",
    "performance review",
    "managed a team",
    "led a team",
)
_JUNIOR = ("intern", "entry", "junior", "graduate", "trainee")
_PRAISE = (
    "great answer",
    "good answer",
    "excellent",
    "impressive",
    "well done",
    "that's great",
    "interesting",
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z0-9+#.-]*", (text or "").lower())


def _stems(text: str) -> set[str]:
    return {word[:5] for word in _words(text) if len(word) > 2 and word not in _STOP}


class Command(BaseCommand):
    help = "Benchmark interview question quality, grounding and follow-ups."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--live", action="store_true", help="Call the configured model.")
        parser.add_argument(
            "--plan", type=int, default=0, help="Write this many kits from scratch."
        )
        parser.add_argument("--only", default="", help="Comma-separated sections to run.")
        parser.add_argument(
            "--repeat", type=int, default=1, help="Run live sections this many times (variance)."
        )
        parser.add_argument("--set", default="", help="Only cases from this corpus set (v1, v2).")
        parser.add_argument(
            "--postings",
            type=int,
            default=0,
            help="Generate kits for this many representative postings (benchmark_postings.json).",
        )
        parser.add_argument("--out", default="", help="Where to write the JSON report.")
        parser.add_argument(
            "--audio-dir",
            default="var/benchmark/speech",
            help="Rendered speech corpus (scripts/render_speech_corpus.ps1).",
        )

    def handle(self, *args, **options) -> None:
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        only = {part for part in options["only"].split(",") if part}
        report: dict[str, Any] = {"ran_at": timezone.now().isoformat(), "sections": {}}

        def wanted(name: str) -> bool:
            return not only or name in only

        if wanted("quality"):
            report["sections"]["quality"] = self.quality()

        def chosen(rows):
            return [row for row in rows if not options["set"] or row.get("set") == options["set"]]

        if options["live"] and wanted("grounding"):
            report["sections"]["grounding"] = self.grounding(
                chosen(cases["evaluation"]), repeat=options["repeat"]
            )
        if options["live"] and wanted("followup"):
            report["sections"]["followup"] = self.followup(
                chosen(cases["followup"]), repeat=options["repeat"]
            )
        if wanted("speech") and options["only"] and "speech" in only:
            report["sections"]["speech"] = self.speech(options["audio_dir"])
        if wanted("relevance") and "relevance" in only:
            report["sections"]["relevance"] = self.relevance()
        if options["plan"] and wanted("plan"):
            report["sections"]["plan"] = self.plan(cases["plan_jobs"][: options["plan"]])
        if options["postings"] and wanted("postings"):
            report["sections"]["postings"] = self.postings(limit=options["postings"])

        out = (
            Path(options["out"])
            if options["out"]
            else (
                Path(settings.BASE_DIR)
                / "var"
                / "benchmark"
                / f"interview-{timezone.now():%Y%m%d-%H%M%S}.json"
            )
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        self.stdout.write(f"\nreport: {out}")

    # ------------------------------------------------------------------ quality

    def quality(self) -> dict[str, Any]:
        from apps.interviews.models import InterviewTemplate
        from apps.interviews.services import planning, relevance
        from apps.jobs.models import Job

        kits: list[dict[str, Any]] = []
        served_by_job: dict[str, set[str]] = {}
        for template in InterviewTemplate.objects.filter(config__source=planning.AUTO_SOURCE):
            job_id = (template.config or {}).get("job_id")
            if not job_id:
                continue
            job = Job.objects.filter(pk=job_id).prefetch_related("skills__skill").first()
            if job is None:
                continue
            stored = template.question_pools or {}
            # Exactly what `planning.cached_pools` serves a candidate.
            served = planning._pools_from(
                {"competencies": [{"key": key, **pool} for key, pool in stored.items()]},
                junior=planning.is_junior(job),
                posting=relevance.Posting.from_job(job),
                other_phrases=planning.other_posting_phrases(job),
            )
            vocab = (
                (
                    {w for w in _words(job.title + " " + (job.description or "")) if len(w) > 3}
                    | {w for row in job.skills.all() for w in _words(row.skill.name)}
                )
                - _STOP
                - _GENERIC
            )
            level = f"{job.experience_level} {job.title}".lower()
            junior = any(token in level for token in _JUNIOR)

            stored_questions = [q for pool in stored.values() for q in pool.get("questions", [])]
            served_questions = [q for pool in served.values() for q in pool.get("questions", [])]
            served_by_job[job.title] = (
                set().union(*(_stems(q) for q in served_questions)) if served_questions else set()
            )

            anchored_competencies = 0
            duplicates = 0
            for pool in served.values():
                questions = pool["questions"]
                anchored = [q for q in questions if (set(_words(q)) & vocab)]
                if questions and len(anchored) / len(questions) >= 2 / 3:
                    anchored_competencies += 1
                for i, first in enumerate(questions):
                    for second in questions[i + 1 :]:
                        a, b = _stems(first), _stems(second)
                        if a and b and len(a & b) / min(len(a), len(b)) >= 0.6:
                            duplicates += 1

            kits.append(
                {
                    "job": job.title,
                    "level": job.experience_level,
                    "competencies_served": len(served),
                    "stored_questions": len(stored_questions),
                    "stored_non_behavioural": sum(
                        planning._is_hypothetical(q) for q in stored_questions
                    ),
                    "served_questions": len(served_questions),
                    "served_non_behavioural": sum(
                        planning._is_hypothetical(q) for q in served_questions
                    ),
                    "served_anchored": sum(bool(set(_words(q)) & vocab) for q in served_questions),
                    "competencies_meeting_two_thirds_anchor": anchored_competencies,
                    "near_duplicate_pairs": duplicates,
                    "unfair_terms": sorted(
                        {w for q in served_questions for w in _words(q) if w in _UNFAIR}
                    ),
                    # Served mismatches are a guarantee check (filtered); stored
                    # ones measure what the model wrote.
                    "stored_level_mismatch": [
                        q
                        for q in stored_questions
                        if junior and any(t in q.lower() for t in _SENIOR_ONLY)
                    ],
                    "level_mismatch": [
                        q
                        for q in served_questions
                        if junior and any(t in q.lower() for t in _SENIOR_ONLY)
                    ],
                    "thin_competencies": [
                        k for k, pool in served.items() if len(pool["questions"]) < 2
                    ],
                }
            )

        overlaps = []
        titles = sorted(served_by_job)
        for i, first in enumerate(titles):
            for second in titles[i + 1 :]:
                a, b = served_by_job[first], served_by_job[second]
                if a and b:
                    overlaps.append((len(a & b) / len(a | b), first, second))
        overlaps.sort(reverse=True)

        total_stored = sum(k["stored_questions"] for k in kits) or 1
        total_served = sum(k["served_questions"] for k in kits) or 1
        total_comps = sum(k["competencies_served"] for k in kits) or 1
        summary: dict[str, Any] = {
            "kits": len(kits),
            "stored_questions": total_stored,
            "served_questions": total_served,
            "stored_non_behavioural_rate": round(
                sum(k["stored_non_behavioural"] for k in kits) / total_stored, 3
            ),
            "served_non_behavioural_rate__guarantee": round(
                sum(k["served_non_behavioural"] for k in kits) / total_served, 3
            ),
            "served_anchored_rate": round(
                sum(k["served_anchored"] for k in kits) / total_served, 3
            ),
            "competencies_meeting_two_thirds_anchor_rate": round(
                sum(k["competencies_meeting_two_thirds_anchor"] for k in kits) / total_comps, 3
            ),
            "near_duplicate_pairs": sum(k["near_duplicate_pairs"] for k in kits),
            "kits_with_unfair_terms": [k["job"] for k in kits if k["unfair_terms"]],
            "stored_level_mismatch_questions": sum(len(k["stored_level_mismatch"]) for k in kits),
            "served_level_mismatch_questions__guarantee": sum(
                len(k["level_mismatch"]) for k in kits
            ),
            "thin_competencies": sum(len(k["thin_competencies"]) for k in kits),
            "role_overlap_jaccard_median": round(statistics.median(o[0] for o in overlaps), 3)
            if overlaps
            else None,
            "role_overlap_top3": [(round(o[0], 3), o[1], o[2]) for o in overlaps[:3]],
        }
        self._print("quality", summary)
        return {"summary": summary, "kits": kits}

    # ------------------------------------------------------------------ grounding

    def grounding(self, cases: list[dict[str, Any]], *, repeat: int = 1) -> dict[str, Any]:
        from apps.ai.models import AIRun
        from apps.interviews.services import evaluation

        results: list[dict[str, Any]] = []
        for run_index in range(max(repeat, 1)):
            for case in cases:
                started = time.monotonic()
                with transaction.atomic():
                    session = self._synthetic_session(case)
                    before = set(
                        AIRun.objects.filter(task_key="interview.evaluate").values_list(
                            "pk", flat=True
                        )
                    )
                    row = evaluation._score_competency(
                        session=session, competency=session.plan["competencies"][0]
                    )
                    runs = list(
                        AIRun.objects.filter(task_key="interview.evaluate")
                        .exclude(pk__in=before)
                        .values("status", "latency_ms", "tokens_in", "tokens_out")
                    )
                    outcome = self._judge_evaluation(case, row, len(runs))
                    outcome.update(
                        {
                            "run": run_index + 1,
                            "set": case.get("set", ""),
                            "category": case.get("category", ""),
                            "technical": case.get("technical"),
                            "level": case.get("level", ""),
                            "seconds": round(time.monotonic() - started, 1),
                            "model_calls": len(runs),
                            "run_statuses": [r["status"] for r in runs],
                            "call_seconds": [round((r["latency_ms"] or 0) / 1000, 1) for r in runs],
                            "tokens_in": [r["tokens_in"] for r in runs],
                            "tokens_out": [r["tokens_out"] for r in runs],
                            "band": [
                                case["expect"].get("score_min", 1),
                                case["expect"].get("score_max", 5),
                            ],
                        }
                    )
                    transaction.set_rollback(True)
                results.append(outcome)
                self.stdout.write(
                    f"  r{run_index + 1} {'PASS' if outcome['passed'] else 'FAIL'} {case['name']}: "
                    f"score={outcome.get('score')} band={outcome['band']} grounded={outcome.get('grounded')} "
                    f"{outcome['seconds']}s {'; '.join(outcome['failures'])}"
                )

        summary = _evaluation_summary(results, repeat=max(repeat, 1))
        self._print("grounding", summary)
        return {"summary": summary, "cases": results}

    def _synthetic_session(self, case: dict[str, Any]):
        from apps.candidates.models import CandidateProfile
        from apps.interviews.models import (
            InterviewKind,
            InterviewSession,
            InterviewTemplate,
            InterviewTurn,
            SessionState,
        )

        key = re.sub(r"[^a-z]+", "_", case["competency"].lower()).strip("_")
        candidate = CandidateProfile.objects.order_by("pk").first()
        template = InterviewTemplate.objects.filter(
            company__isnull=True, kind=InterviewKind.AI_TEXT
        ).first()
        assert candidate is not None and template is not None, "seed demo data first"
        session = InterviewSession.objects.create(
            candidate=candidate,
            template=template,
            kind=InterviewKind.AI_TEXT,
            state=SessionState.COMPLETED,
            started_at=timezone.now() - dt.timedelta(minutes=20),
            ended_at=timezone.now(),
            plan={
                "job_level": case["level"],
                "competencies": [
                    {"key": key, "label": case["competency"], "why": case["why"], "questions": []}
                ],
            },
        )
        for order, (question, answer) in enumerate(case["turns"]):
            InterviewTurn.objects.create(
                session=session,
                order=order,
                competency=key,
                question_text=question,
                answer_text=answer,
                answered_at=timezone.now(),
            )
        return session

    def _judge_evaluation(self, case: dict[str, Any], row: Any, calls: int) -> dict[str, Any]:
        expect = case["expect"]
        failures: list[str] = []
        if row is None:
            return {
                "name": case["name"],
                "passed": False,
                "score": None,
                "failures": ["model unavailable"],
            }

        rationale = (row.rationale or "").lower()
        if "score_min" in expect and row.score < expect["score_min"]:
            failures.append(f"score {row.score} < {expect['score_min']}")
        if "score_max" in expect and row.score > expect["score_max"]:
            failures.append(f"score {row.score} > {expect['score_max']}")
        if expect.get("grounded") and not row.grounded:
            failures.append("quotation not found in answers")
        if expect.get("no_model_call") and calls:
            failures.append(f"{calls} model call(s) for a non-answer")
        answers = " ".join(turn.answer_text.lower() for turn in row.session.turns.all())
        for term in expect.get("rationale_absent", []):
            if term in rationale and term not in answers:
                failures.append(f"rationale asserts {term!r}")
        return {
            "name": case["name"],
            "passed": not failures,
            "score": row.score,
            "grounded": row.grounded,
            "rationale": row.rationale,
            "evidence": row.evidence,
            "failures": failures,
        }

    # ------------------------------------------------------------------ follow-ups

    def followup(self, cases: list[dict[str, Any]], *, repeat: int = 1) -> dict[str, Any]:
        from apps.ai.models import AIRun
        from apps.interviews.services import engine

        results: list[dict[str, Any]] = []
        for run_index in range(max(repeat, 1)):
            for case in cases:
                before = set(
                    AIRun.objects.filter(task_key="interview.followup").values_list("pk", flat=True)
                )
                started = time.monotonic()
                # Through the engine, so correction handling and its retry are
                # what is measured, not the bare prompt.
                question, run = engine.write_followup(
                    competency=case["competency"], question=case["question"], answer=case["answer"]
                )
                seconds = round(time.monotonic() - started, 1)
                runs = list(
                    AIRun.objects.filter(task_key="interview.followup")
                    .exclude(pk__in=before)
                    .values("status", "latency_ms", "tokens_in", "tokens_out")
                )
                failures = _judge_followup(case, question, run)
                results.append(
                    {
                        "run": run_index + 1,
                        "name": case["name"],
                        "set": case.get("set", ""),
                        "technical": case.get("technical"),
                        "question": question,
                        "seconds": seconds,
                        "model_calls": len(runs),
                        "call_seconds": [round((r["latency_ms"] or 0) / 1000, 1) for r in runs],
                        "tokens_in": [r["tokens_in"] for r in runs],
                        "tokens_out": [r["tokens_out"] for r in runs],
                        "passed": not failures,
                        "failures": failures,
                    }
                )
                self.stdout.write(
                    f"  r{run_index + 1} {'PASS' if not failures else 'FAIL'} {case['name']} {seconds}s: "
                    f"{question[:100]} {'; '.join(failures)}"
                )

        summary = _followup_summary(results, repeat=max(repeat, 1))
        self._print("followup", summary)
        return {"summary": summary, "cases": results}

    # ------------------------------------------------------------------ plan

    def plan(self, titles: list[str]) -> dict[str, Any]:
        from apps.ai.models import RunStatus
        from apps.ai.services import runner
        from apps.interviews.services import planning
        from apps.jobs.models import Job

        results: list[dict[str, Any]] = []
        for title in titles:
            job = (
                Job.objects.filter(title__icontains=title)
                .select_related("company")
                .prefetch_related("skills__skill")
                .first()
            )
            if job is None:
                results.append({"job": title, "status": "no such job"})
                continue
            started = time.monotonic()
            run = runner.run(
                task_key="interview.plan",
                inputs={
                    "job_title": job.title,
                    "experience_level": job.get_experience_level_display() or "unspecified",
                    "company": job.company.name if job.company_id else "not stated",
                    "job_description": (job.description or "")[:6_000],
                    "skills": ", ".join(row.skill.name for row in job.skills.all()[:20])
                    or "none listed",
                },
                company=job.company,
                force=True,
            )
            seconds = round(time.monotonic() - started, 1)
            raw = [
                q
                for item in (run.output or {}).get("competencies", [])
                for q in item.get("questions", [])
            ]
            served = planning._pools_from(run.output or {}) if run.status == RunStatus.OK else {}
            results.append(
                {
                    "job": job.title,
                    "status": run.status,
                    "seconds": seconds,
                    "raw_questions": len(raw),
                    "raw_non_behavioural": sum(planning._is_hypothetical(q) for q in raw),
                    "served_competencies": len(served),
                    "served_questions": sum(len(p["questions"]) for p in served.values()),
                    "competency_keys": sorted(served),
                    "error": run.error[:200],
                }
            )
            self.stdout.write(
                f"  {job.title}: {run.status} {seconds}s raw={len(raw)} nonbeh={results[-1]['raw_non_behavioural']}"
            )

        ok = [r for r in results if r.get("status") == RunStatus.OK]
        raw_total = sum(r["raw_questions"] for r in ok) or 1
        summary: dict[str, Any] = {
            "jobs": len(results),
            "ok": len(ok),
            "raw_non_behavioural_rate": round(
                sum(r["raw_non_behavioural"] for r in ok) / raw_total, 3
            )
            if ok
            else None,
            "median_seconds": statistics.median(r["seconds"] for r in results if "seconds" in r)
            if results
            else None,
        }
        self._print("plan", summary)
        return {"summary": summary, "jobs": results}

    # ------------------------------------------------------------------ speech

    def speech(self, audio_dir: str) -> dict[str, Any]:
        """Word error rate and speed of the configured transcriber on the corpus.

        Also exercises the delivery measurements on real word timings, and the
        long-pause case, which only real audio can show. Runs only when asked
        for (`--only speech`), because it needs the rendered audio files.
        """
        import wave

        from apps.interviews import speech
        from apps.interviews.services import delivery

        corpus = json.loads((CASES.parent / "speech_benchmark.json").read_text(encoding="utf-8"))[
            "cases"
        ]
        engine = speech.transcriber()
        results: list[dict[str, Any]] = []
        for case in corpus:
            for suffix in (".webm", ".wav"):
                path = Path(audio_dir) / f"{case['name']}{suffix}"
                if path.exists():
                    break
            else:
                results.append({"name": case["name"], "status": "missing audio"})
                continue

            started = time.monotonic()
            try:
                transcript = engine.transcribe(str(path))
            except (speech.SpeechUnavailable, speech.SpeechFailed) as exc:
                results.append({"name": case["name"], "status": type(exc).__name__})
                continue
            seconds = time.monotonic() - started

            reference, hypothesis = (
                _normalise_speech(case["text"]),
                _normalise_speech(transcript.text),
            )
            errors = _edit_distance(reference, hypothesis)
            measured = delivery.signals(transcript)
            duration = transcript.duration_seconds
            if not duration and path.suffix == ".wav":
                with wave.open(str(path)) as handle:
                    duration = handle.getnframes() / handle.getframerate()
            results.append(
                {
                    "name": case["name"],
                    "format": path.suffix,
                    "status": "ok",
                    "reference_words": len(reference),
                    "errors": errors,
                    "wer": round(errors / max(len(reference), 1), 4),
                    "audio_seconds": round(duration, 1),
                    "transcribe_seconds": round(seconds, 2),
                    "long_pauses": measured["long_pauses"],
                    "words_per_minute": measured["words_per_minute"],
                    "expected_long_pauses": len(case["text"].split(". ")) - 1
                    if case.get("pause_after_sentences_ms", 0) >= 2000
                    else 0,
                    "text": transcript.text,
                }
            )
            self.stdout.write(
                f"  {case['name']:24s} {path.suffix} WER={errors / max(len(reference), 1):.1%} "
                f"{seconds:.2f}s pauses={measured['long_pauses']} wpm={measured['words_per_minute']}"
            )

        ok = [r for r in results if r.get("status") == "ok"]
        words = sum(r["reference_words"] for r in ok) or 1
        pause_cases = [r for r in ok if r["expected_long_pauses"]]
        summary: dict[str, Any] = {
            "engine": getattr(engine, "key", ""),
            "cases": len(results),
            "transcribed": len(ok),
            "corpus_wer": round(sum(r["errors"] for r in ok) / words, 4),
            "worst_case": max(ok, key=lambda r: r["wer"])["name"] if ok else None,
            "median_transcribe_seconds": statistics.median(r["transcribe_seconds"] for r in ok)
            if ok
            else None,
            "long_pauses_detected": [
                (r["name"], r["long_pauses"], r["expected_long_pauses"]) for r in pause_cases
            ],
        }
        self._print("speech", summary)
        return {"summary": summary, "cases": results}

    # ------------------------------------------------------------------ relevance

    def relevance(self) -> dict[str, Any]:
        """Job-specificity rules against hand labels, with a held-out threshold."""
        from apps.interviews.services import relevance

        labels = json.loads((CASES.parent / "relevance_labels.json").read_text(encoding="utf-8"))[
            "labels"
        ]
        from apps.jobs.models import Job

        jobs = {j.pk: j for j in Job.objects.prefetch_related("skills__skill").all()}
        postings = {pk: relevance.Posting.from_job(j) for pk, j in jobs.items()}
        phrases = {pk: p.phrases() for pk, p in postings.items()}
        embed = relevance.Embedder()

        rows = []
        started = time.monotonic()
        for label in labels:
            posting = postings[label["job"]]
            others = set().union(*(phrases[o] for o in phrases if o != label["job"]))
            scored = relevance.score_questions(
                [label["q"]], posting, other_phrases=others, embed=embed
            )[0]
            rows.append(
                {
                    "job": label["job"],
                    "truth": label["label"] == "specific",
                    "label": label["label"],
                    "old_lexical": _old_lexical(label["q"], jobs[label["job"]]),
                    "stem": scored.lexical,
                    "borrowed": bool(scored.borrowed),
                    "semantic": scored.semantic,
                }
            )
        embed_seconds = round(time.monotonic() - started, 1)

        def predict(row, rule, threshold):
            semantic_ok = row["semantic"] is not None and row["semantic"] >= threshold
            if rule == "old_lexical":
                return row["old_lexical"]
            if rule == "stem":
                return row["stem"]
            if rule == "stem_minus_borrowed":
                return row["stem"] and not row["borrowed"]
            if rule == "semantic_only":
                return semantic_ok
            return (row["stem"] or semantic_ok) and not row["borrowed"]

        thresholds = [round(0.40 + 0.025 * i, 3) for i in range(17)]

        def best_threshold(subset, rule):
            return max(
                thresholds,
                key=lambda t: (
                    _metrics([predict(r, rule, t) for r in subset], [r["truth"] for r in subset])[
                        "f1"
                    ],
                    t,
                ),
            )

        job_ids = sorted({r["job"] for r in rows})
        folds = [set(job_ids[0::2]), set(job_ids[1::2])]
        held_out: dict[str, dict[str, Any]] = {}
        for rule in ("semantic_only", "combined"):
            predictions, truths, chosen = [], [], []
            for fold in folds:
                train = [r for r in rows if r["job"] not in fold]
                test = [r for r in rows if r["job"] in fold]
                t = best_threshold(train, rule)
                chosen.append(t)
                predictions += [predict(r, rule, t) for r in test]
                truths += [r["truth"] for r in test]
            held_out[rule] = {**_metrics(predictions, truths), "thresholds_chosen_per_fold": chosen}

        truths = [r["truth"] for r in rows]
        in_sample = {
            rule: _metrics([predict(r, rule, relevance.SEMANTIC_THRESHOLD) for r in rows], truths)
            for rule in ("old_lexical", "stem", "stem_minus_borrowed", "semantic_only", "combined")
        }
        sweep = {
            str(t): _metrics([predict(r, "combined", t) for r in rows], truths)["f1"]
            for t in thresholds
        }
        wrong = [r for r in rows if r["label"] == "wrong_job"]
        summary: dict[str, Any] = {
            "labelled_questions": len(rows),
            "label_counts": {
                k: sum(1 for r in rows if r["label"] == k)
                for k in ("specific", "generic", "wrong_job")
            },
            "configured_threshold": relevance.SEMANTIC_THRESHOLD,
            "in_sample_at_configured_threshold": in_sample,
            "held_out_two_fold_by_job": held_out,
            "combined_f1_by_threshold": sweep,
            "wrong_job_flagged": f"{sum(r['borrowed'] for r in wrong)}/{len(wrong)}",
            "embedding_seconds_total": embed_seconds,
        }
        self._print("relevance", summary)
        return {"summary": summary, "rows": rows}

    # ------------------------------------------------------------------ representative postings

    def postings(self, *, limit: int) -> dict[str, Any]:
        """Generate kits for representative non-demo postings and measure them."""
        from apps.ai.models import RunStatus
        from apps.ai.services import runner
        from apps.interviews.services import planning, relevance

        fixture = json.loads(
            (CASES.parent / "benchmark_postings.json").read_text(encoding="utf-8")
        )["postings"][:limit]
        postings = {
            row["key"]: relevance.Posting(
                title=row["title"],
                description=row["description"],
                skills=row["skills"],
                level=row["level"],
            )
            for row in fixture
        }
        from apps.jobs.models import Job

        db_phrases = set()
        for job in Job.objects.filter(deleted_at__isnull=True).prefetch_related("skills__skill")[
            :500
        ]:
            db_phrases |= relevance.Posting.from_job(job).phrases()
        embed = relevance.Embedder()
        results: list[dict[str, Any]] = []
        for row in fixture:
            posting = postings[row["key"]]
            others = db_phrases | set().union(
                *(p.phrases() for k, p in postings.items() if k != row["key"])
            )
            inputs = {
                "job_title": posting.title,
                "experience_level": posting.level,
                "company": "a representative employer",
                "job_description": posting.description,
                "skills": ", ".join(posting.skills),
                "responsibilities": "\n".join(
                    f"{n}. {sentence}"
                    for n, sentence in enumerate(posting.responsibilities(), start=1)
                ),
            }
            started = time.monotonic()
            run = runner.run(task_key="interview.plan", inputs=inputs, force=True)
            attempts = 1
            if run.status == RunStatus.SCHEMA_ERROR:
                run = runner.run(task_key="interview.plan", inputs=inputs, force=True)
                attempts = 2
            seconds = round(time.monotonic() - started, 1)
            if run.status != RunStatus.OK:
                results.append(
                    {
                        "posting": row["key"],
                        "status": run.status,
                        "seconds": seconds,
                        "attempts": attempts,
                    }
                )
                self.stdout.write(f"  {row['key']}: {run.status} {seconds}s")
                continue
            raw = [
                q
                for item in (run.output or {}).get("competencies", [])
                for q in item.get("questions", [])
            ]
            junior = posting.level.lower() in {"intern", "entry level"}
            served_pools = planning._pools_from(
                run.output or {}, junior=junior, posting=posting, other_phrases=others
            )
            served = [q for pool in served_pools.values() for q in pool["questions"]]
            scored = relevance.score_questions(served, posting, other_phrases=others, embed=embed)
            raw_scored = relevance.score_questions(raw, posting, other_phrases=others, embed=embed)
            duplicates = 0
            for pool in served_pools.values():
                qs = pool["questions"]
                for i, first in enumerate(qs):
                    for second in qs[i + 1 :]:
                        a, c = _stems(first), _stems(second)
                        if a and c and len(a & c) / min(len(a), len(c)) >= 0.6:
                            duplicates += 1
            results.append(
                {
                    "posting": row["key"],
                    "industry": row["industry"],
                    "level": row["level"],
                    "status": "ok",
                    "seconds": seconds,
                    "attempts": attempts,
                    "competencies": len(served_pools),
                    "raw_questions": len(raw),
                    "raw_non_behavioural": sum(planning._is_hypothetical(q) for q in raw),
                    "raw_specific_rate": round(
                        sum(r.specific for r in raw_scored) / len(raw_scored), 3
                    )
                    if raw_scored
                    else None,
                    "served_questions": len(served),
                    "relevance": round(sum(r.specific for r in scored) / len(scored), 3)
                    if scored
                    else None,
                    "borrowed_served": sum(bool(r.borrowed) for r in scored),
                    "clarity": round(sum(r.clear for r in scored) / len(scored), 3)
                    if scored
                    else None,
                    "coverage": relevance.coverage(served, posting, embed=embed),
                    "near_duplicate_pairs": duplicates,
                    "pool_sizes": [len(p["questions"]) for p in served_pools.values()],
                    "expected_overlap_between_sessions": relevance.expected_overlap(
                        [len(p["questions"]) for p in served_pools.values()]
                    ),
                    "competency_keys": sorted(served_pools),
                }
            )
            last = results[-1]
            self.stdout.write(
                f"  {row['key']:22s} {seconds}s comps={last['competencies']} served={last['served_questions']} "
                f"relevance={last['relevance']} coverage={last['coverage']} clarity={last['clarity']} "
                f"overlap={last['expected_overlap_between_sessions']} dups={duplicates}"
            )
        ok = [r for r in results if r["status"] == "ok"]

        def mean(key):
            values = [r[key] for r in ok if r.get(key) is not None]
            return round(statistics.mean(values), 3) if values else None

        summary: dict[str, Any] = {
            "postings": len(results),
            "ok": len(ok),
            "industries": sorted({r.get("industry", "") for r in ok}),
            "mean_relevance": mean("relevance"),
            "mean_raw_specific_rate": mean("raw_specific_rate"),
            "mean_coverage": mean("coverage"),
            "mean_clarity": mean("clarity"),
            "borrowed_served_total": sum(r["borrowed_served"] for r in ok),
            "near_duplicate_pairs_total": sum(r["near_duplicate_pairs"] for r in ok),
            "mean_expected_overlap": mean("expected_overlap_between_sessions"),
            "min_pool_size": min(
                (min(r["pool_sizes"]) for r in ok if r["pool_sizes"]), default=None
            ),
            "raw_non_behavioural_total": sum(r["raw_non_behavioural"] for r in ok),
            "median_seconds": statistics.median(r["seconds"] for r in results) if results else None,
        }
        self._print("postings", summary)
        return {"summary": summary, "postings": results}

    def _print(self, name: str, summary: dict[str, Any]) -> None:
        self.stdout.write(f"\n== {name} ==")
        for key, value in summary.items():
            self.stdout.write(f"  {key}: {value}")


_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def _normalise_speech(text: str) -> list[str]:
    """Lower case, no punctuation, spelled numbers as digits.

    So "forty two thousand" and "42,000" are the same words: the benchmark
    measures recognition, not number formatting.
    """
    tokens = re.sub(
        r"[^a-z0-9' ]", " ", (text or "").lower().replace("-", " ").replace(",", "")
    ).split()
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _TENS or token in _ONES:
            value = _TENS.get(token, _ONES.index(token) if token in _ONES else 0)
            index += 1
            if token in _TENS and index < len(tokens) and tokens[index] in _ONES[1:10]:
                value += _ONES.index(tokens[index])
                index += 1
            if index < len(tokens) and tokens[index] == "thousand":
                value *= 1000
                index += 1
            out.append(str(value))
            continue
        out.append(token)
        index += 1
    return out


def _edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    row = list(range(len(hypothesis) + 1))
    for i in range(1, len(reference) + 1):
        previous, row[0] = row[0], i
        for j in range(1, len(hypothesis) + 1):
            current = min(
                row[j] + 1, row[j - 1] + 1, previous + (reference[i - 1] != hypothesis[j - 1])
            )
            previous, row[j] = row[j], current
    return row[len(hypothesis)]


# --- live summaries -----------------------------------------------------------


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)]


def _latency(results: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [c for r in results for c in r.get("call_seconds", [])]
    tokens_in = [t for r in results for t in r.get("tokens_in", []) if t]
    tokens_out = [t for r in results for t in r.get("tokens_out", []) if t]
    per_case = [r["seconds"] for r in results if r.get("model_calls")]
    return {
        "model_calls": len(calls),
        "call_seconds_median": statistics.median(calls) if calls else None,
        "call_seconds_p90": _pct(calls, 0.9),
        "case_seconds_median": statistics.median(per_case) if per_case else None,
        "tokens_in_median": statistics.median(tokens_in) if tokens_in else None,
        "tokens_out_median": statistics.median(tokens_out) if tokens_out else None,
    }


def _evaluation_summary(results: list[dict[str, Any]], *, repeat: int) -> dict[str, Any]:
    scored = [r for r in results if r.get("score") is not None]
    by_case: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_case.setdefault(r["name"], []).append(r)

    def within(r):
        return r["band"][0] <= r["score"] <= r["band"][1]

    def distance(r):
        return max(r["band"][0] - r["score"], r["score"] - r["band"][1], 0)

    flips = [name for name, rows in by_case.items() if len({row["passed"] for row in rows}) > 1]
    spreads = [
        max(row["score"] for row in rows if row.get("score") is not None)
        - min(row["score"] for row in rows if row.get("score") is not None)
        for rows in by_case.values()
        if sum(1 for row in rows if row.get("score") is not None) > 1
    ]

    def group(key):
        out: dict[str, str] = {}
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in scored:
            buckets.setdefault(str(r.get(key)), []).append(r)
        for name, rows in sorted(buckets.items()):
            out[name] = f"{sum(within(r) for r in rows)}/{len(rows)}"
        return out

    rationale_violations = sum(
        1 for r in results for f in r.get("failures", []) if f.startswith("rationale asserts")
    )
    return {
        "cases": len(by_case),
        "repeat": repeat,
        "attempts": len(results),
        "unavailable": len(results) - len(scored),
        "passed_attempts": sum(r["passed"] for r in results),
        "band_agreement": round(sum(within(r) for r in scored) / len(scored), 3)
        if scored
        else None,
        "mean_distance_outside_band": round(statistics.mean(distance(r) for r in scored), 3)
        if scored
        else None,
        "ungrounded_rate": round(
            sum(1 for r in scored if r.get("grounded") is False) / len(scored), 3
        )
        if scored
        else None,
        "unsupported_claims_in_rationale": rationale_violations,
        "cases_that_flipped_between_runs": flips,
        "max_score_spread_between_runs": max(spreads) if spreads else None,
        "cases_with_any_spread": sum(1 for spread in spreads if spread),
        "agreement_by_category": group("category"),
        "agreement_by_technical": group("technical"),
        "agreement_by_level": group("level"),
        **_latency(results),
    }


def _followup_summary(results: list[dict[str, Any]], *, repeat: int) -> dict[str, Any]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_case.setdefault(r["name"], []).append(r)
    corrections = [r for r in results if any(k in r["name"] for k in ("correction", "retraction"))]
    return {
        "cases": len(by_case),
        "repeat": repeat,
        "attempts": len(results),
        "passed_attempts": sum(r["passed"] for r in results),
        "pass_rate": round(sum(r["passed"] for r in results) / len(results), 3)
        if results
        else None,
        "correction_pass_rate": round(sum(r["passed"] for r in corrections) / len(corrections), 3)
        if corrections
        else None,
        "no_followup": sum(1 for r in results if not r["question"]),
        "cases_that_flipped_between_runs": [
            name for name, rows in by_case.items() if len({row["passed"] for row in rows}) > 1
        ],
        **_latency(results),
    }


def _judge_followup(case: dict[str, Any], question: str, run: Any) -> list[str]:
    failures: list[str] = []
    lowered = question.lower()
    if not question:
        return [f"no follow-up ({getattr(run, 'status', 'none')})"]
    if lowered.count("?") > 1:
        failures.append("more than one question")
    if len(question) > 300:
        failures.append("too long")
    if any(phrase in lowered for phrase in _PRAISE):
        failures.append("evaluates or praises the answer")
    if set(_words(question)) & _UNFAIR:
        failures.append("asks about something outside the work")
    original, new = _stems(case["question"]), _stems(question)
    if new and len(original & new) / len(new) >= 0.8 and case["expect"].get("references_any"):
        failures.append("restates the original question")
    wanted = case["expect"].get("references_any") or []
    # Stems, not substrings: "re-releasing" pulls on "release" as much as
    # "release" does.
    asked = _stems(question)
    if wanted and not any((_stems(term) & asked) or term in lowered for term in wanted):
        failures.append("does not pull on anything the candidate said")
    for term in case["expect"].get("absent", []):
        if term in lowered:
            failures.append(f"uses withdrawn or irrelevant content {term!r}")
    return failures


def _metrics(predictions: list[bool], truths: list[bool]) -> dict[str, Any]:
    tp = sum(p and t for p, t in zip(predictions, truths, strict=False))
    fp = sum(p and not t for p, t in zip(predictions, truths, strict=False))
    fn = sum((not p) and t for p, t in zip(predictions, truths, strict=False))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = (
        sum(p == t for p, t in zip(predictions, truths, strict=False)) / len(truths)
        if truths
        else 0.0
    )
    return {
        "accuracy": round(accuracy, 3),
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "n": len(truths),
    }


def _old_lexical(question: str, job: Any) -> bool:
    """The first rule, kept here only to measure it: a shared word of 4+ letters."""
    generic = {
        "about",
        "after",
        "also",
        "business",
        "client",
        "company",
        "customer",
        "data",
        "describe",
        "example",
        "experience",
        "have",
        "issue",
        "manager",
        "people",
        "problem",
        "process",
        "product",
        "project",
        "role",
        "situation",
        "system",
        "task",
        "tasks",
        "team",
        "tell",
        "that",
        "their",
        "them",
        "there",
        "these",
        "they",
        "thing",
        "things",
        "this",
        "through",
        "time",
        "walk",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "work",
        "would",
        "year",
        "years",
        "your",
        "you",
        "from",
        "were",
        "been",
        "being",
        "able",
        "must",
        "should",
        "strong",
        "good",
        "great",
        "working",
        "including",
        "within",
        "across",
    }

    def tokens(text):
        return {w.strip(".-") for w in re.findall(r"[a-z][a-z0-9+#.-]{3,}", (text or "").lower())}

    vocabulary = tokens(job.title) | tokens(job.description or "")
    for row in job.skills.all():
        vocabulary |= tokens(row.skill.name) | {row.skill.name.lower()}
    return bool(tokens(question) & (vocabulary - generic))
