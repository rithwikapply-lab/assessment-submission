from typing import Optional, Dict, Any

ERROR_MESSAGES: Dict[str, str] = {
    "invalid_request": "Invalid request payload or parameters.",
    "unauthorized": "Invalid or missing API key.",
    "request_too_large": "Requested token count exceeds maximum allowed.",
    "rate_limit_exceeded": "Rate limit exceeded. Please retry later.",
    "upstream_unavailable": "Upstream service is currently unavailable.",
    "upstream_timeout": "Upstream request timed out.",
    "internal_error": "An internal server error occurred.",
    "not_found": "The requested resource was not found.",
    "method_not_allowed": "Method not allowed for the requested resource.",
}

ERROR_STATUS_CODES: Dict[str, int] = {
    "invalid_request": 400,
    "unauthorized": 401,
    "request_too_large": 413,
    "rate_limit_exceeded": 429,
    "internal_error": 500,
    "upstream_unavailable": 502,
    "upstream_timeout": 504,
    "not_found": 404,
    "method_not_allowed": 405,
}


class GatewayError(Exception):
    def __init__(self, code: str, http_status: Optional[int] = None, retry_after_s: Optional[int] = None):
        self.code = code
        self.http_status = http_status or ERROR_STATUS_CODES.get(code, 500)
        self.message = ERROR_MESSAGES.get(code, "An internal server error occurred.")
        self.retry_after_s = retry_after_s
        super().__init__(self.message)


def build_error_payload(code: str, request_id: str, retry_after_s: Optional[int] = None) -> Dict[str, Any]:
    message = ERROR_MESSAGES.get(code, "An internal server error occurred.")
    return {
        "error": {
            "type": "gateway_error",
            "code": code,
            "message": message,
            "request_id": request_id,
            "retry_after_seconds": retry_after_s
        }
    }
