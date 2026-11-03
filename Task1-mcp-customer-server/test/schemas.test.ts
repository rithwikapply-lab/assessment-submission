import { describe, expect, it } from "vitest";
import {
  AmountSchema,
  CustomerIdSchema,
  GetCustomerRecordInputSchema,
  ReasonSchema,
  TriggerRefundInputSchema,
} from "../src/schemas.js";

describe("CustomerIdSchema", () => {
  it("accepts valid customer id", () => {
    expect(CustomerIdSchema.safeParse("CUST-10001").success).toBe(true);
    expect(CustomerIdSchema.safeParse("CUST-99999").success).toBe(true);
  });

  it("rejects lowercase format", () => {
    expect(CustomerIdSchema.safeParse("cust-10001").success).toBe(false);
  });

  it("rejects 4 digits", () => {
    expect(CustomerIdSchema.safeParse("CUST-1001").success).toBe(false);
  });

  it("rejects 6 digits", () => {
    expect(CustomerIdSchema.safeParse("CUST-100001").success).toBe(false);
  });

  it("rejects trailing space", () => {
    expect(CustomerIdSchema.safeParse("CUST-10001 ").success).toBe(false);
  });

  it("rejects trailing newline", () => {
    expect(CustomerIdSchema.safeParse("CUST-12345\n").success).toBe(false);
  });

  it("rejects non-string types", () => {
    expect(CustomerIdSchema.safeParse(12345).success).toBe(false);
    expect(CustomerIdSchema.safeParse({}).success).toBe(false);
    expect(CustomerIdSchema.safeParse(true).success).toBe(false);
  });

  it("rejects null and undefined", () => {
    expect(CustomerIdSchema.safeParse(null).success).toBe(false);
    expect(CustomerIdSchema.safeParse(undefined).success).toBe(false);
  });
});

describe("AmountSchema", () => {
  it("accepts valid positive numbers with up to 2 decimal places", () => {
    expect(AmountSchema.safeParse(50).success).toBe(true);
    expect(AmountSchema.safeParse(10.99).success).toBe(true);
    expect(AmountSchema.safeParse(0.01).success).toBe(true);
    expect(AmountSchema.safeParse(10000).success).toBe(true);
  });

  it("accepts 0.01 minimum valid cent", () => {
    expect(AmountSchema.safeParse(0.01).success).toBe(true);
  });

  it("accepts floating point 0.1 + 0.2 representation", () => {
    expect(AmountSchema.safeParse(0.1 + 0.2).success).toBe(true);
  });

  it("rejects 0", () => {
    expect(AmountSchema.safeParse(0).success).toBe(false);
  });

  it("rejects negative amounts", () => {
    expect(AmountSchema.safeParse(-0.01).success).toBe(false);
    expect(AmountSchema.safeParse(-50).success).toBe(false);
  });

  it("rejects sub-cent amounts", () => {
    expect(AmountSchema.safeParse(1e-9).success).toBe(false);
    expect(AmountSchema.safeParse(5e-9).success).toBe(false);
    expect(AmountSchema.safeParse(0.001).success).toBe(false);
    expect(AmountSchema.safeParse(0.004).success).toBe(false);
    expect(AmountSchema.safeParse(0.005).success).toBe(false);
    expect(AmountSchema.safeParse(Number.MIN_VALUE).success).toBe(false);
  });

  it("rejects NaN", () => {
    expect(AmountSchema.safeParse(NaN).success).toBe(false);
  });

  it("rejects Infinity and -Infinity", () => {
    expect(AmountSchema.safeParse(Infinity).success).toBe(false);
    expect(AmountSchema.safeParse(-Infinity).success).toBe(false);
  });

  it("rejects numeric string '50'", () => {
    expect(AmountSchema.safeParse("50").success).toBe(false);
  });

  it("rejects more than 2 decimal places like 10.999", () => {
    expect(AmountSchema.safeParse(10.999).success).toBe(false);
    expect(AmountSchema.safeParse(0.001).success).toBe(false);
  });

  it("rejects values above the 10000 cap", () => {
    expect(AmountSchema.safeParse(10000.01).success).toBe(false);
    expect(AmountSchema.safeParse(10001).success).toBe(false);
    expect(AmountSchema.safeParse(1e21).success).toBe(false);
  });

  it("rejects null and undefined", () => {
    expect(AmountSchema.safeParse(null).success).toBe(false);
    expect(AmountSchema.safeParse(undefined).success).toBe(false);
  });
});

describe("ReasonSchema", () => {
  it("accepts reason with exactly 10 characters", () => {
    expect(ReasonSchema.safeParse("1234567890").success).toBe(true);
  });

  it("accepts reason with valid trimmed length above 10", () => {
    expect(ReasonSchema.safeParse("Customer requested full refund for delayed item").success).toBe(true);
  });

  it("rejects reason with 9 characters", () => {
    expect(ReasonSchema.safeParse("123456789").success).toBe(false);
  });

  it("rejects 10 spaces", () => {
    expect(ReasonSchema.safeParse("          ").success).toBe(false);
  });

  it("rejects non-string types", () => {
    expect(ReasonSchema.safeParse(1234567890).success).toBe(false);
    expect(ReasonSchema.safeParse({}).success).toBe(false);
  });

  it("rejects missing / undefined", () => {
    expect(ReasonSchema.safeParse(undefined).success).toBe(false);
  });

  it("rejects string longer than 500 characters", () => {
    const longString = "a".repeat(501);
    expect(ReasonSchema.safeParse(longString).success).toBe(false);
  });
});

describe("Tool input schemas (.strict and edge cases)", () => {
  it("GetCustomerRecordInputSchema rejects unknown keys", () => {
    const res = GetCustomerRecordInputSchema.safeParse({
      customer_id: "CUST-10001",
      extra: "unexpected",
    });
    expect(res.success).toBe(false);
  });

  it("GetCustomerRecordInputSchema rejects undefined or non-object arguments", () => {
    expect(GetCustomerRecordInputSchema.safeParse(undefined).success).toBe(false);
    expect(GetCustomerRecordInputSchema.safeParse(null).success).toBe(false);
    expect(GetCustomerRecordInputSchema.safeParse("string").success).toBe(false);
    expect(GetCustomerRecordInputSchema.safeParse([]).success).toBe(false);
  });

  it("TriggerRefundInputSchema rejects unknown keys", () => {
    const res = TriggerRefundInputSchema.safeParse({
      customer_id: "CUST-10001",
      amount: 45.5,
      reason: "Item arrived damaged during transit",
      extra_field: true,
    });
    expect(res.success).toBe(false);
  });

  it("TriggerRefundInputSchema rejects undefined or non-object arguments", () => {
    expect(TriggerRefundInputSchema.safeParse(undefined).success).toBe(false);
    expect(TriggerRefundInputSchema.safeParse(null).success).toBe(false);
    expect(TriggerRefundInputSchema.safeParse(123).success).toBe(false);
  });
});
