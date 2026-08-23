"""Configuration: secrets from the environment, behaviour from config.yaml.

The split is not cosmetic. `Settings` is per-deployment and must never be
committed; `EngineConfig` is the reviewed description of how the system
behaves and belongs in version control, because "why did it answer that in
August" is answerable only if the trust model is diffable.
"""

from __future__ import annotations

import functools
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentcore.errors import ConfigError
from agentcore.types import Eligibility, SourceClass

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


# ---------------------------------------------------------------------------
# Environment (secrets, endpoints, per-deployment limits)
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- database ---
    #: Runtime role. Must be a NON-superuser, NON-owner role or row-level
    #: security silently does nothing: Postgres exempts both.
    database_url: str = "postgresql://parcelpilot_app:parcelpilot@127.0.0.1:5432/parcelpilot"
    #: Superuser connection, used only by bootstrap/migrate commands.
    admin_database_url: str | None = None
    db_pool_min: int = 1
    db_pool_max: int = 8
    db_statement_timeout_ms: int = 10_000

    # --- llm ---
    llm_provider: Literal["gemini", "vertex", "openai", "null"] = "gemini"
    llm_api_key: str | None = None
    #: Cheap model for tool routing; the loop calls it many times per run.
    llm_routing_model: str = "gemini-2.5-flash"
    #: Stronger model for the one call that must produce a cited answer.
    llm_synthesis_model: str = "gemini-2.5-pro"
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    #: Consecutive upstream failures before the breaker opens and requests are
    #: shed instead of queued. Queueing against a dead provider converts one
    #: outage into a thread-pool exhaustion.
    llm_breaker_threshold: int = 5
    llm_breaker_cooldown_seconds: float = 30.0

    # --- vertex ai (LLM_PROVIDER=vertex) ---
    #: Google Cloud project id. Required for Vertex; it is part of the URL path,
    #: not a header, so there is no default that could silently work.
    vertex_project: str | None = None
    #: Region. Affects data residency and which models are available, so it is
    #: explicit rather than inferred.
    vertex_location: str = "us-central1"
    #: Paste `gcloud auth print-access-token` for a zero-setup connection test.
    #: Short-lived by design; not for production.
    vertex_access_token: str | None = None
    #: Service-account JSON path. The production path: IAM-governed, and the
    #: token refreshes itself.
    google_application_credentials: str | None = None
    #: The service-account JSON itself, for hosts that inject environment
    #: variables but have no filesystem you can put a secret on (Railway,
    #: Render, Fly, Cloud Run). Written to a private temp file at startup and
    #: then treated exactly like a credentials path, so there is one code path
    #: for auth rather than two.
    #:
    #: Precedence is deliberate: an explicit PATH wins, because a developer with
    #: a file on disk means to use it. This is the fallback, not an override.
    google_application_credentials_json: str | None = None

    # --- embeddings ---
    #: "none" is a supported, tested mode: retrieval degrades to lexical-only
    #: rather than the whole system refusing to boot without a key.
    embedding_backend: Literal["api", "local", "none"] = "api"
    embedding_model: str = "gemini-embedding-001"
    #: Gemini's embedding models accept an output dimensionality; the value here
    #: is asserted against what the API actually returns at ingest time, because
    #: a silent mismatch corrupts every cosine score in the index.
    embedding_dim: int = 1536
    embedding_batch_size: int = 64

    # --- auth ---
    jwt_secret: str = "dev-only-change-me"
    jwt_ttl_seconds: int = 3600

    # --- run limits (ceilings; config.yaml may lower them, never raise) ---
    agent_max_steps: int = 8
    agent_token_budget: int = 24_000
    agent_wall_clock_seconds: int = 90

    # --- observability ---
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    @field_validator("database_url", "admin_database_url")
    @classmethod
    def _must_be_postgres(cls, v: str | None) -> str | None:
        if v and not v.startswith(("postgresql://", "postgres://")):
            raise ValueError("only PostgreSQL connection URLs are supported")
        return v

    @property
    def embeddings_enabled(self) -> bool:
        return self.embedding_backend != "none"

    def require_llm_key(self) -> str:
        if not self.llm_api_key:
            raise ConfigError(
                "LLM_API_KEY is not set; the engine will refuse to synthesise answers",
                provider=self.llm_provider,
            )
        return self.llm_api_key


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:  # pragma: no cover - startup path
        raise ConfigError(f"invalid environment configuration: {exc}") from exc


# ---------------------------------------------------------------------------
# Engine config (behaviour, version-controlled)
# ---------------------------------------------------------------------------


class Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TenantConfig(Strict):
    id: str
    name: str
    industry: str = "general"
    #: Every SLA and cancellation window is wall-clock sensitive, so the
    #: tenant's civil timezone is config, not a runtime guess.
    timezone: str = "UTC"
    currency: str = "USD"


class DataConfig(Strict):
    documents_dir: Path
    structured_dir: Path
    artifacts_dir: Path = Path("./artifacts")
    #: The instant this dataset describes. When set, it is the reference time for
    #: every wall-clock comparison instead of `now()`.
    #:
    #: The dataset is a fixed snapshot. Staff had an "as of" field and customers
    #: had nothing, so a customer was told a pickup was "171.1 hours past the
    #: 4-hour threshold" -- seven days of real elapsed time since the snapshot,
    #: presented as fact, while the dashboard said 4.5 hours for the same order.
    #: The credit amount stayed correct because the rule engine compares against
    #: thresholds rather than printing elapsed time, so this was an evidence bug
    #: rather than a money bug. It is still the kind of number that ends a
    #: customer's trust in one line.
    #:
    #: Server-side and per-deployment on purpose: a customer must not be able to
    #: choose what time it is, and a demo must not depend on the operator
    #: remembering to type a timestamp. Leave it null in a real deployment, where
    #: the data is live and `now()` is the truth.
    snapshot_at: datetime | None = None

    def resolved(self, root: Path = REPO_ROOT) -> DataConfig:
        return DataConfig(
            documents_dir=(root / self.documents_dir).resolve(),
            structured_dir=(root / self.structured_dir).resolve(),
            artifacts_dir=(root / self.artifacts_dir).resolve(),
            # Carried through explicitly. Rebuilding the model without it would
            # drop the snapshot clock the moment paths were resolved, and the
            # only symptom would be wrong elapsed times in prose.
            snapshot_at=self.snapshot_at,
        )


class SourceClassConfig(Strict):
    type: SourceClass
    #: Tie-breaker between two sources that could both answer. Compared only
    #: against other authorities -- never mixed into a relevance score.
    authority: int = Field(ge=0, le=100)
    eligibility: Eligibility
    scope: Literal["global", "account"] = "global"
    overrides: list[SourceClass] = Field(default_factory=list)
    note: str | None = None


class ConflictRule(Strict):
    id: str
    when: str
    then: str


class SourcesConfig(Strict):
    classes: list[SourceClassConfig]
    conflict_rules: list[ConflictRule] = Field(default_factory=list)

    @field_validator("classes")
    @classmethod
    def _one_entry_per_class(cls, v: list[SourceClassConfig]) -> list[SourceClassConfig]:
        seen = [c.type for c in v]
        if len(seen) != len(set(seen)):
            raise ValueError("duplicate source class; trust model would be ambiguous")
        return v

    @functools.cached_property
    def by_type(self) -> dict[SourceClass, SourceClassConfig]:
        return {c.type: c for c in self.classes}

    def get(self, source_class: SourceClass) -> SourceClassConfig:
        try:
            return self.by_type[source_class]
        except KeyError as exc:
            raise ConfigError(
                f"source class {source_class} is not declared in config.yaml"
            ) from exc

    @functools.cached_property
    def groundable(self) -> frozenset[SourceClass]:
        """The only classes permitted to support a claim."""
        return frozenset(
            c.type for c in self.classes if c.eligibility is Eligibility.GROUNDABLE
        )


class LexicalConfig(Strict):
    enabled: bool = True
    backend: Literal["postgres_fts"] = "postgres_fts"
    candidates: int = 50


class DenseConfig(Strict):
    enabled: bool = True
    #: postgres_array = float4[] + exact cosine, correct at this corpus size.
    #: pgvector swaps in behind the same interface once the extension exists.
    backend: Literal["postgres_array", "pgvector"] = "postgres_array"
    candidates: int = 50


class FusionConfig(Strict):
    #: Reciprocal rank fusion is rank-based, so two scorers with unrelated
    #: score scales can be combined without inventing a normalisation.
    method: Literal["rrf"] = "rrf"
    k: int = 60


class RerankConfig(Strict):
    enabled: bool = True
    backend: Literal["llm_listwise", "none"] = "llm_listwise"
    top_n: int = 20


