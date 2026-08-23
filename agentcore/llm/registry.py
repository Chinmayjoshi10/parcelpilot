"""Provider selection, and the degraded modes that keep the system bootable.

The engine asks for capabilities, never for a vendor. Two absences are handled
as first-class states rather than crashes:

* **No API key.** `NullLLM` raises a domain error on use, so the orchestrator
  converts it into an honest refusal with an escalation offer. The server still
  starts, health still reports, retrieval still works.
* **No embedder.** Retrieval degrades to lexical-only. On a corpus of legal
  prose full of exact tokens that is a real fallback, not a token gesture --
  but it is logged loudly at startup so nobody mistakes it for full fidelity.
"""

from __future__ import annotations

from typing import Any

from agentcore.errors import ConfigError, ProviderError
from agentcore.llm.base import LLM, Completion, Embedder
from agentcore.logging import get_logger
from agentcore.settings import Settings, get_settings

log = get_logger(__name__)


class NullLLM:
    """Stands in when no key is configured.

    Deliberately not a stub that returns plausible text. A system whose
    trustworthiness is the product must fail visibly, not fluently.
    """

    @property
    def routing_model(self) -> str:
        return "null"

    @property
    def synthesis_model(self) -> str:
        return "null"

    async def complete_json(self, **_: Any) -> Completion:
        raise ProviderError(
            "no LLM is configured, so the engine cannot compose an answer. "
            "Set LLM_API_KEY in .env."
        )

    async def complete_text(self, **_: Any) -> Completion:
        raise ProviderError(
            "no LLM is configured. Set LLM_API_KEY in .env."
        )

    async def aclose(self) -> None:
        return None


#: Process-wide singletons for the default configuration. Deliberately NOT
#: lru_cache: pydantic Settings is not hashable, so caching on the argument
#: raised TypeError for every caller that passed settings explicitly -- only the
#: no-argument path ever worked. Explicit settings now bypass the cache, which
#: is also what tests want.
_llm_singleton: LLM | None = None
_embedder_singleton: Embedder | None = None
_embedder_resolved = False


def get_llm(settings: Settings | None = None) -> LLM:
    global _llm_singleton
    if settings is not None:
        return _build_llm(settings)
    if _llm_singleton is None:
        _llm_singleton = _build_llm(get_settings())
    return _llm_singleton


def _build_llm(cfg: Settings) -> LLM:

    has_credentials = bool(
        cfg.llm_api_key or cfg.vertex_access_token or cfg.google_application_credentials
    )
    if cfg.llm_provider == "null" or not has_credentials:
        log.warning(
            "llm_unavailable",
            reason="no credentials configured" if not has_credentials else "provider=null",
            consequence="the engine will refuse to synthesise rather than guess",
        )
        return NullLLM()

    if cfg.llm_provider == "gemini":
        from agentcore.llm.gemini import GeminiLLM

        return GeminiLLM(cfg)

    if cfg.llm_provider == "vertex":
        from agentcore.llm.vertex import VertexLLM

        return VertexLLM(cfg)

    # The provider seam is real -- Gemini and Null both implement it -- but no
    # OpenAI adapter is written. Failing here with a clear message beats an
    # opaque ImportError from a lazy import of a module that does not exist.
    if cfg.llm_provider == "openai":
        raise ConfigError(
            "LLM_PROVIDER=openai is declared but no OpenAI adapter is implemented. "
            "Use LLM_PROVIDER=gemini, or add agentcore/llm/openai.py satisfying "
            "the LLM protocol in agentcore/llm/base.py.",
            provider=cfg.llm_provider,
        )

    raise ConfigError(f"unknown LLM_PROVIDER: {cfg.llm_provider}")


def get_embedder(settings: Settings | None = None) -> Embedder | None:
    """Return the embedder, or None when dense retrieval is unavailable.

    None is a supported state that the retrieval layer handles by running
    lexical-only. Callers must not treat it as an error -- which is why the
    cache needs a separate `_resolved` flag rather than treating None as "not
    yet built".
    """
    global _embedder_singleton, _embedder_resolved
    if settings is not None:
        return _build_embedder(settings)
    if not _embedder_resolved:
        _embedder_singleton = _build_embedder(get_settings())
        _embedder_resolved = True
    return _embedder_singleton


