import util from "node:util";

export type LogLevel = "DEBUG" | "INFO" | "WARN" | "ERROR";

function formatMessage(level: LogLevel, message: string, meta?: unknown): string {
  const timestamp = new Date().toISOString();
  const metaStr = meta !== undefined ? ` ${util.inspect(meta, { colors: false, depth: 5 })}` : "";
  return `[${timestamp}] [${level}] ${message}${metaStr}\n`;
}

export const logger = {
  debug(message: string, meta?: unknown): void {
    process.stderr.write(formatMessage("DEBUG", message, meta));
  },
  info(message: string, meta?: unknown): void {
    process.stderr.write(formatMessage("INFO", message, meta));
  },
  warn(message: string, meta?: unknown): void {
    process.stderr.write(formatMessage("WARN", message, meta));
  },
  error(message: string, meta?: unknown): void {
    process.stderr.write(formatMessage("ERROR", message, meta));
  },
};
