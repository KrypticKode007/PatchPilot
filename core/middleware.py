from __future__ import annotations

import logging
import uuid

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .security import sanitize_error_for_client

logger = logging.getLogger("patchpilot")


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id

        logger.info("request_id=%s method=%s path=%s", request_id, request.method, request.url.path)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled exception on request_id=%s", request_id)
    return JSONResponse(
        status_code=500,
        content={
            "detail": sanitize_error_for_client(exc),
            "request_id": request_id,
        },
    )