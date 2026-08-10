"""FastAPI entry point.

Runs the REST control plane and, unless ``RUN_ARI_IN_API=false``, the ARI
Stasis application in the same process. Both share the business-logic layer, so
splitting them into two containers later is a deployment change only.

Typical usage example:

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import fastapi
from fastapi import exceptions as fastapi_exceptions
from fastapi import responses

import app as app_package
from app import background
from app.api.routers import calls, health, numbers, sessions
from app.ari import client as ari_client
from app.ari import stasis
from app.core import config, errors, logging_config, phone
from app.db import cache, engine
from app.services import webhooks

log = logging_config.get_logger(__name__)

_API_PREFIX = "/api/v1"


@contextlib.asynccontextmanager
async def lifespan(api: fastapi.FastAPI) -> AsyncIterator[None]:
    """Starts and stops everything the process owns.

    Args:
        api: The application whose state holds the ARI objects.

    Yields:
        Nothing; control returns to the server while the app serves requests.
    """
    settings = config.get_settings()
    logging_config.configure_logging(settings.log_level, settings.log_json)

    for problem in settings.validate_production_secrets():
        log.warning("config.insecure_default", problem=problem)

    engine.init_engine(settings)
    cache.init_cache(settings)

    tasks: list[asyncio.Task] = [
        asyncio.create_task(background.expiry_loop(settings), name="expiry"),
        asyncio.create_task(
            background.retention_loop(settings), name="retention"
        ),
    ]

    if settings.run_ari_in_api:
        stasis_app = stasis.MaskingStasisApp(settings)
        api.state.stasis_app = stasis_app
        api.state.ari_client = stasis_app.client
        tasks.append(asyncio.create_task(stasis_app.run(), name="stasis"))
    else:
        api.state.stasis_app = None
        api.state.ari_client = ari_client.ARIClient(settings)

    log.info(
        "service.started",
        version=app_package.__version__,
        ari_in_process=settings.run_ari_in_api,
        ext_code_mode=settings.ext_code_mode,
        cooldown_scope=settings.cooldown_scope,
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if api.state.stasis_app is not None:
            await api.state.stasis_app.stop()
        elif api.state.ari_client is not None:
            await api.state.ari_client.aclose()

        await webhooks.drain()
        await cache.close_cache()
        await engine.dispose_engine()
        log.info("service.stopped")


def create_app() -> fastapi.FastAPI:
    """Builds the FastAPI application with its middleware and routers."""
    settings = config.get_settings()
    logging_config.configure_logging(settings.log_level, settings.log_json)

    api = fastapi.FastAPI(
        title="Number masking service",
        version=app_package.__version__,
        description=(
            "Proxy-number voice masking: two parties talk through a DID"
            " without learning each other's real number."
        ),
        lifespan=lifespan,
        openapi_url=f"{_API_PREFIX}/openapi.json",
        docs_url=f"{_API_PREFIX}/docs",
    )

    @api.middleware("http")
    async def request_context(
        request: fastapi.Request,
        call_next: Callable[[fastapi.Request], Awaitable[fastapi.Response]],
    ) -> fastapi.Response:
        """Binds a trace id, enforces the body limit and logs the request."""
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        logging_config.bind_trace_id(trace_id)

        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > settings.api_max_body_bytes:
            return _error_response(
                errors.PayloadTooLargeError("request body is too large")
            )

        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        log.info(
            "http.request",
            method=request.method,
            path=phone.scrub_text(request.url.path),
            status_code=response.status_code,
        )
        return response

    @api.exception_handler(errors.DomainError)
    async def domain_error_handler(
        _request: fastapi.Request, exc: errors.DomainError
    ) -> responses.JSONResponse:
        """Renders a domain error as its stable code and message."""
        return _error_response(exc)

    @api.exception_handler(fastapi_exceptions.RequestValidationError)
    async def validation_handler(
        _request: fastapi.Request,
        exc: fastapi_exceptions.RequestValidationError,
    ) -> responses.JSONResponse:
        """Reports which fields failed, never the values they carried."""
        fields = [
            ".".join(str(part) for part in error.get("loc", ()))
            for error in exc.errors()
        ]
        return responses.JSONResponse(
            status_code=422,
            content={
                "error": "validation_failed",
                "message": f"invalid fields: {', '.join(fields)}",
                "trace_id": logging_config.get_trace_id(),
            },
        )

    @api.exception_handler(ValueError)
    async def value_error_handler(
        _request: fastapi.Request, exc: ValueError
    ) -> responses.JSONResponse:
        """Renders a validation ValueError with a scrubbed message."""
        return responses.JSONResponse(
            status_code=422,
            content={
                "error": "validation_failed",
                "message": phone.scrub_text(str(exc)),
                "trace_id": logging_config.get_trace_id(),
            },
        )

    api.include_router(sessions.router, prefix=_API_PREFIX)
    api.include_router(calls.router, prefix=_API_PREFIX)
    api.include_router(numbers.router, prefix=_API_PREFIX)
    api.include_router(health.router)
    api.include_router(
        health.router, prefix=_API_PREFIX, include_in_schema=False
    )
    return api


def _error_response(exc: errors.DomainError) -> responses.JSONResponse:
    """Builds the JSON body of an error response."""
    return responses.JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.code,
            "message": phone.scrub_text(exc.message),
            "trace_id": logging_config.get_trace_id(),
        },
    )


app = create_app()


def main() -> None:
    """Runs the API with uvicorn, for the ``masking-api`` console script."""
    import uvicorn

    settings = config.get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
