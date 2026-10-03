"""
AI provider, model and routing configuration (ADR-A28).

**This is code, not data.** v1.0 had `AIProvider`, `AIModel` and `AITaskConfig`
tables — three tables, an admin surface and a cache, for perhaps six rows,
bought to enable runtime provider swapping that nobody performs at 2 a.m.
without a deploy anyway. Configuration that changes model behaviour deserves
review, a diff and a rollback; only the record of what actually ran needs to be
queryable, and that is `AIRun`.

The one deliberate exception is the **kill switch**, which is a runtime
`FeatureFlag`: disabling AI during an incident must not require a deploy.

Routing is per task, with a fallback chain. A task names the tier it needs, not
a model, so changing which model serves "cheap" is one line here rather than a
search through the codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelSpec:
    """One model on one provider, with what it costs to use.

    Prices are per million tokens, as every provider quotes them. Stored as
    `Decimal` because cost attribution is money and floating point money is a
    bug that shows up in an invoice.
    """

    provider: str
    name: str
    input_cost_per_million: Decimal
    output_cost_per_million: Decimal
    context_window: int = 128_000
    #: Whether the model reliably honours a JSON schema instruction. A model
    #: that does not needs the repair path, which costs a second call.
    supports_json_mode: bool = True

    def cost_for(self, tokens_in: int, tokens_out: int) -> Decimal:
        million = Decimal(1_000_000)
        return (
            Decimal(tokens_in) / million * self.input_cost_per_million
            + Decimal(tokens_out) / million * self.output_cost_per_million
        ).quantize(Decimal("0.000001"))


@dataclass(frozen=True)
class ProviderSpec:
    """How to reach one provider."""

    key: str
    adapter: str
    base_url: str
    api_key_env: str = ""
    timeout_seconds: int = 60

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""

    @property
    def is_configured(self) -> bool:
        """A provider with no key is not a provider, it is a 500 waiting to
        happen. The router skips it rather than trying it."""
        return not self.api_key_env or bool(self.api_key)


@dataclass(frozen=True)
class TaskSpec:
    """What one AI task needs.

    `models` is a fallback chain, tried in order. The chain is per task rather
    than global because "summarise a CV" and "extract structured skills" fail
    differently and deserve different second choices.
    """

    key: str
    models: tuple[str, ...]
    #: Cache lifetime. Zero means never cache — right for anything whose output
    #: should differ between two identical requests.
    cache_hours: int = 24 * 7
    max_output_tokens: int = 2_000
    temperature: float = 0.2
    #: Refuse to start a run that would cost more than this. A ceiling per run,
    #: not per day: the daily figure is a quota and lives in the database.
    max_cost: Decimal = Decimal("0.05")
    retries: int = 2
    #: Overrides the provider's timeout for this task. For work that runs off
    #: the request and is larger than the provider default was sized for.
    timeout_seconds: int | None = None
    description: str = ""


# --- providers ------------------------------------------------------------

PROVIDERS: dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        key="openrouter",
        adapter="apps.ai.providers.openrouter.OpenRouterAdapter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        timeout_seconds=60,
    ),
    # Local. No key, no cost, no data leaving the machine — which is why it is
    # the one a developer gets by default and the one an evaluation run uses.
    "ollama": ProviderSpec(
        key="ollama",
        adapter="apps.ai.providers.ollama.OllamaAdapter",
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        timeout_seconds=120,
    ),
}


# --- models ---------------------------------------------------------------

MODELS: dict[str, ModelSpec] = {
    "cheap": ModelSpec(
        provider="openrouter",
        name="openai/gpt-4o-mini",
        input_cost_per_million=Decimal("0.15"),
        output_cost_per_million=Decimal("0.60"),
    ),
    "capable": ModelSpec(
        provider="openrouter",
        name="anthropic/claude-sonnet-4",
        input_cost_per_million=Decimal("3.00"),
        output_cost_per_million=Decimal("15.00"),
        context_window=200_000,
    ),
    "local": ModelSpec(
        provider="ollama",
        name=os.environ.get("OLLAMA_MODEL", "llama3.1:8b"),
        input_cost_per_million=Decimal("0"),
        output_cost_per_million=Decimal("0"),
        context_window=32_000,
        supports_json_mode=False,
    ),
    # --- embeddings (P13) --------------------------------------------------
    #
    # Both produce 768 dimensions, which is why `EMBEDDING_DIMENSIONS` can be a
    # constant rather than a per-model column. A model that produced a different
    # width would need its own column and a backfill, and the runner refuses a
    # vector of the wrong size rather than storing a truncated one (ADR-A27).
    "embed-hosted": ModelSpec(
        provider="openrouter",
        name="openai/text-embedding-3-small",
        input_cost_per_million=Decimal("0.02"),
        output_cost_per_million=Decimal("0"),
    ),
    "embed-local": ModelSpec(
        provider="ollama",
        name=os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        input_cost_per_million=Decimal("0"),
        output_cost_per_million=Decimal("0"),
        supports_json_mode=False,
    ),
}


# --- tasks ----------------------------------------------------------------
#
# Every AI feature in the product routes through one of these keys. A feature
# that wants a model rather than a task key is a feature that has started
# making provider decisions of its own, which is what A28 exists to prevent.

TASKS: dict[str, TaskSpec] = {
    "resume.analysis": TaskSpec(
        key="resume.analysis",
        models=("capable", "cheap", "local"),
        cache_hours=24 * 30,
        # Measured answers are 350-400 tokens (CV-Analysis-Timing.md). The cap
        # bounds a runaway generation, which on a CPU at ~4 tokens a second
        # was the difference between a slow review and a timed-out one.
        max_output_tokens=1_200,
        max_cost=Decimal("0.10"),
        # Always off the request (`defer_ai`): the deterministic report is on
        # screen already. 120 s timed out every CV longer than a page on a
        # CPU-only local model; the review is worth waiting for in the background.
        timeout_seconds=300,
        description="Structured findings on one CV version.",
    ),
    "resume.bullet_rewrite": TaskSpec(
        key="resume.bullet_rewrite",
        models=("cheap", "local"),
        cache_hours=24 * 7,
        max_output_tokens=800,
        description="Tighter wording for one bullet, in the candidate's voice.",
    ),
    "resume.summary": TaskSpec(
        key="resume.summary",
        models=("cheap", "local"),
        max_output_tokens=600,
        description="A professional summary from the rest of the CV.",
    ),
    "skills.extract": TaskSpec(
        key="skills.extract",
        models=("cheap", "local"),
        cache_hours=24 * 30,
        max_output_tokens=1_200,
        temperature=0.0,
        description="Skill terms from free text, for the taxonomy resolver.",
    ),
    "cover_letter.draft": TaskSpec(
        key="cover_letter.draft",
        # `local` is last and is a genuine fallback, not a preference: without
        # it this chain is empty on any deployment that has no OpenRouter key,
        # and "Draft with AI" answers "No configured provider can serve this
        # task" — a dead button rather than a degraded one.
        models=("capable", "cheap", "local"),
        # Never cached: two people asking for a letter for the same job should
        # not receive the same letter.
        cache_hours=0,
        max_output_tokens=1_500,
        temperature=0.6,
        max_cost=Decimal("0.08"),
        description="A first draft of a cover letter for one application.",
    ),
    "resume.section_draft": TaskSpec(
        key="resume.section_draft",
        models=("capable", "cheap", "local"),
        # Never cached: it is a conversation. Two turns that happen to contain
        # the same sentence are still two different moments, and returning the
        # earlier answer to the later one would make the assistant appear to
        # have stopped listening.
        cache_hours=0,
        max_output_tokens=900,
        temperature=0.4,
        max_cost=Decimal("0.05"),
        description="Turn what a candidate said about their work into CV bullets (P14).",
    ),
    "match.explain": TaskSpec(
        key="match.explain",
        models=("cheap", "local"),
        cache_hours=24 * 7,
        max_output_tokens=500,
        description="Why this candidate and this job were scored as they were.",
    ),
    # --- interviews (P16) --------------------------------------------------
    #
    # Three keys, not one. A plan, a follow-up and an evaluation are different
    # calls with different budgets and different fallback chains: a plan runs
    # once per session and can afford a capable model, a follow-up runs inside a
    # loop a candidate is waiting on, and an evaluation runs per competency and
    # is the one whose quality decides whether any of this predicts anything.
    "interview.plan": TaskSpec(
        key="interview.plan",
        models=("capable", "cheap", "local"),
        # Cached per (job, competencies) so two candidates interviewing for one
        # role do not each pay to plan it. The *questions* are sampled from the
        # plan per session, which is what keeps them rotated (FR-10.3).
        cache_hours=24 * 7,
        max_output_tokens=1_500,
        max_cost=Decimal("0.06"),
        # Runs on the `ai` queue, never on a request. On the local 7B model a
        # kit takes 65-125s (measured: 86s ok, two 120s timeouts in a row), so
        # the provider's 120s default killed most of them and every practice
        # interview quietly fell back to the general questions.
        timeout_seconds=300,
        description="A competency plan and question pool for one job's interview.",
    ),
    "interview.followup": TaskSpec(
        key="interview.followup",
        models=("cheap", "local"),
        # Never cached: a follow-up references the candidate's own prior answer,
        # so two candidates cannot share one, and a cache hit would ask somebody
        # about an answer they did not give.
        cache_hours=0,
        max_output_tokens=120,
        temperature=0.4,
        max_cost=Decimal("0.02"),
        description="One bounded follow-up question about what the candidate just said.",
    ),
    "interview.evaluate": TaskSpec(
        key="interview.evaluate",
        # Same reasoning as the cover letter: a local model scores a competency
        # less well than a capable one, and far better than an interview that
        # ends with no scorecard at all.
        models=("capable", "cheap", "local"),
        cache_hours=0,
        # Room for a judgement and a quotation per answer as well as the
        # rationale (2026-09-25); 500 was sized for the rationale alone.
        max_output_tokens=800,
        temperature=0.1,
        max_cost=Decimal("0.08"),
        description="Score one competency against the rubric, with the reasoning.",
    ),
    "embed.text": TaskSpec(
        key="embed.text",
        # Local first, and deliberately: embedding is the one AI workload whose
        # volume follows the size of the corpus rather than what people click,
        # so it is the one where per-token pricing compounds into a bill nobody
        # decided to spend (§9.6). Hosted is the fallback, not the default.
        models=("embed-local", "embed-hosted"),
        # The vector lives on the row that needed it, so that column is the
        # cache. A run-level cache would store nothing useful.
        cache_hours=0,
        max_output_tokens=0,
        max_cost=Decimal("0.01"),
        description="One vector for one piece of text, for retrieval (P13).",
    ),
}


# --- global limits --------------------------------------------------------

#: Where prompt files live, relative to the repository root (ADR-A20).
PROMPT_DIR = "prompts"

#: Default monthly ceiling per user, in the base currency. A quota, not a rate
#: limit: it is about money rather than about load.
DEFAULT_USER_MONTHLY_COST: Decimal = Decimal("2.00")
DEFAULT_COMPANY_MONTHLY_COST: Decimal = Decimal("50.00")


def task(key: str) -> TaskSpec:
    from apps.common.errors import InvalidState

    spec = TASKS.get(key)
    if spec is None:
        raise InvalidState(f"{key!r} is not a configured AI task.")
    return spec


def model(name: str) -> ModelSpec:
    from apps.common.errors import InvalidState

    spec = MODELS.get(name)
    if spec is None:
        raise InvalidState(f"{name!r} is not a configured model.")
    return spec


def provider(key: str) -> ProviderSpec:
    from apps.common.errors import InvalidState

    spec = PROVIDERS.get(key)
    if spec is None:
        raise InvalidState(f"{key!r} is not a configured provider.")
    return spec


def chain_for(task_key: str) -> list[tuple[str, ModelSpec, ProviderSpec]]:
    """The models to try for one task, in order, skipping any that cannot run.

    A provider with no API key is skipped rather than attempted: trying it
    produces a 401 that looks like an outage in the logs and wastes a retry
    slot that a working provider could have used.
    """
    chain = []
    for name in task(task_key).models:
        model_spec = MODELS.get(name)
        if model_spec is None:
            continue
        provider_spec = PROVIDERS.get(model_spec.provider)
        if provider_spec is None or not provider_spec.is_configured:
            continue
        chain.append((name, model_spec, provider_spec))
    return chain
