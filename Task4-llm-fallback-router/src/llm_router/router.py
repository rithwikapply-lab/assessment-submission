import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Optional, Set
from llm_router.providers import Provider, ProviderRequest, ProviderResult, ProviderConnectionError
from llm_router.errors import GatewayError
from llm_router.config import Settings

@dataclass
class Attempt:
    provider: str
    outcome: str
    status: Optional[int] = None
    latency_ms: float = 0.0

@dataclass
class RouteResult:
    provider_name: str
    body: dict
    attempts: List[Attempt]
    fallback_used: bool

DEFAULT_FAILOVER_STATUSES = {429, 500, 502, 503, 504}

class ModelRouter:
    def __init__(
        self,
        primary: Provider,
        secondary: Provider,
        config: Optional[Settings] = None,
        failover_statuses: Optional[Set[int]] = None
    ):
        self.primary = primary
        self.secondary = secondary
        self.config = config or Settings()
        self.failover_statuses = failover_statuses if failover_statuses is not None else DEFAULT_FAILOVER_STATUSES
        self._abandoned: Set[asyncio.Task] = set()

    async def aclose(self) -> None:
        tasks = list(self._abandoned)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._abandoned.clear()

    async def _execute_attempt(
        self,
        provider: Provider,
        provider_label: str,
        request: ProviderRequest,
        timeout_ms: int
    ) -> tuple[Optional[ProviderResult], Attempt]:
        start = time.perf_counter()
        deadline_s = timeout_ms / 1000.0

        task = asyncio.create_task(provider.complete(request))
        try:
            done, _ = await asyncio.wait({task}, timeout=deadline_s)
        except asyncio.CancelledError:
            task.cancel()
            def _cleanup_on_cancel(t: asyncio.Task):
                try:
                    t.exception()
                except (asyncio.CancelledError, Exception):
                    pass
            task.add_done_callback(_cleanup_on_cancel)
            raise

        duration_ms = (time.perf_counter() - start) * 1000.0

        if task not in done:
            task.cancel()
            def _swallow_exc(t: asyncio.Task):
                try:
                    t.exception()
                except (asyncio.CancelledError, Exception):
                    pass
                self._abandoned.discard(t)
            task.add_done_callback(_swallow_exc)
            self._abandoned.add(task)
            return None, Attempt(provider=provider_label, outcome="timeout", status=None, latency_ms=duration_ms)

        # Task finished within deadline
        try:
            result: ProviderResult = task.result()
            status = result.status
            if 200 <= status < 300:
                return result, Attempt(provider=provider_label, outcome="success", status=status, latency_ms=duration_ms)
            else:
                return result, Attempt(provider=provider_label, outcome=f"http_{status}", status=status, latency_ms=duration_ms)
        except ProviderConnectionError:
            return None, Attempt(provider=provider_label, outcome="connection_error", status=None, latency_ms=duration_ms)
        except Exception:
            return None, Attempt(provider=provider_label, outcome="error", status=None, latency_ms=duration_ms)

    async def route(self, request: ProviderRequest) -> RouteResult:
        attempts: List[Attempt] = []

        # 1. Try Primary
        prim_result, prim_attempt = await self._execute_attempt(
            self.primary, "primary", request, self.config.primary_timeout_ms
        )
        attempts.append(prim_attempt)

        if prim_attempt.outcome == "success" and prim_result and prim_result.body is not None:
            return RouteResult(
                provider_name=self.primary.name,
                body=prim_result.body,
                attempts=attempts,
                fallback_used=False
            )

        # Explicit failover rules:
        # 1. timeout
        # 2. connection error
        # 3. HTTP status in failover_statuses (429, 500, 502, 503, 504)
        # 4. 2xx with unreadable body (body is None)
        # Any other unknown outcome raises upstream_unavailable without failover.
        should_failover = False
        if prim_attempt.outcome in ("timeout", "connection_error"):
            should_failover = True
        elif prim_attempt.status in self.failover_statuses:
            should_failover = True
        elif (prim_attempt.status is not None and 200 <= prim_attempt.status < 300) and (prim_result is None or prim_result.body is None):
            should_failover = True
        elif prim_attempt.status in (400, 422):
            raise GatewayError("invalid_request", 400)
        else:
            raise GatewayError("upstream_unavailable", 502)

        if not should_failover:
            raise GatewayError("upstream_unavailable", 502)

        # 2. Try Secondary
        sec_result, sec_attempt = await self._execute_attempt(
            self.secondary, "secondary", request, self.config.secondary_timeout_ms
        )
        attempts.append(sec_attempt)

        if sec_attempt.outcome == "success" and sec_result and sec_result.body is not None:
            return RouteResult(
                provider_name=self.secondary.name,
                body=sec_result.body,
                attempts=attempts,
                fallback_used=True
            )

        if sec_attempt.status in (400, 422):
            raise GatewayError("invalid_request", 400)

        # Both attempts failed
        if all(a.outcome == "timeout" for a in attempts):
            raise GatewayError("upstream_timeout", 504)
        else:
            raise GatewayError("upstream_unavailable", 502)
