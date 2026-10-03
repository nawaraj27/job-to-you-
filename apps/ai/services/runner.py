"""
The run pipeline (SRS §5.11, ADR-A11, ADR-A20, ADR-A28).

**This is the only way to call a model.** Every AI feature in the product —
resume analysis, bullet rewriting, cover letters, match explanations — calls
`run(task_key, inputs, ...)` and nothing else. No feature selects a provider,
implements a retry, checks a budget, validates a schema or writes an `AIRun`,
because six features doing those five things is thirty places for them to be
subtly different and one place for a bug to be fixed thirty times.

What one call does, in order:

  1. **kill switch** — a runtime flag, the one deliberate exception to A28's
     "configuration lives in code", because switching AI off during an
     incident must not require a deploy;
  2. **quota** — checked before dispatch, never after;
  3. **cache** — keyed on task, prompt version and input hash, so a prompt
     change invalidates every answer it produced;
  4. **dispatch** — down the task's fallback chain, one `AIRun` per attempt;
  5. **validate** — the prompt's own output schema;
  6. **record** — always, success or failure, with cost, latency and lineage.

Failures are classified rather than counted. A timeout is worth another
provider; a schema error is the same prompt producing the same shape and will
fail identically forever, so it stops.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from celery.exceptions import SoftTimeLimitExceeded
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from apps.common.models import EMBEDDING_DIMENSIONS
from config import ai as ai_config

from ..models import AIRun, PromptTemplate, RunStatus
from ..providers.base import (
    ProviderFailure,
    ProviderRefused,
    ProviderTimeout,
    Request,
)
from . import prompts, quotas, schema

if TYPE_CHECKING:  # pragma: no cover
    from apps.accounts.models import User
    from apps.companies.models import Company

logger = logging.getLogger(__name__)

#: The task key every embedding goes through. One key rather than one per
#: caller, because a job and a CV are embedded by the same model into the same
#: space — that is the whole point of the space — and two keys would make it
#: possible to route them differently, which would make their distances noise.
EMBED_TASK = "embed.text"


class AIDisabled(Exception):
    """The kill switch is on. Callers decide what to show instead."""


def is_enabled() -> bool:
    """The kill switch (ADR-A28).

    A runtime `FeatureFlag`, deliberately, so an incident can be stopped
    without a deploy. Everything else about provider configuration is code.
    """
    from apps.core.features import is_enabled as flag

    return flag("ai")


def is_being_erased(user: User | None) -> bool:
    """Whether this subject's data is mid-erasure (SRS §19.4).

    A thin wrapper so the runner does not import an accounts service at module
    scope, and so the one sentence that matters — *no AI work for a subject
    being erased* — has a name that says it.
    """
    from apps.accounts.services import erasure

    return erasure.is_erasing(user)


def run(
    *,
    task_key: str,
    inputs: dict[str, Any],
    user: User | None = None,
    company: Company | None = None,
    input_ref: Any = None,
    force: bool = False,
) -> AIRun:
    """Run one AI task. Always returns an `AIRun` — never raises for a model
    failure, because a failed run is a fact worth recording and a caller that
    has to catch four exception types will catch three.

    `force` skips the cache. For a person pressing "try again", which is the
    only legitimate reason to pay twice for one input.
    """
    spec = ai_config.task(task_key)
    template = prompts.current(task_key)
    digest = input_hash(task_key, template.version, inputs)

    if not is_enabled():
        return _record_failure(
            task_key=task_key,
            template=template,
            digest=digest,
            inputs=inputs,
            user=user,
            company=company,
            input_ref=input_ref,
            status=RunStatus.DISABLED,
            error="AI is switched off.",
        )

    # The erasure gate (SRS §19.4). Here rather than in each task, because this
    # is the one mandatory entry point (ADR-A10) — a check in twelve tasks is
    # eleven chances to add a thirteenth without one. A task already queued when
    # an erasure begins stops here instead of writing resume quotes back into a
    # table the erasure has already walked past.
    if is_being_erased(user):
        return _record_failure(
            task_key=task_key,
            template=template,
            digest=digest,
            inputs={},
            user=user,
            company=company,
            input_ref=input_ref,
            status=RunStatus.ERASED,
            error="This account is being erased.",
        )

    if not force and spec.cache_hours:
        cached = _cache_hit(task_key, template.version, digest, spec.cache_hours)
        if cached is not None:
            return _record_cache_hit(cached, user=user, company=company, input_ref=input_ref)

    allowed, reason = quotas.check(user=user, company=company)
    if not allowed:
        return _record_failure(
            task_key=task_key,
            template=template,
            digest=digest,
            inputs=inputs,
            user=user,
            company=company,
            input_ref=input_ref,
            status=RunStatus.QUOTA_EXCEEDED,
            error=reason,
        )

    return _dispatch(
        spec=spec,
        template=template,
        digest=digest,
        inputs=inputs,
        user=user,
        company=company,
        input_ref=input_ref,
    )


def input_hash(task_key: str, prompt_version: int, inputs: dict[str, Any]) -> str:
    """The cache key.

    Sorted and separator-normalised so two structurally identical inputs hash
    the same however the caller happened to build the dict — otherwise the
    cache misses constantly and nobody can see why.
    """
    material = json.dumps(
        {"task": task_key, "version": prompt_version, "inputs": inputs},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


# --- dispatch -------------------------------------------------------------


def _dispatch(
    *,
    spec: ai_config.TaskSpec,
    template: PromptTemplate,
    digest: str,
    inputs: dict[str, Any],
    user: User | None,
    company: Company | None,
    input_ref: Any,
) -> AIRun:
    """Walk the fallback chain until something works or nothing is left."""
    chain = ai_config.chain_for(spec.key)
    if not chain:
        return _record_failure(
            task_key=spec.key,
            template=template,
            digest=digest,
            inputs=inputs,
            user=user,
            company=company,
            input_ref=input_ref,
            status=RunStatus.PROVIDER_ERROR,
            error="No configured provider can serve this task.",
        )

    system, prompt = prompts.render(template, inputs)
    last: AIRun | None = None

    for attempt, (_name, model_spec, provider_spec) in enumerate(chain, start=1):
        if attempt > spec.retries + 1:
            break

        last = _attempt(
            spec=spec,
            template=template,
            digest=digest,
            inputs=inputs,
            user=user,
            company=company,
            input_ref=input_ref,
            model_spec=model_spec,
            provider_spec=provider_spec,
            system=system,
            prompt=prompt,
            attempt=attempt,
        )
        if last.status in RunStatus.terminal():
            # Success, a refusal, or a schema error: trying the next provider
            # would spend money to reach the same place.
            return last

    return last  # type: ignore[return-value]


def _attempt(
    *,
    spec: ai_config.TaskSpec,
    template: PromptTemplate,
    digest: str,
    inputs: dict[str, Any],
    user: User | None,
    company: Company | None,
    input_ref: Any,
    model_spec: ai_config.ModelSpec,
    provider_spec: ai_config.ProviderSpec,
    system: str,
    prompt: str,
    attempt: int,
) -> AIRun:
    """One provider, one call, one row."""
    started = time.monotonic()
    adapter = _adapter_for(provider_spec)
    request = Request(
        system=system,
        user=prompt,
        max_output_tokens=spec.max_output_tokens,
        temperature=spec.temperature,
        json_schema=template.output_schema or None,
    )

    status = RunStatus.OK
    error = ""
    output: dict[str, Any] = {}
    raw = ""
    tokens_in = tokens_out = 0

    try:
        result = adapter.complete(
            request,
            model=model_spec.name,
            timeout=spec.timeout_seconds or provider_spec.timeout_seconds,
        )
        raw, tokens_in, tokens_out = result.text, result.tokens_in, result.tokens_out

        valid, parsed, problem = schema.validate(result.text, template.output_schema)
        if valid:
            output = parsed
        else:
            status = RunStatus.SCHEMA_ERROR
            error = problem
    except ProviderTimeout as exc:
        status, error = RunStatus.TIMEOUT, str(exc)[:500]
    except ProviderRefused as exc:
        status, error = RunStatus.REFUSED, str(exc)[:500]
    except ProviderFailure as exc:
        status, error = RunStatus.PROVIDER_ERROR, str(exc)[:500]
    except SoftTimeLimitExceeded:
        # The task's time is up. Recorded as a provider error, this let a
        # scoring task carry on to its next model call until the hard limit
        # killed the worker — and a task lost that way is delivered again.
        raise
    except Exception as exc:  # noqa: BLE001 - an adapter bug is a provider error here
        logger.exception("adapter raised", extra={"task": spec.key})
        status, error = RunStatus.PROVIDER_ERROR, str(exc)[:500]

    cost = model_spec.cost_for(tokens_in, tokens_out)
    with transaction.atomic():
        run_row = AIRun.objects.create(
            task_key=spec.key,
            prompt_version=template.version,
            provider=provider_spec.key,
            model=model_spec.name,
            input_hash=digest,
            input_ref_ct=_content_type(input_ref),
            input_ref_id=str(getattr(input_ref, "pk", "") or ""),
            input_snapshot=inputs,
            output=output,
            raw_output=raw[:20_000],
            status=status,
            error=error,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt=attempt,
            user=user,
            company=company,
        )

        # Spend is recorded whatever the outcome: a failed call still cost
        # money, and a budget that only counts successes is not a budget.
        if cost:
            quotas.record(user=user, company=company, cost=cost, tokens=tokens_in + tokens_out)

        _emit(run_row)
    return run_row


def _adapter_for(provider_spec: ai_config.ProviderSpec) -> Any:
    adapter_class = import_string(provider_spec.adapter)
    return adapter_class(base_url=provider_spec.base_url, api_key=provider_spec.api_key)


# --- caching --------------------------------------------------------------


def _cache_hit(task_key: str, version: int, digest: str, cache_hours: int) -> AIRun | None:
    since = timezone.now() - dt.timedelta(hours=cache_hours)
    return AIRun.objects.fresh(
        task_key=task_key, prompt_version=version, input_hash=digest, since=since
    ).first()


def _record_cache_hit(
    source: AIRun, *, user: User | None, company: Company | None, input_ref: Any
) -> AIRun:
    """A cache hit is its own row, pointing at the answer it reused.

    Not simply returning the original: the second person's request is a fact,
    it belongs to them for attribution, and `cached_from` is what makes "how
    often does this cache actually help" answerable.
    """
    return AIRun.objects.create(
        task_key=source.task_key,
        prompt_version=source.prompt_version,
        provider=source.provider,
        model=source.model,
        input_hash=source.input_hash,
        input_ref_ct=_content_type(input_ref),
        input_ref_id=str(getattr(input_ref, "pk", "") or ""),
        input_snapshot=source.input_snapshot,
        output=source.output,
        raw_output="",
        status=RunStatus.OK,
        tokens_in=0,
        tokens_out=0,
        cost=Decimal("0"),
        latency_ms=0,
        cached_from=source,
        user=user,
        company=company,
    )


def _record_failure(
    *,
    task_key: str,
    template: PromptTemplate | None,
    digest: str,
    inputs: dict[str, Any],
    user: User | None,
    company: Company | None,
    input_ref: Any,
    status: str,
    error: str,
) -> AIRun:
    """A refusal before dispatch is still a run.

    Recorded so "the AI did nothing" has an answer: which task, for whom, and
    why — a switched-off flag and an exhausted budget look identical from the
    screen and must not look identical here.
    """
    with transaction.atomic():
        run_row = AIRun.objects.create(
            task_key=task_key,
            prompt_version=template.version if template is not None else 0,
            provider="",
            model="",
            input_hash=digest,
            input_ref_ct=_content_type(input_ref),
            input_ref_id=str(getattr(input_ref, "pk", "") or ""),
            input_snapshot=inputs,
            status=status,
            error=error[:500],
            user=user,
            company=company,
        )
        _emit(run_row)
    return run_row


def _content_type(obj: Any) -> Any:
    if obj is None:
        return None
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(obj)


def _emit(run_row: AIRun) -> None:
    """Announce the run. Cost dashboards and analytics subscribe; nothing in
    the pipeline waits for them.

    Called from inside the same `atomic()` block that wrote the row, which is
    what `events.emit` requires — and requiring every *caller* of `run()` to
    open one instead was a contract nobody could see. Four features had already
    missed it: drafting a cover letter, explaining a match, drafting a fix from
    a finding and asking an interview follow-up each raised
    "events.emit('ai.run_completed') was called outside a transaction" and
    returned a 500 to somebody who had pressed a button. The state change an
    `ai.run_completed` event describes is this row, so this is where the
    transaction belongs.
    """
    from apps.notifications import events

    events.emit(
        "ai.run_completed",
        run_row,
        payload={
            "run_public_id": str(run_row.public_id),
            "task_key": run_row.task_key,
            "status": run_row.status,
            "cost": str(run_row.cost),
        },
    )


# --- embeddings (P13) -----------------------------------------------------


@dataclass
class Embedding:
    """One embedding attempt: the vector if there is one, and the run either way.

    `vector` is `None` when the call failed — switched off, out of budget, no
    configured provider, or every provider in the chain down. Callers check it
    and carry on: an unembedded row is fully matchable, because the score is
    deterministic and vectors only affect retrieval order (ADR-A6, ADR-A24).
    """

    vector: list[float] | None
    run: AIRun

    @property
    def ok(self) -> bool:
        return self.vector is not None

    @property
    def model(self) -> str:
        return self.run.model


def embed(
    *,
    text: str,
    user: User | None = None,
    company: Company | None = None,
    input_ref: Any = None,
    task_key: str = EMBED_TASK,
) -> Embedding:
    """Turn text into a vector, through the same pipeline as everything else.

    Kill switch, budget, provider chain, retry classification, cost accounting
    and the `AIRun` record are the ones already here — an embedding is a
    different *call*, not a different *pipeline*, and a second orchestrator is
    exactly what P11 was built to prevent.

    Three deliberate differences from `run`:

      * **No prompt.** There is no template, no schema and no `prompt_version`:
        the text being embedded is the input, not an instruction wrapped around
        one.
      * **No cache lookup.** The vector is stored on the row that needed it, so
        that column *is* the cache. A cached `AIRun` could not return the vector
        anyway, because keeping 768 floats on every run row would make the
        cheapest call in the system the largest thing in the table.
      * **Never raises.** Same contract as `run`: a failure is a status.
    """
    spec = ai_config.task(task_key)
    digest = input_hash(task_key, 0, {"text": text})
    # Length, not content. An embedding input is a whole CV or a whole job
    # description, and `AIRun.input_snapshot` inherits the retention of the
    # record it came from — storing a second copy of every posting here would
    # be a retention problem bought for nothing (ADR-A11).
    snapshot = {"chars": len(text)}

    if not is_enabled():
        return Embedding(
            vector=None,
            run=_record_failure(
                task_key=task_key,
                template=None,
                digest=digest,
                inputs=snapshot,
                user=user,
                company=company,
                input_ref=input_ref,
                status=RunStatus.DISABLED,
                error="AI is switched off.",
            ),
        )

    if is_being_erased(user):
        return Embedding(
            vector=None,
            run=_record_failure(
                task_key=task_key,
                template=None,
                digest=digest,
                inputs={},
                user=user,
                company=company,
                input_ref=input_ref,
                status=RunStatus.ERASED,
                error="This account is being erased.",
            ),
        )

    allowed, reason = quotas.check(user=user, company=company)
    if not allowed:
        return Embedding(
            vector=None,
            run=_record_failure(
                task_key=task_key,
                template=None,
                digest=digest,
                inputs=snapshot,
                user=user,
                company=company,
                input_ref=input_ref,
                status=RunStatus.QUOTA_EXCEEDED,
                error=reason,
            ),
        )

    return _dispatch_embedding(
        spec=spec,
        text=text,
        digest=digest,
        snapshot=snapshot,
        user=user,
        company=company,
        input_ref=input_ref,
    )


def _dispatch_embedding(
    *,
    spec: ai_config.TaskSpec,
    text: str,
    digest: str,
    snapshot: dict[str, Any],
    user: User | None,
    company: Company | None,
    input_ref: Any,
) -> Embedding:
    """Walk the chain, skipping providers that cannot embed.

    A completion provider in an embedding task's chain is a configuration
    mistake, and skipping it here turns that mistake into "the next provider
    served it" rather than a 404 dressed up as an outage.
    """
    chain = [
        entry for entry in ai_config.chain_for(spec.key) if hasattr(_adapter_for(entry[2]), "embed")
    ]
    if not chain:
        return Embedding(
            vector=None,
            run=_record_failure(
                task_key=spec.key,
                template=None,
                digest=digest,
                inputs=snapshot,
                user=user,
                company=company,
                input_ref=input_ref,
                status=RunStatus.PROVIDER_ERROR,
                error="No configured provider can produce embeddings.",
            ),
        )

    last: Embedding | None = None
    for attempt, (_name, model_spec, provider_spec) in enumerate(chain, start=1):
        if attempt > spec.retries + 1:
            break

        last = _attempt_embedding(
            spec=spec,
            text=text,
            digest=digest,
            snapshot=snapshot,
            user=user,
            company=company,
            input_ref=input_ref,
            model_spec=model_spec,
            provider_spec=provider_spec,
            attempt=attempt,
        )
        if last.run.status in RunStatus.terminal():
            return last

    return last  # type: ignore[return-value]


def _attempt_embedding(
    *,
    spec: ai_config.TaskSpec,
    text: str,
    digest: str,
    snapshot: dict[str, Any],
    user: User | None,
    company: Company | None,
    input_ref: Any,
    model_spec: ai_config.ModelSpec,
    provider_spec: ai_config.ProviderSpec,
    attempt: int,
) -> Embedding:
    """One provider, one vector, one row."""
    started = time.monotonic()
    adapter = _adapter_for(provider_spec)

    status = RunStatus.OK
    error = ""
    vector: list[float] | None = None
    tokens_in = 0

    try:
        result = adapter.embed(text, model=model_spec.name, timeout=provider_spec.timeout_seconds)
        tokens_in = result.tokens_in
        if len(result.vector) != EMBEDDING_DIMENSIONS:
            # A vector of the wrong width cannot be stored, and must never be
            # padded or truncated to fit: the result would be a plausible
            # answer to a different question, and nothing would error (A27).
            status = RunStatus.SCHEMA_ERROR
            error = (
                f"Expected {EMBEDDING_DIMENSIONS} dimensions from "
                f"{model_spec.name}, got {len(result.vector)}."
            )
        else:
            vector = result.vector
    except ProviderTimeout as exc:
        status, error = RunStatus.TIMEOUT, str(exc)[:500]
    except ProviderRefused as exc:
        status, error = RunStatus.REFUSED, str(exc)[:500]
    except ProviderFailure as exc:
        status, error = RunStatus.PROVIDER_ERROR, str(exc)[:500]
    except SoftTimeLimitExceeded:
        # The task's time is up. Recorded as a provider error, this let a
        # scoring task carry on to its next model call until the hard limit
        # killed the worker — and a task lost that way is delivered again.
        raise
    except Exception as exc:  # noqa: BLE001 - an adapter bug is a provider error here
        logger.exception("embedding adapter raised", extra={"task": spec.key})
        status, error = RunStatus.PROVIDER_ERROR, str(exc)[:500]

    cost = model_spec.cost_for(tokens_in, 0)
    # One transaction for the row and its event, as `run()` does (EMB-01).
    # Without it every embedding outside a test raised "events.emit(...) was
    # called outside a transaction": the `embed_job` and `embed_profile`
    # tasks crashed on every run, so no vector was ever stored, and the test
    # suite could not see it because `TestCase` is itself a transaction.
    with transaction.atomic():
        run_row = AIRun.objects.create(
            task_key=spec.key,
            prompt_version=0,
            provider=provider_spec.key,
            model=model_spec.name,
            input_hash=digest,
            input_ref_ct=_content_type(input_ref),
            input_ref_id=str(getattr(input_ref, "pk", "") or ""),
            input_snapshot=snapshot,
            output={"dimensions": len(vector)} if vector is not None else {},
            raw_output="",
            status=status,
            error=error,
            tokens_in=tokens_in,
            cost=cost,
            latency_ms=int((time.monotonic() - started) * 1000),
            attempt=attempt,
            user=user,
            company=company,
        )
        if cost:
            quotas.record(user=user, company=company, cost=cost, tokens=tokens_in)

        _emit(run_row)
    return Embedding(vector=vector, run=run_row)
