"""Chat: start a run, then tail its durable log.

The transport is deliberately split in two:

    POST /api/chat            -> starts the run, returns a run_id immediately
    GET  /api/chat/{id}/stream -> Server-Sent Events, tailing run_steps

Most implementations run the agent *inside* the streaming request. That couples
the answer's survival to the connection: a dropped socket kills the run, a proxy
idle timeout kills the run, and a reconnect starts from scratch.

Here the run writes each step to `run_steps` and commits. The stream is a reader
over that table with a cursor. So:

* a dropped connection loses nothing -- reconnect and resume from `Last-Event-ID`
* the reasoning trace is replayable after the fact, for audit
* several viewers can watch the same run (a supervisor over an agent's shoulder)
* the 60-second idle timeouts common in proxies stop being a design constraint

The cost is one extra round trip. Worth it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agentcore.db import engine
from agentcore.db.engine import fetch_all, fetch_one
from agentcore.errors import ParcelPilotError
from agentcore.logging import get_logger
from agentcore.orchestrator.engine import summarise
from agentcore.types import Principal, Query, RunStatus
from app.deps import (
    CurrentOrchestrator,
    CurrentPrincipal,
    ReadOnlyConn,
    ScopedConn,
    http_error,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

#: How long a stream waits between polls of the run log. 250 ms is
#: imperceptible next to multi-second model calls, and cheap: the query is an
#: indexed read of a handful of rows.
_POLL_SECONDS = 0.25

#: Ceiling on a single stream, so an abandoned browser tab cannot hold a
#: connection indefinitely.
_STREAM_TIMEOUT_SECONDS = 300


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    #: The dataset is a snapshot; this lets a demo reproduce decisions at that
    #: instant. Ignored unless the caller is staff -- a customer must not be able
    #: to ask what a policy said at a time of their choosing.
    as_of: str | None = None


class ChatAccepted(BaseModel):
    run_id: UUID
    status: str
    stream_url: str
    #: The thread this run belongs to. Echoed back so the client can send it on
    #: the next message and keep one durable conversation rather than a series of
    #: unrelated runs.
    conversation_id: UUID


def _resolve_conversation(conn: Any, who: Any, requested: UUID | None) -> UUID:
    """Return the conversation this run belongs to, creating one if needed.

    A supplied id is verified through the SCOPED connection before it is used.
    That check is not redundant with the foreign key: PostgreSQL evaluates
    referential integrity as the referenced table's owner, which is exempt from
    row-level security. So the FK would happily accept another tenant's
    conversation id and silently file this run under it -- RLS would hide the
    row from the attacker afterwards, but the write would already have crossed a
    boundary, and the victim would see a stranger's question in their thread.
    Verifying the read first means an id you cannot see is an id you cannot use.
    """
    if requested is not None:
        visible = conn.execute(
            "SELECT conversation_id FROM conversations WHERE conversation_id = %s",
            (requested,),
        ).fetchone()
        if not visible:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="conversation not found",
            )
        conn.execute(
            "UPDATE conversations SET updated_at = now() WHERE conversation_id = %s",
            (requested,),
        )
        conn.commit()
        return requested

    row = conn.execute(
        """
        INSERT INTO conversations (tenant_id, account_id, user_id)
        VALUES (%s, %s, %s)
        RETURNING conversation_id
        """,
        (who.tenant_id, who.account_id, who.user_id),
    ).fetchone()
    conn.commit()
    return row["conversation_id"]


#: In-flight runs, so a task is not garbage-collected mid-execution. Bounded by
#: the number of concurrent requests, which the ASGI server already limits.
_running: set[asyncio.Task[Any]] = set()


@router.post("", response_model=ChatAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_chat(
    body: ChatRequest,
    who: CurrentPrincipal,
    agent: CurrentOrchestrator,
) -> ChatAccepted:
    """Accept a question and start answering it in the background.

    Returns 202 with a `run_id`. The run owns its own connection rather than the
    request's: the request is about to end, and the run outlives it.
    """
    from datetime import datetime

    reference_time = None
    if body.as_of and who.is_staff:
        try:
            reference_time = datetime.fromisoformat(body.as_of)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="as_of must be an ISO 8601 timestamp",
            ) from exc

    # The run is created synchronously so the caller gets a real run_id to
    # stream. Without this the client would have to poll for a run that might
    # not exist yet.
    with engine.scoped(who) as conn:
        conversation_id = _resolve_conversation(conn, who, body.conversation_id)
        query = Query(text=body.question, conversation_id=conversation_id)
        run_id = agent.create_run(conn, who, query)

    async def execute() -> None:
        try:
            with engine.scoped(who) as conn:
                await agent.resume_run(conn, who, query, run_id, now=reference_time)
        except Exception as exc:  # noqa: BLE001 - already recorded on the run
            log.error("background_run_failed", run_id=str(run_id), error=str(exc))

    task = asyncio.create_task(execute())
    _running.add(task)
    task.add_done_callback(_running.discard)

    return ChatAccepted(
        run_id=run_id,
        status=RunStatus.PENDING.value,
        stream_url=f"/api/chat/{run_id}/stream",
        conversation_id=conversation_id,
    )


class HandoffRequest(BaseModel):
    #: Optional extra context the customer types before sending it on.
    note: str | None = Field(default=None, max_length=2000)


@router.post("/{run_id}/handoff", status_code=status.HTTP_201_CREATED)
async def handoff(
    run_id: UUID,
    body: HandoffRequest,
    conn: ScopedConn,
    who: CurrentPrincipal,
) -> dict[str, Any]:
    """Hand this conversation to a human.

    Every refusal in this system says "I can pass this to a human support agent
    with everything gathered so far". This is the endpoint that makes the offer
    real, and it is deliberately NOT staff-gated: asking a person for help grants
    the asker nothing, and an offer only staff can accept is not an offer.

    The request becomes a `create_follow_up` ledger row awaiting confirmation,
    which is what "passed to a human" means -- it is in the support queue with
    the question, the refusal reason and the run id attached. The client is told
    that plainly rather than being told it is done.
    """
    run = fetch_one(
        conn,
        "SELECT run_id, query, refusal_reason FROM runs WHERE run_id = %s",
        (run_id,),
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such run"
        )

    from agentcore.tools import actions

    try:
        view = actions.request_handoff(
            conn,
            who,
            run_id=run_id,
            question=body.note or run["query"],
            reason=run["refusal_reason"],
        )
    except ParcelPilotError as exc:
        raise http_error(exc) from exc

    return {
        "handed_off": True,
        "action_id": str(view.action_id),
        "status": view.status.value,
        "summary": view.summary,
        "message": (
            "This is with the support team now, along with your question and "
            "everything the assistant looked at."
        ),
    }


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: UUID,
    request: Request,
    who: CurrentPrincipal,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    """Tail a run's steps as Server-Sent Events.

    `Last-Event-ID` is the step sequence number, so a reconnecting client
    resumes exactly where it dropped -- the browser sends it automatically on
    EventSource reconnect, and the sequence is database-assigned and gapless.

    RLS applies to every read here, so streaming another account's run returns
    nothing rather than their reasoning.
    """
    try:
        cursor = int(last_event_id) if last_event_id else 0
    except ValueError:
        cursor = 0

    async def events():
        seq = cursor
        waited = 0.0

        # Confirm the run is visible before streaming. Without this, a bad id
        # would produce an empty stream that looks like a slow run.
        with engine.scoped(who, read_only=True) as conn:
            run = fetch_one(
                conn, "SELECT run_id, status FROM runs WHERE run_id = %s", (run_id,)
            )
        if run is None:
            yield {"event": "error", "data": json.dumps({"detail": "no such run"})}
            return

        while waited < _STREAM_TIMEOUT_SECONDS:
            if await request.is_disconnected():
                log.info("stream_client_disconnected", run_id=str(run_id), seq=seq)
                return

            with engine.scoped(who, read_only=True) as conn:
                steps = fetch_all(
                    conn,
                    """
                    SELECT seq, kind, label, detail, duration_ms, started_at
                    FROM run_steps WHERE run_id = %s AND seq > %s
                    ORDER BY seq
                    """,
                    (run_id, seq),
                )
                state = fetch_one(
                    conn,
                    "SELECT status, answer_json FROM runs WHERE run_id = %s",
                    (run_id,),
                )

            for step in steps:
                seq = step["seq"]
                yield {
                    # The SSE id becomes the client's Last-Event-ID on reconnect.
                    "id": str(seq),
                    "event": "step",
                    "data": json.dumps(
                        {
                            "seq": seq,
                            "kind": step["kind"],
                            "label": step["label"],
                            "detail": step["detail"],
                            "duration_ms": step["duration_ms"],
                        },
                        default=str,
                    ),
                }

            status_value = (state or {}).get("status")
            if status_value in {
                RunStatus.COMPLETED.value,
                # A run that prepared an action is finished from the stream's
                # point of view: it produced an answer and is now waiting on a
                # person. Omitting it here left the stream open until the
                # 300-second ceiling, so the UI never received `done` and never
                # showed the confirmation drawer.
                RunStatus.AWAITING_CONFIRMATION.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
                RunStatus.EXPIRED.value,
            }:
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {
                            "status": status_value,
                            "answer": (state or {}).get("answer_json"),
                        },
                        default=str,
                    ),
                }
                return

            await asyncio.sleep(_POLL_SECONDS)
            waited += _POLL_SECONDS

        yield {
            "event": "timeout",
            "data": json.dumps({"detail": "stream timed out; re-open to continue"}),
        }

    return EventSourceResponse(events())


@router.get("/{run_id}")
async def get_run(run_id: UUID, conn: ReadOnlyConn, who: CurrentPrincipal) -> dict[str, Any]:
    """The finished run: answer, citations, conflicts and the full step trace.

    This is what makes an answer auditable months later -- it reads the persisted
    run rather than re-executing anything.
    """
    run = fetch_one(
        conn,
        """
        SELECT run_id, status, query, answer_json, refusal_reason, error,
               prompt_tokens, completion_tokens, index_version_id, action_notice,
               started_at, finished_at, account_id, role
        FROM runs WHERE run_id = %s
        """,
        (run_id,),
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such run")

    steps = fetch_all(
        conn,
        """
        SELECT seq, kind, label, detail, duration_ms, started_at
        FROM run_steps WHERE run_id = %s ORDER BY seq
        """,
        (run_id,),
    )
    candidates = fetch_all(
        conn,
        """
        SELECT rc.chunk_id, rc.document_id, rc.lexical_rank, rc.dense_rank,
               rc.fused_score, rc.selected, d.filename, d.title, c.section_path
        FROM retrieval_candidates rc
        JOIN documents d ON d.document_id = rc.document_id
        JOIN chunks c ON c.chunk_id = rc.chunk_id
        WHERE rc.run_id = %s
        ORDER BY rc.fused_score DESC
        """,
        (run_id,),
    )

    # The approval this run proposed, if any. Summary and justification only --
    # the payload is deliberately not echoed to the client, because confirming
    # takes an id and nothing else.
    pending = fetch_one(
        conn,
        """
        SELECT action_id, action_type, account_id, summary, status,
               expires_at, justification, origin
        FROM pending_actions
        WHERE run_id = %s AND status = 'pending' AND expires_at > now()
        ORDER BY prepared_at DESC LIMIT 1
        """,
        (run_id,),
    )

    return {
        "run": run,
        "pending_action": pending,
        "steps": steps,
        # What was CONSIDERED, not only what was cited. This is the panel that
        # answers "why didn't it find the contract clause".
        "retrieval_candidates": candidates,
    }


@router.get("/{run_id}/citation/{chunk_id}")
async def get_citation(
    run_id: UUID, chunk_id: UUID, conn: ReadOnlyConn, who: CurrentPrincipal
) -> dict[str, Any]:
    """The full source behind a citation chip.

    Constrained to chunks this run actually retrieved, so the endpoint cannot be
    used to walk the corpus by guessing chunk ids -- and RLS still applies on top.
    """
    row = fetch_one(
        conn,
        """
        SELECT c.chunk_id, c.text, c.section_path, c.page_from,
               d.filename, d.title, d.source_class, d.authority, d.eligibility,
               d.freshness, d.version_label, d.owner_account_id
        FROM retrieval_candidates rc
        JOIN chunks c ON c.chunk_id = rc.chunk_id
        JOIN documents d ON d.document_id = c.document_id
        WHERE rc.run_id = %s AND rc.chunk_id = %s
        """,
        (run_id, chunk_id),
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="that source was not used by this run",
        )
    return row


@router.get("")
async def list_runs(conn: ReadOnlyConn, who: CurrentPrincipal) -> list[dict[str, Any]]:
    """Recent runs in the caller's scope. Staff see the tenant; customers see their own."""
    return fetch_all(
        conn,
        """
        SELECT run_id, status, query, refusal_reason, account_id, role,
               started_at, finished_at, prompt_tokens, completion_tokens
        FROM runs ORDER BY started_at DESC LIMIT 50
        """,
    )


def _summary_or_error(response) -> dict[str, Any]:
    try:
        return summarise(response)
    except ParcelPilotError as exc:  # pragma: no cover - defensive
        raise http_error(exc) from exc


__all__ = ["router", "Principal", "Depends"]
