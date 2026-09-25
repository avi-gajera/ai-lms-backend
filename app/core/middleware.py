"""ASGI middleware.

`BodySizeLimitMiddleware` enforces the upload limit *while the body streams in*. Starlette parses a
multipart form (spooling the file to a temp file) before the endpoint runs, so the size check in
`POST /videos` alone would only fire after an oversized upload had already been written to disk.
"""

from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import error_envelope


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Reject request bodies larger than `max_bytes()` with 413.

    - A declared `Content-Length` over the limit is rejected before any of the body is read.
    - A chunked body (no length) is counted as it arrives and cut off once it crosses the limit;
      whatever response the app produced for the aborted read is replaced by the 413.
    """

    def __init__(self, app: ASGIApp, max_bytes: Callable[[], int]):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.max_bytes()
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send, limit)
            return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                return  # drop the app's response to the aborted body read; the 413 goes out instead
            response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass
        if exceeded and not response_started:
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        response = JSONResponse(
            status_code=413,
            content=error_envelope("payload_too_large", "Request body is too large", details={"max_bytes": limit}),
        )
        await response(scope, receive, send)
