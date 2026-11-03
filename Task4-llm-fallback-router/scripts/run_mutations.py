#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VENV_PYTEST = os.path.join(REPO_DIR, ".venv", "bin", "pytest")

MUTATIONS = [
    (1, "expiry uses < instead of <=", "src/llm_router/limiter.py", [
        ("ts_ms <= ?", "ts_ms < ?"),
        ("ts_ms > ?", "ts_ms >= ?"),
    ]),
    (2, "never evict expired rows", "src/llm_router/limiter.py", [
        ("DELETE FROM usage WHERE tenant_id = ? AND ts_ms <= ?;", "DELETE FROM usage WHERE tenant_id = ? AND ts_ms <= ? AND 0;"),
        ("DELETE FROM usage WHERE ts_ms <= ?;", "DELETE FROM usage WHERE ts_ms <= ? AND 0;"),
    ]),
    (3, "usage sum includes other tenants", "src/llm_router/limiter.py", [
        ("WHERE tenant_id = ? AND ts_ms > ?", "WHERE ? IS NOT NULL AND ts_ms > ?"),
    ]),
    (4, "commit does not reconcile actual tokens", "src/llm_router/service.py", [
        ("await self.limiter.commit(reservation, tokens_actual)", "await self.limiter.commit(reservation, tokens_reserved)"),
    ]),
    (5, "release does not refund", "src/llm_router/limiter.py", [
        ("DELETE FROM usage WHERE id = ? AND state = 'reserved';", "DELETE FROM usage WHERE id = ? AND state = 'reserved' AND 0;"),
    ]),
    (6, "do not cancel the timed-out provider task", "src/llm_router/router.py", [
        ("""        if task not in done:
            task.cancel()""",
         """        if task not in done:
            pass"""),
    ]),
    (7, "await the provider after the timeout", "src/llm_router/router.py", [
        ("""        if task not in done:
            task.cancel()""",
         """        if task not in done:
            await task"""),
    ]),
    (8, "remove failover on 429", "src/llm_router/router.py", [
        ("DEFAULT_FAILOVER_STATUSES = {429, 500, 502, 503, 504}", "DEFAULT_FAILOVER_STATUSES = {500, 502, 503, 504}"),
    ]),
    (9, "remove failover on timeout", "src/llm_router/router.py", [
        ('if prim_attempt.outcome in ("timeout", "connection_error"):', 'if prim_attempt.outcome in ("connection_error",):'),
    ]),
    (10, "timeout of 3000 seconds instead of 3000 ms", "src/llm_router/router.py", [
        ("deadline_s = timeout_ms / 1000.0", "deadline_s = float(timeout_ms)"),
    ]),
    (11, "error payload includes the upstream body", "src/llm_router/errors.py", [
        ('"retry_after_seconds": retry_after_s', '"retry_after_seconds": retry_after_s, "upstream_body": {"raw": "leaked_upstream_content"}'),
    ]),
    (12, "reserve uses a deferred BEGIN with separate SELECT and INSERT transactions", "src/llm_router/limiter.py", [
        ('conn.execute("BEGIN IMMEDIATE;")', 'conn.execute("BEGIN DEFERRED;")'),
        ("""                if used + estimated_tokens <= effective_limit:
                    res_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO usage (id, tenant_id, ts_ms, tokens, state) VALUES (?, ?, ?, ?, 'reserved');",
                        (res_id, tenant_id, now, estimated_tokens)
                    )
                    conn.execute("COMMIT;")
                    return Reservation(id=res_id, tenant_id=tenant_id, tokens=estimated_tokens, ts_ms=now)""",
         """                conn.execute("COMMIT;")
                if used + estimated_tokens <= effective_limit:
                    import time; time.sleep(0.002)
                    conn.execute("BEGIN DEFERRED;")
                    res_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO usage (id, tenant_id, ts_ms, tokens, state) VALUES (?, ?, ?, ?, 'reserved');",
                        (res_id, tenant_id, now, estimated_tokens)
                    )
                    conn.execute("COMMIT;")
                    return Reservation(id=res_id, tenant_id=tenant_id, tokens=estimated_tokens, ts_ms=now)"""),
    ]),
    (13, "skip the release in finally on cancellation", "src/llm_router/service.py", [
        ("if reservation is not None and not committed:", "if reservation is not None and not committed and outcome != 'cancelled':"),
    ]),
    (14, "charge both providers' tokens", "src/llm_router/service.py", [
        ("await self.limiter.commit(reservation, tokens_actual)", "await self.limiter.commit(reservation, (tokens_actual * 2) if route_result.fallback_used else tokens_actual)"),
    ]),
    (15, "retry_after_ms equals the window instead of the walk", "src/llm_router/limiter.py", [
        ("retry_after_ms = max(1, retry_at - now)", "retry_after_ms = self.window_ms"),
    ]),
]

