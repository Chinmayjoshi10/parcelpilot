"""Provider-agnostic LLM and embedding contracts, plus the reliability shell.

Two ideas here matter more than the interfaces:

**Structured output is the primary mode.** `complete_json` takes a JSON schema
and returns parsed data. Free-text generation exists, but the answer path never
uses it: an answer is a list of claims each carrying citations, and asking for
that shape explicitly is what makes the citation validator possible at all.

**Failure is a first-class path.** Every call is wrapped in a timeout, bounded
retries with jitter, and a circuit breaker. Retrying into a dead provider
converts one outage into thread-pool exhaustion, and an un-budgeted agent loop
converts one pathological question into an unbounded bill.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agentcore.errors import BudgetExhausted, CircuitOpen, ProviderError, ProviderTimeout
from agentcore.logging import get_logger
from agentcore.types import TokenUsage

log = get_logger(__name__)


@dataclass(frozen=True)
class Completion:
    text: str
    #: Populated when the call went through `complete_json`.
    data: dict[str, Any] | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    #: True when the provider stopped for length rather than completing. A
    #: truncated JSON answer must never be treated as a valid one.
    truncated: bool = False


@runtime_checkable
class LLM(Protocol):
    """What the engine needs from a text model."""

    @property
    def routing_model(self) -> str: ...

    @property
    def synthesis_model(self) -> str: ...

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 2048,
        thinking_budget: int | None = None,
    ) -> Completion: ...

    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 1024,
        thinking_budget: int | None = None,
    ) -> Completion: ...


@runtime_checkable
class Embedder(Protocol):
    """What the engine needs from an embedding model."""

    @property
    def model(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        """Embed a batch.

        `is_query` exists because retrieval is asymmetric: a question and a
        policy clause are not the same kind of text, and providers that support
        task-typed embeddings measurably improve on treating them alike.
        """
        ...


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------


class TokenBudget:
    """A hard ceiling for one run, not an advisory counter.

    Checked *before* each call using a conservative estimate, so the budget
    cannot be blown by a single large request that only reveals its cost after
    it completes.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._spent = 0

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        return max(0, self._limit - self._spent)

    def reserve(self, estimated: int) -> None:
        if self._spent + estimated > self._limit:
            raise BudgetExhausted(
                "this question would exceed its token budget",
                limit=self._limit,
                spent=self._spent,
                requested=estimated,
            )

    def record(self, usage: TokenUsage) -> None:
        self._spent += usage.total


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Per-provider breaker with a half-open probe.

    Closed -> (N consecutive failures) -> Open -> (cooldown) -> Half-open.
    A single success in half-open closes it; a failure re-opens immediately.
    Process-local, which is the right scope: each replica discovers upstream
    health independently rather than depending on shared state to fail safely.
    """

    def __init__(self, threshold: int, cooldown_seconds: float, name: str = "llm") -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._name = name
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            # Half-open: let exactly one request through to probe.
            self._opened_at = None
            self._failures = self._threshold - 1
            log.info("circuit_half_open", provider=self._name)
            return False
        return True

    def check(self) -> None:
        if self.is_open:
            raise CircuitOpen(
                f"{self._name} is failing; requests are being shed rather than queued",
                cooldown_seconds=self._cooldown,
            )

    def record_success(self) -> None:
        if self._failures:
            log.info("circuit_closed", provider=self._name)
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            log.warning(
                "circuit_opened",
                provider=self._name,
                failures=self._failures,
                cooldown_seconds=self._cooldown,
            )


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

#: Retried. Everything else -- malformed request, auth failure, content
#: rejection -- is a bug or a policy decision, and retrying it just burns
#: latency before failing the same way.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


async def with_retries(
    call,
    *,
    attempts: int,
    breaker: CircuitBreaker,
    timeout: float,
    label: str,
):
    """Run `call` with a timeout, bounded retries and breaker accounting.

    Backoff is exponential with full jitter. Without jitter, N concurrent
    requests that fail together retry together, and the retry storm is
    indistinguishable from the outage that caused it.
    """
    breaker.check()
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            result = await asyncio.wait_for(call(), timeout=timeout)
            breaker.record_success()
            return result
        except TimeoutError:
            last = ProviderTimeout(f"{label} timed out after {timeout}s")
            breaker.record_failure()
        except ProviderError as exc:
            last = exc
            status = exc.context.get("status")
            breaker.record_failure()
            if status is not None and status not in RETRYABLE_STATUS:
                raise
        except Exception as exc:  # noqa: BLE001 - normalised into a domain error
            last = ProviderError(f"{label} failed: {exc}")
            breaker.record_failure()

        if attempt < attempts:
            delay = random.uniform(0, min(2.0**attempt * 0.25, 4.0))
            log.warning(
                "provider_retry",
                label=label,
                attempt=attempt,
                of=attempts,
                delay_seconds=round(delay, 2),
                error=str(last),
            )
            await asyncio.sleep(delay)

    assert last is not None
    raise last


def estimate_tokens(text: str) -> int:
    """Rough pre-flight estimate: ~4 characters per token, rounded up.

    Only used to reserve budget before a call. Deliberately crude and
    deliberately not a tokeniser dependency -- being approximately right one
    call early beats being exactly right one call too late.
    """
    return max(1, (len(text) + 3) // 4)
