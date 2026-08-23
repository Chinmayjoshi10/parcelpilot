"""Gemini API provider (generativelanguage.googleapis.com), over REST.

Talking to the endpoint directly with httpx rather than adding the Google SDK:
one fewer heavy dependency, and the reliability shell in `base.py` needs to see
real HTTP status codes to decide what is retryable.

`build_payload`, `parse_completion` and `extract_json` are module-level because
the Vertex AI provider shares them verbatim -- the `generateContent` request and
response shapes are identical across the two APIs; only transport, auth and the
embedding call differ.

Three provider-specific details that are easy to get wrong and expensive to get
wrong silently:

1. **Thinking budget.** The 2.5 models spend output tokens on internal
   reasoning. With a small `maxOutputTokens` the call can return
   `finishReason: MAX_TOKENS` and an EMPTY text part -- a truncated answer that
   looks like a successful one. Routing calls disable thinking; synthesis calls
   get headroom and an explicit truncation check.

2. **Task-typed embeddings.** A question and a policy clause are different
   kinds of text. `RETRIEVAL_QUERY` vs `RETRIEVAL_DOCUMENT` is free accuracy,
   and skipping it is the most common reason a hybrid index underperforms.

3. **Truncated-dimension normalisation.** Gemini embeddings are unit-normalised
   at full width. Ask for fewer dimensions and they are no longer normalised,
   so cosine similarity silently stops being cosine similarity. We re-normalise
   client-side, always.
"""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from agentcore.errors import ConfigError, ProviderError
from agentcore.llm.base import CircuitBreaker, Completion, with_retries
from agentcore.logging import get_logger
from agentcore.settings import Settings
from agentcore.types import TokenUsage

log = get_logger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

