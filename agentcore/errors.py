"""Exception hierarchy.

Deliberately shallow. The important distinction is not "what went wrong" but
"who is allowed to see why" -- so every error declares whether its message is
safe to show a user, and what HTTP status it maps to. Leaking an internal
message to a customer portal is a security bug, not a cosmetic one.
"""

from __future__ import annotations


class ParcelPilotError(Exception):
    """Base for everything this engine raises deliberately."""

    #: Shown verbatim to the caller when True; replaced with a generic message
    #: when False. Default False, so a new exception type is private until
    #: someone decides otherwise.
    user_safe: bool = False
    status_code: int = 500

    def __init__(self, message: str, /, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def public_message(self) -> str:
        if self.user_safe:
            return self.message
        return "The system could not complete this request. It has been logged for review."


# --- configuration and startup ---------------------------------------------


class ConfigError(ParcelPilotError):
    """Malformed config.yaml or missing required environment."""


class MigrationError(ParcelPilotError):
    """Schema is not at the revision this build requires.

    Fatal at startup on purpose: serving traffic against an unknown schema is
    how you get silent wrong answers instead of a loud failure.
    """


# --- security ---------------------------------------------------------------


class AuthenticationError(ParcelPilotError):
    user_safe = True
    status_code = 401


class AuthorizationError(ParcelPilotError):
    """The principal is known but not permitted.

    Message is intentionally not user-safe in detail: telling a customer
    *which* account they failed to reach confirms that account exists.
    """

    status_code = 403

    def public_message(self) -> str:
        return "You do not have access to this resource."


class TenancyViolation(ParcelPilotError):
    """A query or result crossed a tenant or account boundary.

    This should be unreachable -- RLS is the primary defence. If it is ever
    raised, the correct response is to fail the request loudly and page
    someone, not to filter the result and continue.
    """

    status_code = 500


class UntrustedContentError(ParcelPilotError):
    """Third-party content attempted to reach a trusted channel."""


# --- data layer -------------------------------------------------------------


class RepositoryError(ParcelPilotError):
    """Database access failed."""


class UnknownQueryTemplate(RepositoryError):
    """A tool asked for a SQL template that is not in the registry.

    The registry is the entire allowed query surface. There is no path from a
    model-generated string to the database.
    """


# --- ingestion --------------------------------------------------------------


class IngestionError(ParcelPilotError):
    """One document failed to ingest.

    Caught per-file by the pipeline: a single unparseable PDF must not abort
    the run or leave the index half-built.
    """


# --- retrieval and synthesis ------------------------------------------------


class NoEligibleSourceError(ParcelPilotError):
    user_safe = True
    status_code = 422


class CitationValidationError(ParcelPilotError):
    """A generated answer cited something it should not have.

    Never surfaced as an error to the user -- the orchestrator converts it into
    a regeneration attempt and then an honest refusal.
    """


class PolicyParameterMissing(ParcelPilotError):
    """A rule needs a reviewed parameter that the policy pack does not define.

    Yields an INDETERMINATE verdict and an escalation, never a guessed number.
    """


# --- upstream providers -----------------------------------------------------


class ProviderError(ParcelPilotError):
    status_code = 502


class ProviderTimeout(ProviderError):
    pass


class CircuitOpen(ProviderError):
    """Upstream is failing; requests are being shed rather than queued."""


class BudgetExhausted(ParcelPilotError):
    """The run hit its token, step or wall-clock ceiling."""

    user_safe = True
    status_code = 429


# --- action ledger ----------------------------------------------------------


class ActionError(ParcelPilotError):
    status_code = 409


class RunNotVisible(ActionError):
    """The run a request refers to is not readable by this principal.

    404 rather than 403, and user-safe: telling someone a run exists but is not
    theirs confirms it exists. One message for "no such run" and "not yours".
    """

    user_safe = True
    status_code = 404


class ActionExpired(ActionError):
    user_safe = True


class ActionAlreadySettled(ActionError):
    """Confirm/reject arrived for an action that is no longer pending.

    The idempotent outcome: report the existing terminal state, never execute
    a second time.
    """

    user_safe = True


class ActionPayloadTampered(ActionError):
    """Stored payload digest does not match the stored payload.

    Indicates database tampering rather than a client bug, since the client
    never sends the payload back. Refuse to execute.
    """

    status_code = 500
