import "./stderr-console.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ErrorCode } from "@modelcontextprotocol/sdk/types.js";
import { logger } from "./logger.js";
import { createServer } from "./server.js";

process.on("uncaughtException", (error: Error) => {
  logger.error("Uncaught exception encountered", error);
  process.exit(1);
});

process.on("unhandledRejection", (reason: unknown) => {
  logger.error("Unhandled rejection encountered", reason);
  process.exit(1);
});

process.on("SIGINT", () => {
  logger.info("Received SIGINT, shutting down");
  process.exit(0);
});

process.on("SIGTERM", () => {
  logger.info("Received SIGTERM, shutting down");
  process.exit(0);
});

async function main(): Promise<void> {
  const server = createServer();
  const transport = new StdioServerTransport();

  server.onerror = (error) => {
    logger.error("Protocol error", error);
    transport
      .send({
        jsonrpc: "2.0",
        error: {
          code: ErrorCode.ParseError,
          message: "Parse error: Invalid JSON",
        },
        id: null as unknown as string,
      })
      .catch((sendErr) => {
        logger.error("Failed to send parse error response", sendErr);
      });
  };

  await server.connect(transport);
  logger.info("Customer MCP server running on stdio transport");
}

main().catch((error) => {
  logger.error("Fatal startup error", error);
  process.exit(1);
});
