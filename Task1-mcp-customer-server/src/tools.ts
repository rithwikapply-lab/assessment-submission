import { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { store } from "./store.js";
import {
  GetCustomerRecordInput,
  TriggerRefundInput,
} from "./schemas.js";

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: {
    type: "object";
    properties: Record<string, unknown>;
    required: string[];
    additionalProperties: false;
  };
}

export const TOOLS: ToolDefinition[] = [
  {
    name: "get_customer_record",
    description: "Retrieve customer profile, status, and current balance by customer ID",
    inputSchema: {
      type: "object",
      properties: {
        customer_id: {
          type: "string",
          pattern: "^CUST-\\d{5}$",
          description: "Unique customer identifier in CUST-XXXXX format",
        },
      },
      required: ["customer_id"],
      additionalProperties: false,
    },
  },
  {
    name: "trigger_refund",
    description: "Trigger a refund for an existing customer account",
    inputSchema: {
      type: "object",
      properties: {
        customer_id: {
          type: "string",
          pattern: "^CUST-\\d{5}$",
          description: "Unique customer identifier in CUST-XXXXX format",
        },
        amount: {
          type: "number",
          exclusiveMinimum: 0,
          maximum: 10000,
          description: "Refund amount, strictly positive with up to 2 decimal places",
        },
        reason: {
          type: "string",
          minLength: 10,
          maxLength: 500,
          description: "Reason for the refund request (at least 10 non-whitespace characters)",
        },
      },
      required: ["customer_id", "amount", "reason"],
      additionalProperties: false,
    },
  },
];

export function handleGetCustomerRecord(input: GetCustomerRecordInput): CallToolResult {
  const customer = store.getCustomer(input.customer_id);
  if (!customer) {
    return {
      isError: true,
      content: [
        {
          type: "text",
          text: `Customer not found: ${input.customer_id}`,
        },
      ],
    };
  }

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(customer),
      },
    ],
  };
}

export function handleTriggerRefund(input: TriggerRefundInput): CallToolResult {
  const refund = store.processRefund(input.customer_id, input.amount, input.reason);
  if (!refund) {
    return {
      isError: true,
      content: [
        {
          type: "text",
          text: `Customer not found: ${input.customer_id}`,
        },
      ],
    };
  }

  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(refund),
      },
    ],
  };
}