#: JSON Schema keywords the Gemini schema dialect (an OpenAPI 3 subset) does
#: not accept. Passing them through is a 400, so they are stripped rather than
#: silently breaking every structured call.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "$schema",
        "$defs",
        "definitions",
        "patternProperties",
        "allOf",
        "oneOf",
        "not",
        "const",
        "examples",
        "default",
        "exclusiveMinimum",
        "exclusiveMaximum",
    }
)


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate a JSON Schema fragment into Gemini's dialect.

    Types are upper-cased (the dialect's canonical form) and unsupported
    keywords dropped. `propertyOrdering` is set from the declared property
    order: the model emits fields in the order given, and a stable order makes
    responses easier to diff when debugging a bad answer.
    """
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(value, str):
            out["type"] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: to_gemini_schema(v) for k, v in value.items()}
            out.setdefault("propertyOrdering", list(value))
        elif key == "items":
            out["items"] = to_gemini_schema(value)
        elif isinstance(value, dict):
            out[key] = to_gemini_schema(value)
        else:
            out[key] = value
    return out


def build_payload(
    *,
    system: str,
    user: str,
    temperature: float,
    max_output_tokens: int,
    model: str,
    response_schema: dict[str, Any] | None,
    thinking_budget: int | None = None,
) -> dict[str, Any]:
    """Build a generateContent request body. Shared with the Vertex provider.

    `thinking_budget=0` disables internal reasoning. The CALLER decides, because
    inferring it here got it wrong: the rule was "disable thinking unless the
    call is structured", but routing IS a structured call and has the smallest
    token ceiling in the system. So thinking stayed enabled on exactly the wrong
    call, the model spent all 1024 tokens reasoning, returned MAX_TOKENS with an
    empty text part, and every question failed as a truncation error that looked
    like a provider outage.

    Whether to think is a property of the task -- classification versus
    reasoning -- and only the caller knows which it is issuing.
    """
    generation: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if response_schema is not None:
        generation["responseMimeType"] = "application/json"
        generation["responseSchema"] = response_schema

    if thinking_budget is not None:
        generation["thinkingConfig"] = {"thinkingBudget": thinking_budget}

    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": generation,
    }


def parse_completion(raw: dict[str, Any], model: str) -> Completion:
    """Parse a generateContent response. Shared with the Vertex provider."""
    usage_meta = raw.get("usageMetadata", {})
    usage = TokenUsage(
        prompt_tokens=usage_meta.get("promptTokenCount", 0),
        # Thinking tokens are billed as output but are not included in
        # candidatesTokenCount, so they are added explicitly -- otherwise the
        # budget under-counts exactly the calls that cost most.
        completion_tokens=usage_meta.get("candidatesTokenCount", 0)
        + usage_meta.get("thoughtsTokenCount", 0),
    )

    candidates = raw.get("candidates") or []
    if not candidates:
        # A prompt blocked by safety filters lands here.
        reason = raw.get("promptFeedback", {}).get("blockReason", "unknown")
        raise ProviderError(f"model returned no candidates (reason: {reason})", model=model)

    candidate = candidates[0]
    finish = candidate.get("finishReason", "")
    parts = candidate.get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)

    # MAX_TOKENS with empty text is the thinking-budget trap: the model spent
    # its whole allowance thinking and emitted nothing.
    return Completion(
        text=text,
        usage=usage,
        model=model,
        truncated=finish == "MAX_TOKENS" or bool(finish and not text),
    )


def extract_json(completion: Completion, model: str) -> dict[str, Any]:
    """Parse structured output, refusing anything incomplete.

    A response that fails to parse is raised, never patched up: an answer whose
    shape we had to guess at cannot then be citation-validated.
    """
    if completion.truncated:
        raise ProviderError(
            "model output was truncated before the JSON was complete; raise "
            "max_output_tokens or reduce the retrieved context",
            model=model,
        )
    try:
        return json.loads(completion.text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"structured output was not valid JSON: {exc}", model=model
        ) from exc


def l2_normalise(values: list[float]) -> list[float]:
    """Unit-length the vector so a dot product equals cosine similarity.

    Required whenever `outputDimensionality` is below the model's native width:
    truncated embeddings come back un-normalised, and the retrieval query
    computes cosine as a dot product.
    """
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        raise ProviderError("embedding has zero magnitude and cannot be normalised")
    return [v / norm for v in values]


def _model_path(model: str) -> str:
    return model if model.startswith("models/") else f"models/{model}"


class GeminiClient:
    """Shared HTTP plumbing: auth, error normalisation, breaker."""

    def __init__(self, settings: Settings, breaker_name: str) -> None:
        self._settings = settings
        self._key = settings.require_llm_key()
        self._breaker = CircuitBreaker(
            settings.llm_breaker_threshold,
            settings.llm_breaker_cooldown_seconds,
            name=breaker_name,
        )
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
            # Header rather than ?key=, so the credential never appears in a URL
            # that could reach an access log or an exception message.
            headers={"x-goog-api-key": self._key, "content-type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post(self, path: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            response = await self._client.post(path, json=payload)
            if response.status_code >= 400:
                # Surface the provider's own message: its 400s are specific
                # ("schema field X not supported") and worth not burying.
                raise ProviderError(
                    f"{label} returned HTTP {response.status_code}: {response.text[:600]}",
                    status=response.status_code,
                )
            return response.json()

        return await with_retries(
            call,
            attempts=self._settings.llm_max_retries + 1,
            breaker=self._breaker,
            timeout=self._settings.llm_timeout_seconds,
            label=label,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        """Enumerate what this key can actually reach.

        Used by `parcelpilot llm probe`. Model availability varies by key and
        region, so the supported set is discovered rather than assumed.
        """

        async def call() -> dict[str, Any]:
            response = await self._client.get("/models", params={"pageSize": 200})
            if response.status_code >= 400:
                raise ProviderError(
                    f"list_models returned HTTP {response.status_code}: {response.text[:400]}",
                    status=response.status_code,
                )
            return response.json()

        data = await with_retries(
            call,
            attempts=self._settings.llm_max_retries + 1,
            breaker=self._breaker,
            timeout=self._settings.llm_timeout_seconds,
            label="list_models",
        )
        return data.get("models", [])


class GeminiLLM:
    """Text and structured generation via the Gemini API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = GeminiClient(settings, breaker_name="gemini-llm")

    @property
    def routing_model(self) -> str:
        return self._settings.llm_routing_model

    @property
    def synthesis_model(self) -> str:
        return self._settings.llm_synthesis_model

    async def aclose(self) -> None:
        await self._client.aclose()

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
    ) -> Completion:
        """Constrained generation. The engine's only sanctioned answer path."""
        target = model or self.synthesis_model
        raw = await self._client.post(
            f"/{_model_path(target)}:generateContent",
            build_payload(
                system=system,
                user=user,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                model=target,
                response_schema=to_gemini_schema(schema),
                thinking_budget=thinking_budget,
            ),
            "generateContent",
        )
        completion = parse_completion(raw, target)
        return Completion(
            text=completion.text,
            data=extract_json(completion, target),
            usage=completion.usage,
            model=target,
        )

    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 1024,
        thinking_budget: int | None = None,
    ) -> Completion:
        target = model or self.routing_model
        raw = await self._client.post(
            f"/{_model_path(target)}:generateContent",
            build_payload(
                system=system,
                user=user,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                model=target,
                response_schema=None,
                thinking_budget=thinking_budget,
            ),
            "generateContent",
        )
        return parse_completion(raw, target)


