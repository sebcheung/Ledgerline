"""A configurable-failure-mode webhook receiver (SPEC.md §10), used by the
fault suite to exercise `ledger.webhooks.dispatcher`'s exception/status
mapping against a real socket.

Signature verification happens for *every* mode, before the failure mode is
applied, and the outcome is always recorded in `state.received` -- so a
fault test can assert the dispatcher signed a request correctly even on an
attempt the receiver then answered with a 500. An invalid signature always
short-circuits to 401, regardless of the configured mode; this is what
makes "verifies HMAC signatures" a real assertion, not just a comment.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum

from fastapi import FastAPI, Request
from starlette.responses import Response, StreamingResponse

from ledger.webhooks.signing import verify


class FailureMode(StrEnum):
    OK = "ok"
    STATUS = "status"
    TIMEOUT = "timeout"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class ReceivedRequest:
    event_id: str | None
    signature_valid: bool
    body: bytes


@dataclass
class ReceiverState:
    """Mutated directly by the test between dispatcher cycles -- e.g. flip
    `mode` from `STATUS` to `OK` to simulate "the receiver recovered",
    with no sleeping on either side."""

    secret: str = ""
    mode: FailureMode = FailureMode.OK
    status_code: int = 500
    #: Only used by TIMEOUT mode. Kept short in tests (well under the
    #: dispatcher's configured read timeout) so a timeout test costs
    #: fractions of a second, not the real 5s default.
    delay_seconds: float = 0.2
    received: list[ReceivedRequest] = field(default_factory=list)


def create_receiver_app(state: ReceiverState) -> FastAPI:
    app = FastAPI()

    @app.post("/hook")
    async def hook(request: Request) -> Response:
        body = await request.body()
        event_id = request.headers.get("X-Ledgerline-Event-Id")
        signature = request.headers.get("X-Ledgerline-Signature", "")
        try:
            timestamp = int(request.headers.get("X-Ledgerline-Timestamp", ""))
        except ValueError:
            timestamp = 0

        signature_valid = verify(state.secret, timestamp, body, signature)
        state.received.append(
            ReceivedRequest(event_id=event_id, signature_valid=signature_valid, body=body)
        )
        if not signature_valid:
            return Response(status_code=401)

        if state.mode is FailureMode.OK:
            return Response(status_code=200)
        if state.mode is FailureMode.STATUS:
            return Response(status_code=state.status_code)
        if state.mode is FailureMode.TIMEOUT:
            await asyncio.sleep(state.delay_seconds)
            return Response(status_code=200)
        # RESET: drop the connection after the response has started, which
        # uvicorn surfaces to the client as a genuine transport-level
        # failure (httpx.RemoteProtocolError / ConnectError), not a
        # simulated one.
        return StreamingResponse(_reset_connection())

    return app


async def _reset_connection() -> AsyncIterator[bytes]:
    yield b""
    raise ConnectionResetError("simulated connection reset")