class RetrievalConfig(Strict):
    mode: Literal["hybrid", "lexical", "dense"] = "hybrid"
    #: Run the dense half only when lexical retrieval looks weak.
    #:
    #: Measured: lexical over this corpus takes 26ms; embedding the query costs
    #: ~1.9s in an API round trip. On a corpus of numbered clauses full of exact
    #: tokens, lexical alone almost always surfaces the operative clause -- so
    #: paying 1.9s on every question to improve recall on the minority of
    #: paraphrased ones is the wrong default. Dense still runs when lexical
    #: returns little, which is exactly when paraphrase recall matters.
    dense_only_when_lexical_weak: bool = True
    #: Lexical hits at or below this count trigger the dense half.
    lexical_weak_threshold: int = 2
    lexical: LexicalConfig = LexicalConfig()
    dense: DenseConfig = DenseConfig()
    fusion: FusionConfig = FusionConfig()
    rerank: RerankConfig = RerankConfig()
    final_k: int = 6


class CitationValidationConfig(Strict):
    enabled: bool = True
    #: The core guarantee: a cited quote must appear character-for-character in
    #: the chunk it points at.
    require_verbatim_span: bool = True
    max_regenerations: int = 1


class AgentConfig(Strict):
    #: "fast" plans tools deterministically from the question; "llm" asks the
    #: model. Fast is the default because it removes a ~2.6s round trip AND
    #: makes tool selection reproducible -- the same question always runs the
    #: same tools, which for a system whose product is trustworthiness is a
    #: feature rather than a compromise. The LLM router remains available for
    #: corpora where intent is genuinely ambiguous.
    router: Literal["fast", "llm"] = "fast"
    #: Tokens the synthesis model may spend on internal reasoning before it
    #: answers. Zero because synthesis is not a reasoning task here: the fee is
    #: already computed, the clause is already selected, and the model's job is
    #: to attribute sentences to spans it has been handed. Thinking was the
    #: single largest contributor to wall clock. Raise it only if the golden set
    #: says you must -- `eval run` is the arbiter, not intuition.
    #: `null` sends no thinkingConfig at all, leaving the provider default --
    #: which is what you want when a model refuses to accept an explicit budget.
    #: gemini-2.5-pro cannot disable thinking; its floor is 128.
    synthesis_thinking_budget: int | None = Field(default=0, ge=0)
    max_steps: int = 8
    wall_clock_seconds: int = 90
    token_budget: int = 24_000
    refusal_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    citation_validation: CitationValidationConfig = CitationValidationConfig()


class ToolConfig(Strict):
    name: str
    enabled: bool = True
    tenant_scoped: bool = False
    requires_confirmation: bool = False
    eligibility: Eligibility | None = None


class ActionLedgerConfig(Strict):
    ttl_seconds: int = 900
    require_idempotency_key: bool = True


class SecurityConfig(Strict):
    untrusted_content_channels: list[str] = Field(
        default_factory=lambda: ["doc_chunk", "ticket_body", "customer_message"]
    )
    action_ledger: ActionLedgerConfig = ActionLedgerConfig()


class ObservabilityConfig(Strict):
    persist_run_steps: bool = True
    #: Candidates, not only citations -- otherwise a retrieval miss is
    #: invisible after the fact.
    persist_retrieval_candidates: bool = True
    persist_token_costs: bool = True


class EngineConfig(Strict):
    schema_version: int
    tenant: TenantConfig
    data: DataConfig
    sources: SourcesConfig
    retrieval: RetrievalConfig = RetrievalConfig()
    agent: AgentConfig = AgentConfig()
    tools: list[ToolConfig] = Field(default_factory=list)
    security: SecurityConfig = SecurityConfig()
    observability: ObservabilityConfig = ObservabilityConfig()

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, v: int) -> int:
        if v != 2:
            raise ValueError(f"config schema_version {v} is not supported by this build")
        return v

    def tool(self, name: str) -> ToolConfig | None:
        return next((t for t in self.tools if t.name == name), None)

    def tool_enabled(self, name: str) -> bool:
        cfg = self.tool(name)
        return bool(cfg and cfg.enabled)


@functools.lru_cache(maxsize=4)
def load_config(path: Path | None = None) -> EngineConfig:
    """Parse and validate config.yaml.

    Fails loudly at startup. A trust model with a typo in it is worse than no
    trust model, because it looks like it is working.
    """
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config file must contain a mapping: {config_path}")

    try:
        config = EngineConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config.yaml: {exc}") from exc

    # Env ceilings win. config.yaml may tighten a limit for a deployment but
    # must not be able to raise one past what the operator allowed.
    settings = get_settings()
    agent = config.agent.model_copy(
        update={
            "max_steps": min(config.agent.max_steps, settings.agent_max_steps),
            "token_budget": min(config.agent.token_budget, settings.agent_token_budget),
            "wall_clock_seconds": min(
                config.agent.wall_clock_seconds, settings.agent_wall_clock_seconds
            ),
        }
    )
    return config.model_copy(
        update={"agent": agent, "data": config.data.resolved()}
    )
