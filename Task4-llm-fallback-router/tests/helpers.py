import asyncio
from typing import Optional, Dict, Any, List
from llm_router.providers import ProviderRequest, ProviderResult, ProviderConnectionError

_DEFAULT_BODY = object()

class FakeProvider:
    def __init__(
        self,
        name: str = "fake",
        status: int = 200,
        body: Any = _DEFAULT_BODY,
        delay: float = 0.0,
        exception: Optional[Exception] = None,
        retry_after_s: Optional[float] = None,
        ignore_cancel: bool = False
    ):
        self.name = name
        self.status = status
        if body is _DEFAULT_BODY:
            self.body = {
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"total_tokens": 10}
            }
        else:
            self.body = body
        self.delay = delay
        self.exception = exception
        self.retry_after_s = retry_after_s
        self.ignore_cancel = ignore_cancel
        self.call_count = 0
        self.cancelled = False
        self.calls: List[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.call_count += 1
        self.calls.append(request)
        if self.delay > 0:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                if self.ignore_cancel:
                    await asyncio.sleep(0.5)
                raise

        if self.exception:
            raise self.exception

        return ProviderResult(
            status=self.status,
            body=self.body,
            retry_after_s=self.retry_after_s
        )


def mp_reserve_worker(db_path: str, tenant_id: str, limit: int, attempts: int, tokens: int, out_queue):
    import asyncio
    from llm_router.storage import SqliteStore
    from llm_router.clock import SystemClock
    from llm_router.limiter import SlidingWindowLimiter, RateLimited

    async def _run():
        store = SqliteStore(db_path)
        await store.init()
        clock = SystemClock()
        limiter = SlidingWindowLimiter(store, clock, default_limit=limit)
        admitted = 0
        rate_limited = 0
        for _ in range(attempts):
            try:
                await limiter.reserve(tenant_id, tokens, limit=limit)
                admitted += 1
            except RateLimited:
                rate_limited += 1
        await store.aclose()
        return admitted, rate_limited

    res = asyncio.run(_run())
    out_queue.put(res)
