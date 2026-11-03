# LLM Fallback Router

A high-performance rate-limiting and model-fallback gateway service for Large Language Model APIs. Built with Python 3.11, FastAPI, httpx, asyncio, and on-disk SQLite3 with WAL mode.

## Architecture

```
                    +------------------------------------+
                    |        Client Application          |
                    +------------------------------------+
                                      |
                                      | HTTP POST /v1/chat/completions
                                      v
                    +------------------------------------+
                    |       Pure ASGI Middleware         |
                    |   (Generates & Injects X-Req-Id)   |
                    +------------------------------------+
                                      |
                                      v
                    +------------------------------------+
                    |    FastAPI Routing & Exception     |
                    |   Handlers (Strict Sanitization)   |
                    +------------------------------------+
                                      |
                                      v
                    +------------------------------------+
                    |          Gateway Service           |
                    | (Auth via Tenants, Token Estimate) |
                    +------------------------------------+
                                      |
               +----------------------+----------------------+
               |                                             |
               v                                             v
+-------------------------------+             +-------------------------------+
|     SlidingWindowLimiter      |             |          ModelRouter          |
|  (reserve, commit, release)   |             | (Primary / Secondary Timeout) |
+-------------------------------+             +-------------------------------+
               |                                             |
               v                                             v
+-------------------------------+             +-------------------------------+
|     SqliteStore (WAL Mode)    |             |       Upstream Providers      |
|  (Immediate TX, Busy Timeout) |             |  (Primary -> Fallback Retry)  |
+-------------------------------+             +-------------------------------+
```

## Core Design & Concurrency Semantics

### Sliding-Window Log: Reserve, Commit, Release
The limiter manages token capacity using an exact transaction log stored in SQLite:
- `reserve(tenant_id, estimated_tokens, limit)`: Runs inside an atomic `BEGIN IMMEDIATE` transaction. Opportunistically deletes expired usage rows (`ts_ms <= now - window_ms`). Calculates current tenant consumption within the window. If `used + estimated_tokens <= limit`, it inserts a reservation row with `state = 'reserved'`.
- `commit(reservation, actual_tokens)`: Reconciles token usage once the upstream provider returns. Updates the reserved row with the actual token count from provider usage metrics and sets `state = 'committed'`. If the upstream response lacks usage statistics, fallback heuristic token estimation is applied.
- `release(reservation)`: Triggered on upstream failure, timeout, or client cancellation. Deletes the `'reserved'` entry, refunding reserved capacity.

### Boundary Semantics
The time window operates with strict inequality on active rows:
- Rows where `ts_ms <= now - window_ms` are expired and excluded from token sums.
- Rows where `ts_ms > now - window_ms` are active.
- A row timestamped exactly at `now - window_ms` is expired.

### The Retry-After Walk
When a reservation exceeds the tenant limit, the required capacity is computed as:
`need = (used + estimated_tokens) - limit`
The limiter queries active records in chronological order (`ORDER BY ts_ms ASC, id ASC`) and accumulates tokens until `accumulated >= need`. The timestamp of that row determines when sufficient capacity will free up:
`retry_at = row.ts_ms + window_ms`
`retry_after_ms = max(1, retry_at - now)`

### Overdraft Handling
Reservations are evaluated against token estimates (`prompt_tokens + max_tokens`). Because actual tokens can differ from estimates, a tenant may briefly consume tokens beyond the nominal limit. Subsequent reservation attempts will immediately observe the updated cumulative total and be rejected until older usage rolls past the window boundary.

### Crash-Safe Expiry
The system does not rely on transient in-memory locks. Uncommitted reservations automatically become harmless over time:
1. Every reservation or query transaction opportunistically evicts expired rows.
2. A background task periodically purges expired rows via `limiter.purge_expired()`.
3. If an application instance terminates mid-flight, uncommitted reservations naturally expire as wall-clock time progresses.

### Failover Rules & Task Lifecycle
ModelRouter dispatches requests first to the primary provider with a deadline (`primary_timeout_ms`).
Failover to the secondary provider is evaluated using explicit rules:
- **Failover conditions**:
  - Primary request times out (`timeout`).
  - Primary encounters network connection errors (`connection_error`).
  - Primary returns an HTTP status code in `failover_statuses`: `429`, `500`, `502`, `503`, or `504`.
  - Primary returns a `2xx` HTTP status code but with an unreadable or missing response body (`body is None`).
- **Client-error conditions (no failover)**:
  - Primary returns HTTP status `400` or `422`, which immediately raises `invalid_request` (HTTP 400).
- **Unknown outcomes (no failover)**:
  - Any other unknown outcome (e.g. redirect statuses like `302`, upstream authentication failures like `401`/`403`/`404`, unsupported statuses like `501`, or unhandled provider exceptions) immediately raises `upstream_unavailable` (HTTP 502) without failing over to the secondary provider.

#### Why `asyncio.wait` + `cancel()` + Abandoned Set instead of `wait_for`
Using `asyncio.wait_for` introduces race conditions near deadlines where tasks completing near timeout boundaries can trigger unhandled cancellation exceptions. Additionally, if an upstream provider does not immediately yield upon receiving `CancelledError`, `wait_for` will block caller execution.