class GeminiEmbedder:
    """Batched, task-typed, re-normalised embeddings via the Gemini API."""

    #: The API caps requests per batchEmbedContents call.
    MAX_BATCH = 100

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = GeminiClient(settings, breaker_name="gemini-embed")
        self._dimension = settings.embedding_dim

    @property
    def model(self) -> str:
        return self._settings.embedding_model

    @property
    def dimension(self) -> int:
        return self._dimension

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        if not texts:
            return []

        task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        batch_size = min(self._settings.embedding_batch_size, self.MAX_BATCH)
        vectors: list[list[float]] = []

        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            raw = await self._client.post(
                f"/{_model_path(self.model)}:batchEmbedContents",
                {
                    "requests": [
                        {
                            "model": _model_path(self.model),
                            "content": {"parts": [{"text": text}]},
                            "taskType": task_type,
                            "outputDimensionality": self._dimension,
                        }
                        for text in batch
                    ]
                },
                "batchEmbedContents",
            )
            embeddings = raw.get("embeddings") or []
            if len(embeddings) != len(batch):
                raise ProviderError(
                    "embedding count does not match the batch size; refusing to "
                    "align vectors to the wrong chunks",
                    requested=len(batch),
                    returned=len(embeddings),
                )
            for item in embeddings:
                values = item.get("values") or []
                if len(values) != self._dimension:
                    # A dimension mismatch corrupts every cosine score in the
                    # index, and does so quietly. Fail the ingest instead.
                    raise ProviderError(
                        "embedding dimension does not match EMBEDDING_DIM",
                        expected=self._dimension,
                        actual=len(values),
                        hint="run `parcelpilot llm probe` and set EMBEDDING_DIM to match",
                    )
                vectors.append(l2_normalise(values))

        log.info(
            "embedded",
            count=len(vectors),
            model=self.model,
            task_type=task_type,
            dimension=self._dimension,
        )
        return vectors

    async def probe(self) -> dict[str, Any]:
        """Measure the true dimension by embedding one token."""
        raw = await self._client.post(
            f"/{_model_path(self.model)}:embedContent",
            {
                "content": {"parts": [{"text": "probe"}]},
                "taskType": "RETRIEVAL_DOCUMENT",
                "outputDimensionality": self._dimension,
            },
            "embedContent",
        )
        values = raw.get("embedding", {}).get("values") or []
        return {
            "model": self.model,
            "requested_dimension": self._dimension,
            "actual_dimension": len(values),
            "matches": len(values) == self._dimension,
        }


def build(settings: Settings) -> tuple[GeminiLLM, GeminiEmbedder | None]:
    if settings.llm_provider != "gemini":
        raise ConfigError(f"gemini builder called for provider {settings.llm_provider}")
    llm = GeminiLLM(settings)
    embedder = GeminiEmbedder(settings) if settings.embedding_backend == "api" else None
    return llm, embedder
