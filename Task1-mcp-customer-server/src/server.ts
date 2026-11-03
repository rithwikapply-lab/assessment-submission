import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  ListToolsRequestSchema,
  ErrorCode,
  McpError,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { logger } from "./logger.js";
import {
  GetCustomerRecordInputSchema,
  TriggerRefundInputSchema,
} from "./schemas.js";
import {
  TOOLS,
  handleGetCustomerRecord,
  handleTriggerRefund,
} from "./tools.js";

const RelaxedCallToolRequestSchema = z.object({
  method: z.literal("tools/call"),
  params: z
    .object({
      name: z.string(),
      arguments: z.unknown().optional(),
    })
    .passthrough(),
});

class CustomerServer extends Server {
  override setRequestHandler(requestSchema: any, handler: any): void {
    const shape = requestSchema?.shape;
    const method = shape?.method?.value;
    if (method === "tools/call") {
      return Object.getPrototypeOf(Server.prototype).setRequestHandler.call(
        this,
        requestSchema,
        handler
      );
    }
    return super.setRequestHandler(requestSchema, handler);
  }
}

export function createServer(): Server {
  const server = new CustomerServer(
    {
      name: "mcp-customer-server",
      version: "1.0.0",
    },
    {
      capabilities: {
        tools: {},
      },
    }
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => {
    logger.debug("Received tools/list request");
    return {
      tools: TOOLS,
    };
  });

  server.setRequestHandler(RelaxedCallToolRequestSchema as any, async (request: any) => {
    const { name, arguments: rawArgs } = request.params;
    logger.debug(`Received tools/call request for tool: ${name}`);

    try {
      switch (name) {
        case "get_customer_record": {
          const parsed = GetCustomerRecordInputSchema.safeParse(rawArgs);
          if (!parsed.success) {
            const issues = parsed.error.issues.map((issue) => {
              const path = issue.path.join(".") || (issue as any).keys?.join(".") || "";
              return {
                path,
                message: issue.message,
              };
            });
            const resolvedIssues =
              issues.length > 0
                ? issues
                : [{ path: "", message: "Invalid parameters" }];
            const summary = resolvedIssues
              .map((i) => (i.path ? `${i.path}: ${i.message}` : i.message))
              .join("; ");
            throw new McpError(
              ErrorCode.InvalidParams,
              `Invalid params: ${summary}`,
              { issues: resolvedIssues }
            );
          }
          return handleGetCustomerRecord(parsed.data);
        }

        case "trigger_refund": {
          const parsed = TriggerRefundInputSchema.safeParse(rawArgs);
          if (!parsed.success) {
            const issues = parsed.error.issues.map((issue) => {
              const path = issue.path.join(".") || (issue as any).keys?.join(".") || "";
              return {
                path,
                message: issue.message,
              };
            });
            const resolvedIssues =
              issues.length > 0
                ? issues
                : [{ path: "", message: "Invalid parameters" }];
            const summary = resolvedIssues
              .map((i) => (i.path ? `${i.path}: ${i.message}` : i.message))
              .join("; ");
            throw new McpError(
              ErrorCode.InvalidParams,
              `Invalid params: ${summary}`,
              { issues: resolvedIssues }
            );
          }
          return handleTriggerRefund(parsed.data);
        }

        default:
          throw new McpError(
            ErrorCode.InvalidParams,
            `Unknown tool: ${name}`,
            {
              issues: [
                {
                  path: "name",
                  message: `Unknown tool: ${name}`,
                },
              ],
            }
          );
      }
    } catch (error) {
      if (error instanceof McpError) {
        throw error;
      }
      logger.error(`Internal error executing tool ${name}`, error);
      throw new McpError(ErrorCode.InternalError, "Internal error");
    }
  });

  return server;
}
