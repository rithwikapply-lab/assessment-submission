import os
import sys
from dataclasses import dataclass

@dataclass
class Settings:
    gateway_db_path: str = "./data/gateway.db"
    rate_limit_tokens_per_minute: int = 50000
    rate_limit_window_seconds: int = 60
    primary_url: str = ""
    primary_api_key: str = ""
    primary_model: str = ""
    secondary_url: str = ""
    secondary_api_key: str = ""
    secondary_model: str = ""
    primary_timeout_ms: int = 3000
    secondary_timeout_ms: int = 3000
    default_max_tokens: int = 256
    max_allowed_max_tokens: int = 8192
    purge_interval_seconds: int = 30

def load_settings_from_env() -> Settings:
    def get_int(key: str, default: int) -> int:
        val = os.getenv(key)
        if val is None or val == "":
            return default
        try:
            return int(val)
        except ValueError:
            print(f"Configuration error: Invalid integer for {key}", file=sys.stderr)
            sys.exit(1)

    settings = Settings(
        gateway_db_path=os.getenv("GATEWAY_DB_PATH", "./data/gateway.db"),
        rate_limit_tokens_per_minute=get_int("RATE_LIMIT_TOKENS_PER_MINUTE", 50000),
        rate_limit_window_seconds=get_int("RATE_LIMIT_WINDOW_SECONDS", 60),
        primary_url=os.getenv("PRIMARY_URL", "http://localhost:8001/v1"),
        primary_api_key=os.getenv("PRIMARY_API_KEY", ""),
        primary_model=os.getenv("PRIMARY_MODEL", "gpt-4o"),
        secondary_url=os.getenv("SECONDARY_URL", "http://localhost:8002/v1"),
        secondary_api_key=os.getenv("SECONDARY_API_KEY", ""),
        secondary_model=os.getenv("SECONDARY_MODEL", "gpt-4o-mini"),
        primary_timeout_ms=get_int("PRIMARY_TIMEOUT_MS", 3000),
        secondary_timeout_ms=get_int("SECONDARY_TIMEOUT_MS", 3000),
        default_max_tokens=get_int("DEFAULT_MAX_TOKENS", 256),
        max_allowed_max_tokens=get_int("MAX_ALLOWED_MAX_TOKENS", 8192),
        purge_interval_seconds=get_int("PURGE_INTERVAL_SECONDS", 30),
    )

    if settings.rate_limit_tokens_per_minute <= 0:
        print("Configuration error: RATE_LIMIT_TOKENS_PER_MINUTE must be positive", file=sys.stderr)
        sys.exit(1)
    if settings.primary_timeout_ms <= 0:
        print("Configuration error: PRIMARY_TIMEOUT_MS must be positive", file=sys.stderr)
        sys.exit(1)
    if settings.secondary_timeout_ms <= 0:
        print("Configuration error: SECONDARY_TIMEOUT_MS must be positive", file=sys.stderr)
        sys.exit(1)

    return settings
