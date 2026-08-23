"""Vertex AI provider (aiplatform.googleapis.com).

Vertex is a different API surface from the Gemini API, and the differences that
matter are all in this file:

* **Endpoint.** Project- and region-scoped:
  `{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{region}/publishers/google/models/{model}:generateContent`.
  The `global` region drops the host prefix.
* **Auth.** Vertex normally uses OAuth bearer tokens from a service account,
  not API keys. Express mode does accept an API key. Both are supported here
  because which one you have depends on how the project was set up, and finding
  out the hard way costs an hour.
* **Embeddings.** `:predict` with an `instances` array, NOT `:embedContent`.
  The response is `predictions[].embeddings.values`. Getting this wrong is a
  404 that looks like a missing model.

What is *identical* is the `generateContent` request and response body, so
`build_payload`, `parse_completion` and `extract_json` are imported from the
Gemini provider rather than duplicated -- the thinking-budget handling and the
truncation trap are the same on both, and should only be fixed in one place.

Why support Vertex at all: it is the enterprise surface. Regional data
residency, VPC-SC, CMEK, IAM instead of a bearer secret in a file, and audit
logs in Cloud Logging. For a system whose selling point is trustworthiness, "we
can run inside your compliance boundary" is a real answer rather than a
checkbox.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from agentcore.errors import ConfigError, ProviderError
from agentcore.llm.base import CircuitBreaker, Completion, with_retries
from agentcore.llm.gemini import (
    build_payload,
    extract_json,
    l2_normalise,
    parse_completion,
    to_gemini_schema,
)
from agentcore.logging import get_logger
from agentcore.settings import Settings

log = get_logger(__name__)

#: Vertex task types match the Gemini API's, but are passed inside the
#: `instances` payload rather than alongside the content.
_TASK_QUERY = "RETRIEVAL_QUERY"
_TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"


def _host(location: str) -> str:
    """Regional endpoint host.

    `global` is not a region prefix -- using `global-aiplatform.googleapis.com`
    fails DNS, which presents as a connection error rather than a config error.
    """
    if location == "global":
        return "https://aiplatform.googleapis.com"
    return f"https://{location}-aiplatform.googleapis.com"


class _HttpxAuthTransport:
    """google-auth transport backed by httpx.

    google-auth's own transport imports `requests`, which would mean carrying a
    second HTTP stack purely to refresh a token. Its `Request` interface is
    small and pluggable, so we keep the part that matters -- the RFC 7523 JWT
    assertion and refresh logic, which is security-sensitive and should not be
    hand-rolled -- and supply the ten lines of transport ourselves.
    """

    class _Response:
        # Attribute names are fixed by google.auth.transport.Response.
        def __init__(self, status: int, data: bytes, headers: dict[str, str]) -> None:
            self.status = status
            self.data = data
            self.headers = headers

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> _Response:
        with httpx.Client(timeout=timeout or 30.0) as client:
            response = client.request(
                method, url, content=body, headers=headers or {}
            )
        return self._Response(
            response.status_code, response.content, dict(response.headers)
        )


def _materialise_credentials(raw: str) -> str:
    """Write service-account JSON to a private temp file and return its path.

    `google-auth` wants a file, and a managed host gives you an environment
    variable. Rather than carry a second auth path for hosted deployments, the
    JSON is written once at startup and everything downstream sees a normal
    credentials path.

    Mode 0600 and a process-owned temp directory: the file holds a private key,
    and a world-readable secret on a shared host is worse than no secret at all.
    Validated as JSON first so a truncated paste fails here, with a clear
    message, rather than as an opaque 401 on the first question.
    """
    import json
    import os
    import tempfile

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON is not valid JSON. Paste the "
            "whole service-account file, including the outer braces."
        ) from exc
    if parsed.get("type") != "service_account":
        raise ConfigError(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON does not look like a service "
            f"account (type={parsed.get('type')!r})."
        )

    directory = tempfile.mkdtemp(prefix="parcelpilot-creds-")
    os.chmod(directory, 0o700)
    path = os.path.join(directory, "service-account.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(raw)
    os.chmod(path, 0o600)
    log.info("credentials_materialised_from_env", project=parsed.get("project_id"))
    return path


class _TokenSource:
    """Supplies a currently-valid bearer token.

    Refreshed lazily, five minutes before expiry. Resolving the token once at
    construction -- which is what this class replaced -- works in a script and
    fails in a server: service-account tokens last about an hour, so a
    long-running process would start returning 401s after that with no obvious
    cause.
    """

    #: Refresh early rather than exactly at expiry, so an in-flight request
    #: cannot be signed with a token that expires mid-call.
    SKEW_SECONDS = 300

    def __init__(self, credentials_path: str) -> None:
        try:
            from google.oauth2 import service_account  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConfigError(
                "GOOGLE_APPLICATION_CREDENTIALS is set but google-auth is not "
                'installed. Run: pip install -e ".[vertex]"',
            ) from exc

        self._credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        self._transport = _HttpxAuthTransport()

    def token(self) -> str:
        expiry = getattr(self._credentials, "expiry", None)
        stale = expiry is None or (
            expiry - datetime.now(UTC).replace(tzinfo=None)
        ) < timedelta(seconds=self.SKEW_SECONDS)

        if not self._credentials.token or stale:
            self._credentials.refresh(self._transport)
            log.info("vertex_token_refreshed", expiry=str(self._credentials.expiry))
        return str(self._credentials.token)


class VertexClient:
    """Transport, auth and breaker for Vertex AI."""

    def __init__(self, settings: Settings, breaker_name: str) -> None:
        if not settings.vertex_project:
            raise ConfigError(
                "LLM_PROVIDER=vertex requires VERTEX_PROJECT (your Google Cloud "
                "project id, e.g. my-project-123456)."
            )
        self._settings = settings
        self._project = settings.vertex_project
        self._location = settings.vertex_location
        self._breaker = CircuitBreaker(
            settings.llm_breaker_threshold,
            settings.llm_breaker_cooldown_seconds,
            name=breaker_name,
        )
        self._token_source: _TokenSource | None = None

        credentials_path = settings.google_application_credentials
        if not credentials_path and settings.google_application_credentials_json:
            credentials_path = _materialise_credentials(
                settings.google_application_credentials_json
            )

        if credentials_path:
            self._token_source = _TokenSource(credentials_path)
            self._mode = "service_account"
        elif settings.vertex_access_token:
            self._mode = "access_token"
        elif settings.llm_api_key:
            self._mode = "api_key_express"
        else:
            raise ConfigError(
                "Vertex needs credentials. Set one of: "
                "GOOGLE_APPLICATION_CREDENTIALS (service-account JSON path), "
                "GOOGLE_APPLICATION_CREDENTIALS_JSON (the JSON itself, for "
                "hosts without a filesystem for secrets), "
                "VERTEX_ACCESS_TOKEN (from `gcloud auth print-access-token`), "
                "or LLM_API_KEY (express mode)."
            )
        log.info("vertex_auth", mode=self._mode, project=self._project,
                 location=self._location)

        self._client = httpx.AsyncClient(
            base_url=_host(self._location),
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
            headers={"content-type": "application/json"},
        )

    def _auth_headers(self) -> dict[str, str]:
        """Per-request, so a refreshed token is actually used.

        Static headers on the AsyncClient would pin whatever token existed when
        the client was built.
        """
        if self._token_source is not None:
            return {"authorization": f"Bearer {self._token_source.token()}"}
        if self._settings.vertex_access_token:
            return {"authorization": f"Bearer {self._settings.vertex_access_token}"}
        # Express mode: sent as a header, never in the URL, so the key cannot
        # leak into an access log or a traceback.
        return {"x-goog-api-key": self._settings.llm_api_key or ""}

    def model_path(self, model: str) -> str:
        return (
            f"/v1/projects/{self._project}/locations/{self._location}"
            f"/publishers/google/models/{model}"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post(self, path: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
        async def call() -> dict[str, Any]:
            response = await self._client.post(
                path, json=payload, headers=self._auth_headers()
            )
            if response.status_code >= 400:
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


class VertexLLM:
    """Text and structured generation via Vertex AI."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = VertexClient(settings, breaker_name="vertex-llm")

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
        target = model or self.synthesis_model
        raw = await self._client.post(
            f"{self._client.model_path(target)}:generateContent",
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
            f"{self._client.model_path(target)}:generateContent",
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


class VertexEmbedder:
    """Embeddings via Vertex `:predict`.

    A different call shape from the Gemini API, which is the main reason this
    class exists rather than a shared one: Vertex takes an `instances` array and
    returns `predictions[].embeddings.values`.
    """

    #: Vertex caps instances per predict call well below the Gemini API's batch.
    MAX_BATCH = 25

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = VertexClient(settings, breaker_name="vertex-embed")
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

        task_type = _TASK_QUERY if is_query else _TASK_DOCUMENT
        batch_size = min(self._settings.embedding_batch_size, self.MAX_BATCH)
        vectors: list[list[float]] = []

        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            raw = await self._client.post(
                f"{self._client.model_path(self.model)}:predict",
                {
                    "instances": [
                        {"content": text, "task_type": task_type} for text in batch
                    ],
                    "parameters": {"outputDimensionality": self._dimension},
                },
                "predict",
            )
            predictions = raw.get("predictions") or []
            if len(predictions) != len(batch):
                raise ProviderError(
                    "embedding count does not match the batch size; refusing to "
                    "align vectors to the wrong chunks",
                    requested=len(batch),
                    returned=len(predictions),
                )
            for prediction in predictions:
                values = (prediction.get("embeddings") or {}).get("values") or []
                if len(values) != self._dimension:
                    raise ProviderError(
                        "embedding dimension does not match EMBEDDING_DIM",
                        expected=self._dimension,
                        actual=len(values),
                        hint="run `parcelpilot llm probe` and set EMBEDDING_DIM to match",
                    )
                # Same reason as the Gemini path: a truncated dimension is no
                # longer unit-normalised, and retrieval computes cosine as a
                # dot product.
                vectors.append(l2_normalise(values))

        log.info(
            "embedded",
            count=len(vectors),
            model=self.model,
            task_type=task_type,
            dimension=self._dimension,
            provider="vertex",
        )
        return vectors

    async def probe(self) -> dict[str, Any]:
        raw = await self._client.post(
            f"{self._client.model_path(self.model)}:predict",
            {
                "instances": [{"content": "probe", "task_type": _TASK_DOCUMENT}],
                "parameters": {"outputDimensionality": self._dimension},
            },
            "predict",
        )
        predictions = raw.get("predictions") or []
        values = (
            (predictions[0].get("embeddings") or {}).get("values") or []
            if predictions
            else []
        )
        return {
            "model": self.model,
            "requested_dimension": self._dimension,
            "actual_dimension": len(values),
            "matches": len(values) == self._dimension,
        }


def build(settings: Settings) -> tuple[VertexLLM, VertexEmbedder | None]:
    if settings.llm_provider != "vertex":
        raise ConfigError(f"vertex builder called for provider {settings.llm_provider}")
    llm = VertexLLM(settings)
    embedder = VertexEmbedder(settings) if settings.embedding_backend == "api" else None
    return llm, embedder