def run_mutation(num, description, target_rel_file, replacements):
    mut_dir = f"/tmp/mut-{num}"
    if os.path.exists(mut_dir):
        shutil.rmtree(mut_dir)

    shutil.copytree(REPO_DIR, mut_dir, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", ".pytest_cache"))

    target_file = os.path.join(mut_dir, target_rel_file)
    with open(target_file, "r") as f:
        content = f.read()

    for old, new in replacements:
        assert old in content, f"Target string not found in {target_rel_file}: {old}"
        content = content.replace(old, new, 1)

    with open(target_file, "w") as f:
        f.write(content)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{os.path.join(mut_dir, 'src')}:{mut_dir}"

    proc = subprocess.run(
        [VENV_PYTEST, "-q"],
        cwd=mut_dir,
        capture_output=True,
        text=True,
        env=env
    )

    is_invalid = False
    invalid_reason = ""
    if proc.returncode != 0:
        tb_proc = subprocess.run(
            [VENV_PYTEST, "-q", "--tb=line"],
            cwd=mut_dir,
            capture_output=True,
            text=True,
            env=env
        )
        combined_tb = (tb_proc.stdout + "\n" + tb_proc.stderr).lower()
        for kw in ["programmingerror", "bindings", "syntaxerror", "importerror"]:
            if kw in combined_tb:
                is_invalid = True
                invalid_reason = kw
                break

    # Clean up copy immediately
    shutil.rmtree(mut_dir)

    lines = proc.stdout.strip().split("\n")
    failing_tests = []
    summary_line = ""
    for l in lines:
        if l.startswith("FAILED "):
            test_name = l.split()[1]
            failing_tests.append(test_name)
        elif "failed" in l or "passed" in l or "error" in l:
            summary_line = l

    if is_invalid:
        status = "INVALID"
        killed = False
    elif proc.returncode != 0:
        status = "KILLED"
        killed = True
    else:
        status = "SURVIVED"
        killed = False

    return {
        "num": num,
        "description": description,
        "failing_tests": failing_tests,
        "failing_count": len(failing_tests),
        "summary": summary_line,
        "status": status,
        "invalid_reason": invalid_reason,
        "killed": killed
    }

def main():
    print("=== Running 15 Mutation Checks ===")
    results = []
    for num, desc, rel_file, reps in MUTATIONS:
        r = run_mutation(num, desc, rel_file, reps)
        results.append(r)
        failing_names = [t.split("::")[-1] for t in r["failing_tests"]]
        failing_str = ", ".join(failing_names[:3]) + (f" (+{len(failing_names)-3} more)" if len(failing_names) > 3 else "")
        print(f"Mutation {r['num']:>2}: {r['description']:<55} | {r['status']} ({r['failing_count']} failed) | {failing_str} | {r['summary']}")

    print("\n=== Mutation Table ===")
    print(f"| {'#':<2} | {'Breakage':<55} | {'Status':<7} | {'Failures':<8} | {'Failing Tests'} |")
    print("|----|---------------------------------------------------------|---------|----------|--------------------------------------------------|")
    for r in results:
        failing_names = [t.split("::")[-1] for t in r["failing_tests"]]
        failing_str = ", ".join(failing_names[:2]) + (f" (+{len(failing_names)-2} more)" if len(failing_names) > 2 else "")
        print(f"| {r['num']:<2} | {r['description']:<55} | {r['status']:<7} | {r['failing_count']:<8} | {failing_str} |")

if __name__ == "__main__":
    main()
