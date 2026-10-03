"""
The provider seam (ADR-A28).

One protocol, three implementations, and **no feature ever touches any of
them**. Everything in the product calls `ai.services.runner.run(task_key, ...)`;
the runner picks a model from the routing table and the adapter turns one
request into one result.

Adding a provider is one file plus a settings entry. Provider-specific
behaviour — how JSON mode is requested, where the token counts live in the
response, what an error looks like — is handled inside the adapter and never
leaks past this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Request:
    """One completion, described in terms no provider owns."""

    system: str
    user: str
    max_output_tokens: int = 2_000
    temperature: float = 0.2
    #: When set, the adapter asks the provider for JSON matching this schema —
    #: by whatever mechanism that provider offers, or by instruction if it
    #: offers none.
    json_schema: dict[str, Any] | None = None


@dataclass
class Result:
    """What came back, normalised."""

    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmbeddingResult:
    """One vector, and what it cost.

    Its own type rather than a `Result` with a vector bolted on: an embedding
    has no text, no finish reason and no output tokens, and a shared shape whose
    fields are meaningless half the time is how a caller ends up reading
    `result.text` on an embedding and getting `""`.
    """

    vector: list[float]
    tokens_in: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class ProviderTimeout(Exception):
    """The provider did not answer in time. Retryable, ideally elsewhere."""


class ProviderFailure(Exception):
    """The provider answered with a failure. Retryable."""


class ProviderRefused(Exception):
    """The model declined to answer.

    Distinct from a failure and deliberately **not** retryable: a refusal is a
    decision about the content, and asking again produces the same decision
    while spending money to do it.
    """


class Adapter(Protocol):
    """What every provider must be able to do."""

    key: str

    def complete(self, request: Request, *, model: str, timeout: int) -> Result:
        """Run one completion, or raise one of the three exceptions above."""
        ...


class EmbeddingAdapter(Protocol):
    """A provider that can also turn text into a vector (P13).

    A **separate** protocol rather than a second method on `Adapter`, because
    not every provider offers embeddings and a required method that half the
    implementations raise `NotImplementedError` from is a protocol that has
    stopped describing anything. The runner checks for this capability before
    routing an embedding task, and a model whose provider lacks it is a
    configuration error caught at dispatch rather than a 404 at runtime.

    The same three exceptions apply, for the same reason: the runner's retry
    classification is the only classification, and it does not care which kind
    of call produced the failure.
    """

    key: str

    def embed(self, text: str, *, model: str, timeout: int) -> EmbeddingResult:
        """Embed one string, or raise one of the three exceptions above."""
        ...