To guarantee bounded latency:
1. `asyncio.wait({task}, timeout=deadline)` executes the attempt.
2. If the task does not finish within the deadline, `task.cancel()` is called immediately.
3. The pending task is moved into an internal `_abandoned` set with a callback that discards it upon termination.
4. Failover to the secondary provider proceeds without waiting for the abandoned primary task.
5. All abandoned tasks are cancelled and awaited during `aclose()`.

## Standardized Error Handling

All error responses strictly adhere to a unified payload structure:
```json
{
  "error": {
    "type": "gateway_error",
    "code": "rate_limit_exceeded",
    "message": "Rate limit exceeded. Please retry later.",
    "request_id": "7b0965a5-d7ab-47eb-9925-ba353fc65321",
    "retry_after_seconds": 60
  }
}
```

### Error Code Mapping

| Error Code | HTTP Status | Fixed Error Message |
|---|---|---|
| `invalid_request` | 400 | Invalid request payload or parameters. |
| `unauthorized` | 401 | Invalid or missing API key. |
| `request_too_large` | 413 | Requested token count exceeds maximum allowed. |
| `rate_limit_exceeded` | 429 | Rate limit exceeded. Please retry later. |
| `upstream_unavailable` | 502 | Upstream service is currently unavailable. |
| `upstream_timeout` | 504 | Upstream request timed out. |
| `internal_error` | 500 | An internal server error occurred. |
| `not_found` | 404 | The requested resource was not found. |
| `method_not_allowed` | 405 | Method not allowed for the requested resource. |

Internal details, provider URLs, model names, raw database errors, file paths, and stack traces are strictly sanitized and never returned to clients.

## How to Run

### 1. Environment Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

### 2. Tenant Management CLI
Tenants are authenticated using SHA-256 hashed API keys.

```bash
# Add or update a tenant
python3 -m llm_router.admin add-tenant --id tenant-alpha --key sk-demo-key-123 --limit 50000

# List existing tenants
python3 -m llm_router.admin list-tenants
```

### 3. Running the Server
The application uses the `create_app(settings=None)` factory, which loads configuration from environment variables via `load_settings_from_env()` when settings are not explicitly provided. Note that the module-level `app` instance has been removed; run the server using uvicorn's `--factory` flag:

```bash
uvicorn llm_router.app:create_app --factory --host 0.0.0.0 --port 8000
```

### Configuration Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_DB_PATH` | `./data/gateway.db` | Filepath for SQLite database |
| `RATE_LIMIT_TOKENS_PER_MINUTE` | `50000` | Default tenant rate limit (tokens/min) |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Duration of rate limiting sliding window |
| `PRIMARY_URL` | `http://localhost:8001/v1` | Primary upstream endpoint |
| `PRIMARY_API_KEY` | `""` | Primary upstream authorization key |
| `PRIMARY_MODEL` | `gpt-4o` | Primary default model name |
| `SECONDARY_URL` | `http://localhost:8002/v1` | Secondary upstream endpoint |
| `SECONDARY_API_KEY` | `""` | Secondary upstream authorization key |
| `SECONDARY_MODEL` | `gpt-4o-mini` | Secondary default model name |
| `PRIMARY_TIMEOUT_MS` | `3000` | Primary upstream timeout in milliseconds |
| `SECONDARY_TIMEOUT_MS` | `3000` | Secondary upstream timeout in milliseconds |
| `DEFAULT_MAX_TOKENS` | `256` | Default max tokens if omitted |
| `MAX_ALLOWED_MAX_TOKENS` | `8192` | Upper bound for max_tokens request parameter |
| `PURGE_INTERVAL_SECONDS` | `30` | Background task interval for expired row cleanup |

## Testing

Run the test suite using pytest:
```bash
pytest -q
```

The test suite covers:
- Unit tests for token estimation, monotonic clock, and storage engines.
- Sliding-window reservation, commit, release, and opportunistic eviction.
- Model router failover, cancellation isolation, and deadline-race properties.
- Full HTTP lifecycle with pure ASGI middleware, header verification, and cancellation propagation.
- Error sanitization ensuring no upstream or internal leaks.
- High-concurrency `asyncio.gather` and multi-process concurrency verification.

## Limitations

- **Local Test Upstreams**: The end-to-end integration test uses local fake HTTP servers (built on Python's standard library `http.server`), not live commercial provider endpoints.
- **Single Provider Pair**: Supports a primary and secondary provider pair; arbitrary provider DAGs or dynamic tiers are not supported.
- **No Streaming**: Responses are buffered and returned in full; `stream: true` requests are rejected with status 400.
- **Approximate Token Estimation**: Pre-request reservation relies on `ceil(len(text) / 4)` heuristics.
- **Estimates Can Overdraft**: Large reservations can briefly consume capacity above the window limit before committing.
- **No Circuit Breaker**: Does not maintain temporary cooldowns after repeated 429s; failover is re-evaluated per request.
- **Wall-Clock Dependence**: Multi-process rate-limiting relies on system clock synchronization.
- **SQLite Write Throughput**: Serialized writes via `BEGIN IMMEDIATE` bound aggregate write throughput compared to distributed caches.
- **No Key Rotation**: Tenants map one key hash to one tenant record.
- **In-Process Thread Executor**: SQLite queries run via a single-worker `ThreadPoolExecutor` per process instance.
