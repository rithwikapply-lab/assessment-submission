import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import httpx
import pytest

from llm_router.app import create_app


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_settings_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_PATH", "/tmp/test_env_db.db")
    monkeypatch.setenv("PRIMARY_URL", "http://primary.env.test/v1")
    monkeypatch.setenv("PRIMARY_API_KEY", "env-key-p1")
    monkeypatch.setenv("PRIMARY_MODEL", "model-env-p1")
    monkeypatch.setenv("SECONDARY_URL", "http://secondary.env.test/v1")
    monkeypatch.setenv("SECONDARY_API_KEY", "env-key-p2")
    monkeypatch.setenv("SECONDARY_MODEL", "model-env-p2")
    monkeypatch.setenv("RATE_LIMIT_TOKENS_PER_MINUTE", "12345")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "45")
    monkeypatch.setenv("PRIMARY_TIMEOUT_MS", "1500")
    monkeypatch.setenv("SECONDARY_TIMEOUT_MS", "2500")
    monkeypatch.setenv("DEFAULT_MAX_TOKENS", "512")
    monkeypatch.setenv("MAX_ALLOWED_MAX_TOKENS", "4096")
    monkeypatch.setenv("PURGE_INTERVAL_SECONDS", "15")

    app = create_app()
    s = app.state.settings

    assert s.gateway_db_path == "/tmp/test_env_db.db"
    assert s.primary_url == "http://primary.env.test/v1"
    assert s.primary_api_key == "env-key-p1"
    assert s.primary_model == "model-env-p1"
    assert s.secondary_url == "http://secondary.env.test/v1"
    assert s.secondary_api_key == "env-key-p2"
    assert s.secondary_model == "model-env-p2"
    assert s.rate_limit_tokens_per_minute == 12345
    assert s.rate_limit_window_seconds == 45
    assert s.primary_timeout_ms == 1500
    assert s.secondary_timeout_ms == 2500
    assert s.default_max_tokens == 512
    assert s.max_allowed_max_tokens == 4096
    assert s.purge_interval_seconds == 15


class Primary429Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            self.rfile.read(length)
        body = b'{"error": "rate limit exceeded"}'
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Secondary200Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            self.rfile.read(length)
        resp = {
            "id": "chatcmpl-e2e-ok",
            "choices": [{"message": {"role": "assistant", "content": "from secondary fallback"}}],
            "usage": {"total_tokens": 15}
        }
        body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_e2e_uvicorn_subprocess_fallback(tmp_path):
    repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    db_file = str(tmp_path / "e2e_gateway.db")
    tenant_id = "e2e-tenant"
    api_key = "sk-e2e-valid-key"

    # 1. Run admin CLI to create tenant
    subprocess.run(
        [
            sys.executable,
            "-m",
            "llm_router.admin",
            "add-tenant",
            "--id",
            tenant_id,
            "--key",
            api_key,
            "--limit",
            "50000",
            "--db",
            db_file,
        ],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    # 2. Start two local HTTP servers
    p1_port = find_free_port()
    p2_port = find_free_port()

    server1 = http.server.HTTPServer(("127.0.0.1", p1_port), Primary429Handler)
    server2 = http.server.HTTPServer(("127.0.0.1", p2_port), Secondary200Handler)

    t1 = threading.Thread(target=server1.serve_forever, daemon=True)
    t2 = threading.Thread(target=server2.serve_forever, daemon=True)
    t1.start()
    t2.start()

    # 3. Start uvicorn as subprocess
    uvicorn_port = find_free_port()
    env = os.environ.copy()
    env["GATEWAY_DB_PATH"] = db_file
    env["PRIMARY_URL"] = f"http://127.0.0.1:{p1_port}/v1"
    env["PRIMARY_API_KEY"] = "mock-key-1"
    env["SECONDARY_URL"] = f"http://127.0.0.1:{p2_port}/v1"
    env["SECONDARY_API_KEY"] = "mock-key-2"
    env["PRIMARY_TIMEOUT_MS"] = "2000"
    env["SECONDARY_TIMEOUT_MS"] = "2000"
    env["PYTHONPATH"] = f"{repo_dir}/src:{repo_dir}"

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "llm_router.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(uvicorn_port),
        ],
        cwd=repo_dir,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # Wait for uvicorn to come up (health check)
        healthy = False
        health_url = f"http://127.0.0.1:{uvicorn_port}/healthz"
        for _ in range(50):
            try:
                r = httpx.get(health_url, timeout=0.2)
                if r.status_code == 200:
                    healthy = True
                    break
            except Exception:
                time.sleep(0.1)

        assert healthy, "Uvicorn failed to start within timeout"

        # 4. Send real request
        req_url = f"http://127.0.0.1:{uvicorn_port}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {"messages": [{"role": "user", "content": "hello e2e"}]}

        res = httpx.post(req_url, json=body, headers=headers, timeout=5.0)

        assert res.status_code == 200
        assert res.headers.get("x-gateway-fallback") == "true"
        data = res.json()
        assert data["choices"][0]["message"]["content"] == "from secondary fallback"
        assert "x-request-id" in res.headers
    finally:
        # 5. Always terminate the subprocess and shutdown servers
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

        server1.shutdown()
        server2.shutdown()