def _build_embedder(cfg: Settings) -> Embedder | None:

    if cfg.embedding_backend == "none":
        log.warning(
            "embeddings_disabled",
            mode="lexical_only",
            consequence="retrieval uses Postgres full-text search alone",
        )
        return None

    # `google_application_credentials_json` belongs in this list, and its absence
    # was a deployment-only defect. A managed host has nowhere to put a key FILE,
    # so `agentcore/llm/vertex.py` accepts the service account as a JSON env var
    # and materialises it -- which is the documented way to deploy this. But this
    # check only looked for the PATH, so on the first real deploy it concluded
    # "no provider credentials are set" and returned None.
    #
    # Nothing failed. `ingest run` reported `embedded: 0` with a warning, the
    # index activated, readiness went green, and dense retrieval was silently
    # gone -- exactly the shape of degradation this codebase keeps trying to make
    # loud. Credentials were present the whole time, in the one form the check
    # could not see.
    if cfg.embedding_backend == "api" and not (
        cfg.llm_api_key
        or cfg.vertex_access_token
        or cfg.google_application_credentials
        or cfg.google_application_credentials_json
    ):
        log.warning(
            "embeddings_unavailable",
            reason="EMBEDDING_BACKEND=api but no provider credentials are set",
            mode="lexical_only",
        )
        return None

    if cfg.embedding_backend == "local":
        raise ConfigError(
            "EMBEDDING_BACKEND=local requires a sentence-transformers adapter "
            "that is not implemented. Install the local-embeddings extra and add "
            "agentcore/llm/local_embedder.py, or use EMBEDDING_BACKEND=api "
            "(or 'none' for lexical-only retrieval).",
            backend=cfg.embedding_backend,
        )

    if cfg.llm_provider == "gemini":
        from agentcore.llm.gemini import GeminiEmbedder

        return GeminiEmbedder(cfg)

    if cfg.llm_provider == "vertex":
        from agentcore.llm.vertex import VertexEmbedder

        return VertexEmbedder(cfg)

    raise ConfigError(
        f"no embedder for provider {cfg.llm_provider} with backend {cfg.embedding_backend}"
    )


def reset() -> None:
    """Drop cached clients. Used by tests and after a config change."""
    global _llm_singleton, _embedder_singleton, _embedder_resolved
    _llm_singleton = None
    _embedder_singleton = None
    _embedder_resolved = False


async def _probe_vertex(cfg: Settings, result: dict[str, Any]) -> dict[str, Any]:
    """Exercise the two Vertex calls the engine depends on.

    No model listing: Vertex has no cheap equivalent, and the endpoints below are
    what actually matter. Reports the resolved project, region and auth mode,
    because a 404 on Vertex is nearly always one of those three being wrong
    rather than the model name.
    """
    from agentcore.llm.vertex import VertexEmbedder, VertexLLM

    result["vertex"] = {
        "project": cfg.vertex_project,
        "location": cfg.vertex_location,
        "auth_mode": (
            "access_token"
            if cfg.vertex_access_token
            else "service_account"
            if cfg.google_application_credentials
            else "api_key_express"
            if cfg.llm_api_key
            else "none"
        ),
    }
    if not cfg.vertex_project:
        result["error"] = "VERTEX_PROJECT is not set"
        return result

    llm = VertexLLM(cfg)
    try:
        completion = await llm.complete_json(
            system="Reply strictly as JSON matching the schema.",
            user="Set ok to true and note to 'vertex structured output works'.",
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}},
                "required": ["ok", "note"],
            },
            model=cfg.llm_routing_model,
            max_output_tokens=256,
        )
        result["structured_output"] = {
            "ok": True,
            "returned": completion.data,
            "tokens": completion.usage.total,
        }
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        result["structured_output"] = {"ok": False, "error": str(exc)[:400]}
    finally:
        await llm.aclose()

    if cfg.embedding_backend == "api":
        embedder = VertexEmbedder(cfg)
        try:
            result["embeddings"] = await embedder.probe()
        except Exception as exc:  # noqa: BLE001
            result["embeddings"] = {"ok": False, "error": str(exc)[:400]}
        finally:
            await embedder.aclose()

    return result


