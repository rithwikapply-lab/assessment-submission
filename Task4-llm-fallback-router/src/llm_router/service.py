import asyncio
import json
import logging
import math
import sys
import time
from dataclasses import asdict
from typing import Any, Dict, Optional, Tuple

from llm_router.config import Settings
from llm_router.errors import GatewayError
from llm_router.limiter import RateLimited, RequestTooLarge, Reservation, SlidingWindowLimiter
from llm_router.providers import ProviderRequest
from llm_router.router import ModelRouter
from llm_router.storage import SqliteStore
from llm_router.tenants import get_tenant_by_key
from llm_router.tokens import estimate_request_tokens, estimate_tokens

logger = logging.getLogger("llm_router.service")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class GatewayService:
    def __init__(
        self,
        store: SqliteStore,
        limiter: SlidingWindowLimiter,
        router: ModelRouter,
        config: Settings
    ):
        self.store = store
        self.limiter = limiter
        self.router = router
        self.config = config

    async def handle(self, api_key: Optional[str], body: Any, request_id: str) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        start_time = time.perf_counter()
        tenant_id: Optional[str] = None
        attempts_log = []
        tokens_reserved = 0
        tokens_actual = None
        outcome = "unknown"

        reservation: Optional[Reservation] = None
        committed = False

        try:
            # 1. Authenticate via tenants
            if not api_key or not isinstance(api_key, str) or not api_key.strip():
                outcome = "unauthorized"
                raise GatewayError("unauthorized", 401)

            # Strip Bearer if present
            clean_key = api_key.strip()
            if clean_key.lower().startswith("bearer"):
                remainder = clean_key[6:]
                if not remainder or remainder[0].isspace():
                    clean_key = remainder.strip()

            if not clean_key:
                outcome = "unauthorized"
                raise GatewayError("unauthorized", 401)

            tenant = await get_tenant_by_key(self.store, clean_key)
            if not tenant:
                outcome = "unauthorized"
                raise GatewayError("unauthorized", 401)

            tenant_id = tenant["tenant_id"]
            limit = tenant.get("tokens_per_minute", self.config.rate_limit_tokens_per_minute)

            # 2. Validate request
            if not isinstance(body, dict):
                outcome = "invalid_request"
                raise GatewayError("invalid_request", 400)

            messages = body.get("messages")
            if not isinstance(messages, list) or len(messages) == 0:
                outcome = "invalid_request"
                raise GatewayError("invalid_request", 400)

            for m in messages:
                if not isinstance(m, dict):
                    outcome = "invalid_request"
                    raise GatewayError("invalid_request", 400)
                if "role" not in m or "content" not in m:
                    outcome = "invalid_request"
                    raise GatewayError("invalid_request", 400)
                if not isinstance(m["role"], str) or not isinstance(m["content"], str):
                    outcome = "invalid_request"
                    raise GatewayError("invalid_request", 400)

            # Check stream parameter
            if "stream" in body and body["stream"] is not False and body["stream"] is not None:
                outcome = "invalid_request"
                raise GatewayError("invalid_request", 400)

            # Check max_tokens
            max_tokens_val = body.get("max_tokens")
            if max_tokens_val is None:
                max_tokens = self.config.default_max_tokens
            else:
                if isinstance(max_tokens_val, bool) or not isinstance(max_tokens_val, int):
                    outcome = "invalid_request"
                    raise GatewayError("invalid_request", 400)
                if max_tokens_val < 1 or max_tokens_val > self.config.max_allowed_max_tokens:
                    outcome = "invalid_request"
                    raise GatewayError("invalid_request", 400)
                max_tokens = max_tokens_val

            # 3. Estimate and reserve
            estimated_tokens = estimate_request_tokens(messages, max_tokens)
            tokens_reserved = estimated_tokens

            try:
                reservation = await self.limiter.reserve(tenant_id, estimated_tokens, limit)
            except RateLimited as e:
                outcome = "rate_limit_exceeded"
                retry_after_s = math.ceil(e.retry_after_ms / 1000)
                raise GatewayError("rate_limit_exceeded", 429, retry_after_s=retry_after_s)
            except RequestTooLarge:
                outcome = "request_too_large"
                raise GatewayError("request_too_large", 413)

            # 4. Route
            provider_req = ProviderRequest(
                messages=messages,
                model=body.get("model"),
                max_tokens=max_tokens,
                temperature=body.get("temperature"),
                extra={k: v for k, v in body.items() if k not in ("messages", "model", "max_tokens", "temperature", "stream")}
            )
            route_result = await self.router.route(provider_req)
            attempts_log = [asdict(a) for a in route_result.attempts]

            # Determine actual tokens
            resp_body = route_result.body
            if isinstance(resp_body, dict) and "usage" in resp_body and isinstance(resp_body["usage"], dict):
                tot = resp_body["usage"].get("total_tokens")
                if isinstance(tot, int) and tot >= 0:
                    tokens_actual = tot

            if tokens_actual is None:
                prompt_tokens = sum(estimate_tokens(m["content"]) + 4 for m in messages)
                resp_text = ""
                if isinstance(resp_body, dict) and "choices" in resp_body and isinstance(resp_body["choices"], list):
                    for c in resp_body["choices"]:
                        if isinstance(c, dict):
                            msg = c.get("message")
                            if isinstance(msg, dict) and "content" in msg and isinstance(msg["content"], str):
                                resp_text += msg["content"]
                tokens_actual = prompt_tokens + estimate_tokens(resp_text)

            await self.limiter.commit(reservation, tokens_actual)
            committed = True
            outcome = "success"

            # Query remaining tokens
            snap = await self.limiter.usage(tenant_id, limit)

            headers = {
                "X-Request-Id": request_id,
                "X-RateLimit-Limit": str(snap.limit),
                "X-RateLimit-Remaining": str(snap.remaining),
                "X-Gateway-Fallback": "true" if route_result.fallback_used else "false"
            }

            return 200, resp_body, headers

        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except GatewayError as ge:
            outcome = ge.code
            raise
        except Exception as e:
            outcome = "internal_error"
            logger.error(
                f"Unexpected error [request_id={request_id}]: {e}",
                exc_info=True
            )
            raise GatewayError("internal_error", 500) from e
        finally:
            if reservation is not None and not committed:
                try:
                    await asyncio.shield(self.limiter.release(reservation))
                except (Exception, asyncio.CancelledError):
                    pass

            total_ms = (time.perf_counter() - start_time) * 1000.0
            log_entry = {
                "request_id": request_id,
                "tenant_id": tenant_id,
                "outcome": outcome,
                "attempts": attempts_log,
                "tokens_reserved": tokens_reserved,
                "tokens_actual": tokens_actual,
                "total_ms": round(total_ms, 2)
            }
            logger.info(json.dumps(log_entry))
