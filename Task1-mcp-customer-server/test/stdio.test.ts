import { describe, expect, it } from "vitest";
import { spawn } from "node:child_process";
import path from "node:path";
import readline from "node:readline";

interface JsonRpcResponse {
  jsonrpc: string;
  id?: number | string | null;
  result?: {
    content?: Array<{ type: string; text: string }>;
    isError?: boolean;
    tools?: Array<{ name: string }>;
    [key: string]: unknown;
  };
  error?: {
    code: number;
    message: string;
    data?: {
      issues?: Array<{ path: string; message: string }>;
      [key: string]: unknown;
    };
  };
}

describe("STDIO JSON-RPC Integration", () => {
  it("processes lifecycle, tool listing, valid/invalid calls, and proves leak detection", async () => {
    const entryPath = path.resolve(__dirname, "../dist/index.js");
    const child = spawn("node", [entryPath], {
      stdio: ["pipe", "pipe", "pipe"],
    });

    const stdoutLines: string[] = [];
    const stderrChunks: string[] = [];
    const pendingResponses = new Map<number | string, (res: JsonRpcResponse) => void>();
    let onLineReceived: (() => void) | null = null;

    const rl = readline.createInterface({
      input: child.stdout,
      terminal: false,
    });

    // Collect stdout lines without assertions inside the stream callback
    rl.on("line", (line) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      stdoutLines.push(trimmed);

      try {
        const parsed = JSON.parse(trimmed) as JsonRpcResponse;
        if (parsed.id !== undefined && parsed.id !== null && pendingResponses.has(parsed.id)) {
          const resolve = pendingResponses.get(parsed.id)!;
          pendingResponses.delete(parsed.id);
          resolve(parsed);
        }
      } catch {
        // Non-JSON lines will trigger leak detection in post-session assertions
      }

      if (onLineReceived) {
        onLineReceived();
        onLineReceived = null;
      }
    });

    child.stderr.on("data", (chunk: Buffer) => {
      stderrChunks.push(chunk.toString("utf-8"));
    });

    function sendRequest(request: Record<string, unknown>): Promise<JsonRpcResponse> {
      return new Promise((resolve, reject) => {
        const id = request.id as number | string;
        pendingResponses.set(id, resolve);
        child.stdin.write(JSON.stringify(request) + "\n", (err) => {
          if (err) reject(err);
        });
      });
    }

    function sendRawLine(line: string): Promise<void> {
      return new Promise((resolve, reject) => {
        onLineReceived = resolve;
        child.stdin.write(line + "\n", (err) => {
          if (err) reject(err);
        });
      });
    }

    function sendNotification(notification: Record<string, unknown>): Promise<void> {
      return new Promise((resolve, reject) => {
        child.stdin.write(JSON.stringify(notification) + "\n", (err) => {
          if (err) reject(err);
          else resolve();
        });
      });
    }

    // 1. Initialize
    const initRes = await sendRequest({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: {
          name: "integration-tester",
          version: "1.0.0",
        },
      },
    });

    // 2. Initialized notification
    await sendNotification({
      jsonrpc: "2.0",
      method: "notifications/initialized",
    });

    // 3. tools/list
    const listRes = await sendRequest({
      jsonrpc: "2.0",
      id: 2,
      method: "tools/list",
      params: {},
    });

    // 4. Valid call: get_customer_record
    const getValidRes = await sendRequest({
      jsonrpc: "2.0",
      id: 3,
      method: "tools/call",
      params: {
        name: "get_customer_record",
        arguments: {
          customer_id: "CUST-10001",
        },
      },
    });

    // 5. Valid call: trigger_refund
    const refundValidRes = await sendRequest({
      jsonrpc: "2.0",
      id: 4,
      method: "tools/call",
      params: {
        name: "trigger_refund",
        arguments: {
          customer_id: "CUST-10001",
          amount: 50.0,
          reason: "Item arrived damaged during transit",
        },
      },
    });

    // 6. Business failure: customer not found (isError: true result)
    const notFoundRes = await sendRequest({
      jsonrpc: "2.0",
      id: 5,
      method: "tools/call",
      params: {
        name: "get_customer_record",
        arguments: {
          customer_id: "CUST-99999",
        },
      },
    });

    // 7. Malformed JSON line
    await sendRawLine("{ malformed json line without closing");

    // 8. Following valid call after malformed line
    const followUpValidRes = await sendRequest({
      jsonrpc: "2.0",
      id: 6,
      method: "tools/list",
      params: {},
    });

    // 9. Unknown method (-32601)
    const unknownMethodRes = await sendRequest({
      jsonrpc: "2.0",
      id: 7,
      method: "tools/non_existent_method",
      params: {},
    });

    // 10. Invalid call: customer_id missing (-32602)
    const missingIdRes = await sendRequest({
      jsonrpc: "2.0",
      id: 8,
      method: "tools/call",
      params: {
        name: "get_customer_record",
        arguments: {},
      },
    });

    // 11. Invalid call: customer_id as number 12345 (-32602)
    const numberIdRes = await sendRequest({
      jsonrpc: "2.0",
      id: 9,
      method: "tools/call",
      params: {
        name: "get_customer_record",
        arguments: {
          customer_id: 12345,
        },
      },
    });

    // 12. Invalid call: amount as string "50" (-32602)
    const stringAmountRes = await sendRequest({
      jsonrpc: "2.0",
      id: 10,
      method: "tools/call",
      params: {
        name: "trigger_refund",
        arguments: {
          customer_id: "CUST-10001",
          amount: "50",
          reason: "Item damaged during shipment",
        },
      },
    });

    // 13. Invalid call: amount missing (-32602)
    const missingAmountRes = await sendRequest({
      jsonrpc: "2.0",
      id: 11,
      method: "tools/call",
      params: {
        name: "trigger_refund",
        arguments: {
          customer_id: "CUST-10001",
          reason: "Item damaged during shipment",
        },
      },
    });

    // 14. Invalid call: reason missing (-32602)
    const missingReasonRes = await sendRequest({
      jsonrpc: "2.0",
      id: 12,
      method: "tools/call",
      params: {
        name: "trigger_refund",
        arguments: {
          customer_id: "CUST-10001",
          amount: 50,
        },
      },
    });

    // 15. Invalid call: reason 9 chars (-32602)
    const shortReasonRes = await sendRequest({
      jsonrpc: "2.0",
      id: 13,
      method: "tools/call",
      params: {
        name: "trigger_refund",
        arguments: {
          customer_id: "CUST-10001",
          amount: 25,
          reason: "123456789",
        },
      },
    });

    // 16. Invalid call: extra unknown key (-32602)
    const extraKeyRes = await sendRequest({
      jsonrpc: "2.0",
      id: 14,
      method: "tools/call",
      params: {
        name: "get_customer_record",
        arguments: {
          customer_id: "CUST-10001",
          extra_unknown: "val",
        },
      },
    });

    // 17. Invalid call: unknown tool (-32602)
    const unknownToolRes = await sendRequest({
      jsonrpc: "2.0",
      id: 15,
      method: "tools/call",
      params: {
        name: "unknown_custom_tool",
        arguments: {},
      },
    });

    // 18. Invalid call: arguments omitted (-32602)
    const omittedArgsRes = await sendRequest({
      jsonrpc: "2.0",
      id: 16,
      method: "tools/call",
      params: {
        name: "get_customer_record",
      },
    });

    // 19. Invalid call: arguments as array (-32602)
    const arrayArgsRes = await sendRequest({
      jsonrpc: "2.0",
      id: 17,
      method: "tools/call",
      params: {
        name: "get_customer_record",
        arguments: ["CUST-10001"],
      },
    });

    // Terminate server process cleanly and await exit
    await new Promise<void>((resolve) => {
      child.on("exit", () => resolve());
      child.kill("SIGTERM");
    });

    // =========================================================================
    // POST-SESSION LEAK DETECTION & PROTOCOL COMPLIANCE ASSERTIONS
    // =========================================================================

    // Assert that every line collected from stdout is strictly valid JSON with jsonrpc: "2.0"
    expect(stdoutLines.length).toBeGreaterThan(0);
    const parsedMessages: JsonRpcResponse[] = [];
    for (const line of stdoutLines) {
      let parsed: JsonRpcResponse;
      try {
        parsed = JSON.parse(line);
      } catch (err) {
        throw new Error(`STDOUT LEAK DETECTED: Line is not valid JSON -> ${JSON.stringify(line)}`);
      }
      expect(parsed.jsonrpc).toBe("2.0");
      parsedMessages.push(parsed);
    }

    // Assert initialize
    expect(initRes.error).toBeUndefined();
    expect(initRes.result).toBeDefined();

    // Assert tools/list
    expect(listRes.result?.tools).toBeDefined();
    const toolNames = listRes.result?.tools?.map((t) => t.name);
    expect(toolNames).toContain("get_customer_record");
    expect(toolNames).toContain("trigger_refund");

    // Assert valid get_customer_record
    expect(getValidRes.error).toBeUndefined();
    expect(getValidRes.result?.isError).toBeFalsy();
    const customer = JSON.parse(getValidRes.result!.content![0].text);
    expect(customer.customer_id).toBe("CUST-10001");
    expect(customer.name).toBe("Alice Smith");

    // Assert valid trigger_refund
    expect(refundValidRes.error).toBeUndefined();
    expect(refundValidRes.result?.isError).toBeFalsy();
    const refund = JSON.parse(refundValidRes.result!.content![0].text);
    expect(refund.customer_id).toBe("CUST-10001");
    expect(refund.amount).toBe(50.0);
    expect(refund.status).toBe("processed");
    expect(refund.refund_id).toBeDefined();

    // Assert business failure: customer not found (result.isError === true)
    expect(notFoundRes.error).toBeUndefined();
    expect(notFoundRes.result?.isError).toBe(true);
    expect(notFoundRes.result?.content?.[0]?.text).toContain("Customer not found");

    // Assert malformed JSON line produced -32700 ParseError
    const parseErrorRes = parsedMessages.find((m) => m.error?.code === -32700);
    expect(parseErrorRes).toBeDefined();
    expect(parseErrorRes?.error?.code).toBe(-32700);

    // Assert following valid call still succeeded after parse error
    expect(followUpValidRes.error).toBeUndefined();
    expect(followUpValidRes.result?.tools).toBeDefined();

    // Assert unknown method produced -32601 MethodNotFound
    expect(unknownMethodRes.error).toBeDefined();
    expect(unknownMethodRes.error?.code).toBe(-32601);

    // Assert specific human-readable messages for invalid parameter calls
    expect(missingIdRes.error?.code).toBe(-32602);
    expect(missingIdRes.error?.data?.issues).toEqual([
      { path: "customer_id", message: "customer_id is required" },
    ]);

    expect(numberIdRes.error?.code).toBe(-32602);
    expect(numberIdRes.error?.data?.issues).toEqual([
      { path: "customer_id", message: "customer_id must be a string" },
    ]);

    expect(stringAmountRes.error?.code).toBe(-32602);
    expect(stringAmountRes.error?.data?.issues).toEqual([
      { path: "amount", message: "amount must be a number" },
    ]);

    expect(missingAmountRes.error?.code).toBe(-32602);
    expect(missingAmountRes.error?.data?.issues).toEqual([
      { path: "amount", message: "amount is required" },
    ]);

    expect(missingReasonRes.error?.code).toBe(-32602);
    expect(missingReasonRes.error?.data?.issues).toEqual([
      { path: "reason", message: "reason is required" },
    ]);

    expect(shortReasonRes.error?.code).toBe(-32602);
    expect(shortReasonRes.error?.data?.issues).toEqual([
      {
        path: "reason",
        message: "reason must be at least 10 characters long after trimming whitespace",
      },
    ]);

    expect(extraKeyRes.error?.code).toBe(-32602);
    expect(extraKeyRes.error?.data?.issues?.[0]?.path).toBe("extra_unknown");

    // Assert every -32602 response has a non-empty error.data.issues array
    const invalidParamResponses = [
      missingIdRes,
      numberIdRes,
      stringAmountRes,
      missingAmountRes,
      missingReasonRes,
      shortReasonRes,
      extraKeyRes,
      unknownToolRes,
      omittedArgsRes,
      arrayArgsRes,
    ];

    for (const res of invalidParamResponses) {
      expect(res.error).toBeDefined();
      expect(res.error?.code).toBe(-32602);
      expect(res.error?.data).toBeDefined();
      expect(Array.isArray(res.error?.data?.issues)).toBe(true);
      expect(res.error?.data?.issues?.length).toBeGreaterThan(0);
    }

    // Assert stderr contains log records
    const fullStderr = stderrChunks.join("");
    expect(fullStderr).toMatch(/\[(INFO|DEBUG|WARN|ERROR)\]/);
  });
});
