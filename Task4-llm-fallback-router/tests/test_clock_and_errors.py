import time
from llm_router.clock import SystemClock, ManualClock
from llm_router.errors import GatewayError, build_error_payload, ERROR_MESSAGES, ERROR_STATUS_CODES

def test_system_clock():
    clock = SystemClock()
    t1 = clock.now_ms()
    time.sleep(0.01)
    t2 = clock.now_ms()
    assert t2 >= t1

def test_manual_clock():
    clock = ManualClock(1000)
    assert clock.now_ms() == 1000
    clock.advance(500)
    assert clock.now_ms() == 1500
    clock.set(3000)
    assert clock.now_ms() == 3000

def test_gateway_error():
    err = GatewayError("unauthorized")
    assert err.code == "unauthorized"
    assert err.http_status == 401
    assert err.message == ERROR_MESSAGES["unauthorized"]
    assert err.retry_after_s is None

    err2 = GatewayError("rate_limit_exceeded", retry_after_s=5)
    assert err2.http_status == 429
    assert err2.retry_after_s == 5

def test_build_error_payload():
    payload = build_error_payload("rate_limit_exceeded", "req-123", 10)
    assert payload == {
        "error": {
            "type": "gateway_error",
            "code": "rate_limit_exceeded",
            "message": ERROR_MESSAGES["rate_limit_exceeded"],
            "request_id": "req-123",
            "retry_after_seconds": 10,
        }
    }
