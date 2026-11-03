# MCP Customer Server

A Model Context Protocol (MCP) server written in TypeScript that exposes customer management and refund tools over a pure STDIO transport. Built using the official `@modelcontextprotocol/sdk` and `zod`.

## Features

- Pure STDIO transport with strict stdout isolation for JSON-RPC messages.
- Low-level `Server` architecture providing standard JSON-RPC error handling.
- Strict input validation with Zod, covering numeric precision and formatting edge cases.
- In-memory mock customer store with seed records.
- Comprehensive unit tests and end-to-end STDIO integration tests.

## Tools

### 1. `get_customer_record`

Retrieves customer profile, status, and account balance.

- **Parameters:**
  - `customer_id` (string, required): Must strictly match the format `^CUST-\d{5}$` (e.g. `CUST-10001`).
- **Response:**
  - On success: JSON string containing customer record (`customer_id`, `name`, `email`, `status`, `balance`).
  - If customer does not exist: Returns tool result with `isError: true` and descriptive message.

### 2. `trigger_refund`

Initiates a refund against an existing customer account.

- **Parameters:**
  - `customer_id` (string, required): Must match format `^CUST-\d{5}$`.
  - `amount` (number, required): Finite positive number strictly greater than 0, maximum 2 decimal places, capped at 10,000.
  - `reason` (string, required): String with trimmed length of at least 10 characters and maximum 500 characters.
- **Response:**
  - On success: JSON string containing refund receipt (`refund_id`, `customer_id`, `amount`, `status: "processed"`).
  - If customer does not exist: Returns tool result with `isError: true` and descriptive message.

## Error Handling

The server implements standard JSON-RPC 2.0 error mapping:

| Code | Constant | Condition | Response Payload |
|---|---|---|---|
| `-32700` | `ParseError` | Malformed JSON received | `{ code: -32700, message: "Parse error: Invalid JSON" }` |
| `-32600` | `InvalidRequest` | Invalid JSON-RPC request structure | Handled by protocol transport layer |
| `-32601` | `MethodNotFound` | Requested method is not registered | Handled by server router |
| `-32602` | `InvalidParams` | Unknown tool name or Zod schema validation failure | `{ code: -32602, message: "MCP error -32602: Invalid params: ...", data: { issues: [...] } }` |
| `-32603` | `InternalError` | Unexpected runtime exception | `{ code: -32603, message: "MCP error -32603: Internal error" }` (details logged to stderr) |
| N/A | Business Failure | Target customer ID not found in store | Regular result object with `isError: true` and text explanation |

The SDK adds the "MCP error <code>: " prefix to wire error messages, so clients should rely on error.code and error.data.issues rather than parsing message strings.

## Architecture Decisions

### STDIO Isolation

In the Model Context Protocol, the client process communicates with the server via standard input/output streams. The stdout stream must remain exclusively reserved for valid JSON-RPC frames. Any stray output (such as startup banners, third-party loggers, or debugging prints) corrupts the JSON stream and breaks the client connection.

In ECMAScript Modules (ESM), static import declarations are evaluated in source order prior to executing the importing module's body. By placing the console method redirections in an isolated module (`src/stderr-console.ts`) and importing it as the very first line of `src/index.ts`, `console.log`, `console.info`, `console.debug`, and `console.warn` are redirected to `process.stderr` before any downstream modules or dependencies are initialized. This prevents imported packages or submodules from leaking logs onto stdout during startup.

To enforce STDIO isolation:
1. Console redirection lives in its own dedicated module (`src/stderr-console.ts`), imported first in `src/index.ts` to ensure execution before subsequent imports.
2. A structured logger (`src/logger.ts`) writes exclusively via `process.stderr.write` using ISO timestamps and log severity levels.
3. Uncaught exceptions and unhandled promise rejections are intercepted, formatted to stderr, and result in a clean non-zero exit.

### Low-Level Server vs McpServer

The high-level `McpServer` abstraction automatically handles tool registrations, but converts validation failures into tool results containing `isError: true` rather than emitting protocol-level JSON-RPC errors.

This implementation uses the low-level `Server` class from `@modelcontextprotocol/sdk/server/index.js` with manual request handlers:
- Hand-written JSON Schema specifications are exposed via `ListToolsRequestSchema`.
- Input arguments are validated in `CallToolRequestSchema` using strict Zod schemas (`.strict()`).
- Validation failures throw `McpError(ErrorCode.InvalidParams, ...)` which the SDK serializes as genuine JSON-RPC `-32602` errors with full issue metadata.
- Domain-level business failures (e.g. customer not found) return tool-level `isError: true` responses as expected by calling clients.

## Project Structure

```
mcp-customer-server/
├── package.json
├── tsconfig.json
├── .gitignore
├── README.md
├── src/
│   ├── index.ts          # Server bootstrap
│   ├── stderr-console.ts # First-imported console override
│   ├── logger.ts         # Stderr-only structured logger
│   ├── schemas.ts        # Zod validation schemas
│   ├── store.ts          # In-memory customer datastore
│   ├── tools.ts          # Tool definitions and business handlers
│   └── server.ts         # Low-level MCP server request routing
└── test/
    ├── schemas.test.ts   # Schema unit test suite
    └── stdio.test.ts     # End-to-end STDIO JSON-RPC integration suite
```

## Getting Started

### Prerequisites

- Node.js (version 18 or later)
- npm

### Installation

```bash
npm install
```

### Build

Compile TypeScript source to `dist/`:

```bash
npm run build
```

### Running Tests

Execute the Vitest test suites:

```bash
npm test
```

### STDIO Smoke Test

Pipe a sample JSON-RPC initialize payload directly into the compiled server and validate JSON output via `jq`:

```bash
npm run test:stdio
```

### Start Server

```bash
npm start
```
