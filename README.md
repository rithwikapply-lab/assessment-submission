# Assessment submission

This repository combines four separate task repositories with their full commit history preserved via git subtree. The original standalone repositories remain available under their respective names.

## Tasks Overview

| Task | Folder | Repository | Stack | Tests | Commits |
|---|---|---|---|---|---|
| Task 1: MCP Customer Server | `Task1-mcp-customer-server` | [Task1-mcp-customer-server](https://github.com/rithwikapply-lab/Task1-mcp-customer-server) | TypeScript, Model Context Protocol SDK, zod, vitest | 32 tests | 22 commits |
| Task 2: MCP Gateway Proxy | `Task2-mcp-gateway-proxy` | [Task2-mcp-gateway-proxy](https://github.com/rithwikapply-lab/Task2-mcp-gateway-proxy) | TypeScript on Node, Express | 104 tests | 25 commits |
| Task 3: LLM Gateway Guardrail | `Task3-llm-gateway-guardrail` | [Task3-llm-gateway-guardrail](https://github.com/rithwikapply-lab/Task3-llm-gateway-guardrail) | TypeScript on Node, Express | 49 tests | 23 commits |
| Task 4: LLM Fallback Router | `Task4-llm-fallback-router` | [Task4-llm-fallback-router](https://github.com/rithwikapply-lab/Task4-llm-fallback-router) | Python 3.11+, FastAPI, SQLite | 82 tests | 26 commits |

### Task Summaries
- **Task 1: MCP Customer Server**: A Model Context Protocol (MCP) server written in TypeScript that exposes customer management and refund tools over a pure STDIO transport.
- **Task 2: MCP Gateway Proxy**: An HTTP/JSON-RPC reverse proxy and security gateway written in TypeScript for the Model Context Protocol (MCP) that enforces fine-grained method-level authorization, role-based tool restrictions, and strict protocol wire validation.
- **Task 3: LLM Gateway Guardrail**: A streaming security gateway and reverse proxy for LLM chat completions that intercepts streaming responses from upstream providers and redacts sensitive PII (emails, US SSNs, and Luhn-validated credit card numbers) in real time with bounded memory.
- **Task 4: LLM Fallback Router**: A high-performance rate-limiting and model-fallback gateway service for Large Language Model APIs with exact sliding-window token limiting on on-disk SQLite, 3 s timeout failover, and sanitized errors.

## How to Run Each

### Task 1: MCP Customer Server
```bash
cd Task1-mcp-customer-server
npm ci
npm test
```

### Task 2: MCP Gateway Proxy
```bash
cd Task2-mcp-gateway-proxy
npm ci
npm test
```

### Task 3: LLM Gateway Guardrail
```bash
cd Task3-llm-gateway-guardrail
npm ci
npm test
```

### Task 4: LLM Fallback Router
```bash
cd Task4-llm-fallback-router
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

---
*Note: Every task folder contains its own comprehensive README.md documenting architecture, concurrency semantics, error mapping, testing strategies, design tradeoffs, and limitations.*
