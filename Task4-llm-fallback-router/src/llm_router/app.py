import asyncio
from contextlib import asynccontextmanager
import json
import logging
import math
import sys
from typing import Optional, Tuple
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
import httpx

from llm_router.clock import SystemClock
from llm_router.config import Settings, load_settings_from_env
from llm_router.errors import ERROR_STATUS_CODES, GatewayError, build_error_payload
from llm_router.limiter import SlidingWindowLimiter
from llm_router.providers import HttpProvider, Provider
from llm_router.router import ModelRouter
from llm_router.service import GatewayService
from llm_router.storage import SqliteStore
from llm_router.tenants import get_tenant_by_key

logger = logging.getLogger("llm_router.app")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class RequestIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        req_id = str(uuid.uuid4())
        state["request_id"] = req_id

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"x-request-id" for k, v in headers):
                    headers.append((b"x-request-id", req_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def _ensure_initialized(app: FastAPI):
    if getattr(app.state, "service", None) is not None:
        return

    settings: Settings = getattr(app.state, "settings", None) or Settings()
    store = getattr(app.state, "store", None)
    if store is None:
        store = SqliteStore(settings.gateway_db_path)
        await store.init()
        app.state.store = store

    limiter = getattr(app.state, "limiter", None)
    if limiter is None:
        clock = SystemClock()
        limiter = SlidingWindowLimiter(
            store,
            clock,
            default_limit=settings.rate_limit_tokens_per_minute,
            window_ms=settings.rate_limit_window_seconds * 1000
        )
        app.state.limiter = limiter

    router = getattr(app.state, "router", None)
    if router is None:
        injected = getattr(app.state, "injected_providers", None)
        if injected is not None:
            primary, secondary = injected
        else:
            client = httpx.AsyncClient(timeout=None)
            primary = HttpProvider("primary", settings.primary_url, settings.primary_api_key, settings.primary_model, client=client)
            secondary = HttpProvider("secondary", settings.secondary_url, settings.secondary_api_key, settings.secondary_model, client=client)
            app.state.client = client
        router = ModelRouter(primary, secondary, settings)
        app.state.router = router

    service = getattr(app.state, "service", None)
    if service is None:
        service = GatewayService(store, limiter, router, settings)
        app.state.service = service


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = getattr(app.state, "settings", None) or Settings()
    await _ensure_initialized(app)

    store: SqliteStore = app.state.store
    limiter: SlidingWindowLimiter = app.state.limiter
    router: ModelRouter = app.state.router
    client: Optional[httpx.AsyncClient] = getattr(app.state, "client", None)

    async def _purge_loop():
        try:
            while True:
                await asyncio.sleep(settings.purge_interval_seconds)
                await limiter.purge_expired()
        except asyncio.CancelledError:
            pass

    purge_task = asyncio.create_task(_purge_loop())
    app.state.purge_task = purge_task

    try:
        yield
    finally:
        purge_task.cancel()
        try:
            await purge_task
        except asyncio.CancelledError:
            pass

        if client is not None:
            await client.aclose()
        await router.aclose()
        await store.aclose()


def create_app(
    settings: Optional[Settings] = None,
    providers: Optional[Tuple[Provider, Provider]] = None
) -> FastAPI:
    if settings is None:
        settings = load_settings_from_env()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.injected_providers = providers
    app.state.client = None
    app.state.store = None
    app.state.limiter = None
    app.state.router = None
    app.state.service = None

    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        req_id = request.scope.get("state", {}).get("request_id") or str(uuid.uuid4())
        headers = {"X-Request-Id": req_id}
        if exc.retry_after_s is not None:
            headers["Retry-After"] = str(exc.retry_after_s)
        payload = build_error_payload(exc.code, req_id, exc.retry_after_s)
        return JSONResponse(
            status_code=exc.http_status,
            content=payload,
            headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        req_id = request.scope.get("state", {}).get("request_id") or str(uuid.uuid4())
        payload = build_error_payload("invalid_request", req_id)
        return JSONResponse(
            status_code=400,
            content=payload,
            headers={"X-Request-Id": req_id}
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        req_id = request.scope.get("state", {}).get("request_id") or str(uuid.uuid4())
        if exc.status_code == 404:
            code = "not_found"
        elif exc.status_code == 405:
            code = "method_not_allowed"
        elif exc.status_code == 400:
            code = "invalid_request"
        elif exc.status_code == 401:
            code = "unauthorized"
        elif exc.status_code == 413:
            code = "request_too_large"
        elif exc.status_code == 429:
            code = "rate_limit_exceeded"
        else:
            code = "internal_error"

        payload = build_error_payload(code, req_id)
        return JSONResponse(
            status_code=exc.status_code if exc.status_code in (404, 405) else ERROR_STATUS_CODES.get(code, 500),
            content=payload,
            headers={"X-Request-Id": req_id}
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        req_id = request.scope.get("state", {}).get("request_id") or str(uuid.uuid4())
        logger.error(
            f"Unhandled exception [request_id={req_id}]: {exc}",
            exc_info=True
        )
        payload = build_error_payload("internal_error", req_id)
        return JSONResponse(
            status_code=500,
            content=payload,
            headers={"X-Request-Id": req_id}
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/v1/usage")
    async def get_usage(request: Request):
        await _ensure_initialized(app)
        auth = request.headers.get("Authorization")
        if not auth or not isinstance(auth, str) or not auth.strip():
            raise GatewayError("unauthorized", 401)
        clean_key = auth.strip()
        if clean_key.lower().startswith("bearer"):
            remainder = clean_key[6:]
            if not remainder or remainder[0].isspace():
                clean_key = remainder.strip()
        if not clean_key:
            raise GatewayError("unauthorized", 401)

        tenant = await get_tenant_by_key(app.state.store, clean_key)
        if not tenant:
            raise GatewayError("unauthorized", 401)

        limit = tenant.get("tokens_per_minute", app.state.settings.rate_limit_tokens_per_minute)
        snapshot = await app.state.limiter.usage(tenant["tenant_id"], limit)
        reset_after_s = math.ceil(snapshot.reset_after_ms / 1000)
        return JSONResponse(
            content={
                "used": snapshot.used,
                "limit": snapshot.limit,
                "remaining": snapshot.remaining,
                "reset_after_seconds": reset_after_s
            }
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        await _ensure_initialized(app)
        req_id = request.scope.get("state", {}).get("request_id") or str(uuid.uuid4())
        auth = request.headers.get("Authorization")

        try:
            raw_body = await request.body()
            if not raw_body:
                raise GatewayError("invalid_request", 400)
            body = json.loads(raw_body)
            if not isinstance(body, dict):
                raise GatewayError("invalid_request", 400)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise GatewayError("invalid_request", 400)

        status, resp_body, resp_headers = await app.state.service.handle(auth, body, req_id)
        return Response(
            content=json.dumps(resp_body),
            status_code=status,
            media_type="application/json",
            headers=resp_headers
        )

    return app