async def probe(settings: Settings | None = None) -> dict[str, Any]:
    """Verify the configured provider against the live API.

    Answers the three questions a config file cannot: does the key work, which
    models can it actually reach, and is EMBEDDING_DIM the dimension the API
    really returns. The last one matters most -- a mismatch corrupts every
    cosine score in the index, silently.
    """
    cfg = settings or get_settings()
    result: dict[str, Any] = {
        "provider": cfg.llm_provider,
        "key_present": bool(cfg.llm_api_key),
        "credentials_present": bool(
            cfg.llm_api_key or cfg.vertex_access_token or cfg.google_application_credentials
        ),
    }
    if not (
        cfg.llm_api_key or cfg.vertex_access_token or cfg.google_application_credentials
    ):
        result["error"] = (
            "no credentials set. Gemini API needs LLM_API_KEY; Vertex needs one of "
            "LLM_API_KEY (express), VERTEX_ACCESS_TOKEN, or "
            "GOOGLE_APPLICATION_CREDENTIALS."
        )
        return result

    if cfg.llm_provider == "vertex":
        return await _probe_vertex(cfg, result)

    if cfg.llm_provider != "gemini":
        result["error"] = f"probe is implemented for gemini and vertex; got {cfg.llm_provider}"
        return result

    from agentcore.llm.gemini import GeminiClient, GeminiEmbedder, GeminiLLM

    # Model listing is INFORMATIONAL, never a gate.
    #
    # An API key can be restricted per-method: observed in practice returning
    # API_KEY_SERVICE_BLOCKED for ModelService.ListModels while generateContent
    # worked fine. A probe that failed on that would report a broken provider
    # when the provider is usable, which is the wrong direction for a
    # diagnostic to be wrong in. What we depend on is exercised directly below.
    client = GeminiClient(cfg, breaker_name="gemini-probe")
    try:
        models = await client.list_models()
        # Only models that can actually generate are candidates for routing or
        # synthesis; the list also includes embedding-only models.
        generative = sorted(
            m["name"].removeprefix("models/")
            for m in models
            if "generateContent" in m.get("supportedGenerationMethods", [])
        )
        embedding = sorted(
            m["name"].removeprefix("models/")
            for m in models
            if "embedContent" in m.get("supportedGenerationMethods", [])
        )
        result["models_reachable"] = len(models)
        result["generative_models"] = generative
        result["embedding_models"] = embedding
        result["configured"] = {
            "routing": cfg.llm_routing_model,
            "routing_available": cfg.llm_routing_model in generative,
            "synthesis": cfg.llm_synthesis_model,
            "synthesis_available": cfg.llm_synthesis_model in generative,
            "embedding": cfg.embedding_model,
            "embedding_available": cfg.embedding_model in embedding,
        }
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        result["model_listing"] = {
            "available": False,
            "error": str(exc)[:300],
            "note": (
                "listing is not used at runtime; the checks below are what matter"
            ),
        }
    finally:
        await client.aclose()

    # A round-trip through the real structured-output path, because a schema
    # the provider rejects is a 400 at answer time, not at startup.
    llm = GeminiLLM(cfg)
    try:
        completion = await llm.complete_json(
            system="Reply strictly as JSON matching the schema.",
            user="Set ok to true and note to 'structured output works'.",
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}},
                "required": ["ok", "note"],
            },
            model=cfg.llm_routing_model,
            max_output_tokens=256,
        )
        result["structured_output"] = {
            "ok": True,
            "returned": completion.data,
            "tokens": completion.usage.total,
        }
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        result["structured_output"] = {"ok": False, "error": str(exc)}
    finally:
        await llm.aclose()

    if cfg.embedding_backend == "api":
        embedder = GeminiEmbedder(cfg)
        try:
            result["embeddings"] = await embedder.probe()
        except Exception as exc:  # noqa: BLE001
            result["embeddings"] = {"ok": False, "error": str(exc)}
        finally:
            await embedder.aclose()

    return result
