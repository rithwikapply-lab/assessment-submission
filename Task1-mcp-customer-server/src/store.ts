import crypto from "node:crypto";

export interface Customer {
  customer_id: string;
  name: string;
  email: string;
  status: "active" | "suspended" | "pending";
  balance: number;
}

export interface RefundResult {
  refund_id: string;
  customer_id: string;
  amount: number;
  status: "processed";
}

const INITIAL_CUSTOMERS: Customer[] = [
  {
    customer_id: "CUST-10001",
    name: "Alice Smith",
    email: "alice.smith@example.com",
    status: "active",
    balance: 1500.0,
  },
  {
    customer_id: "CUST-10002",
    name: "Bob Jones",
    email: "bob.jones@example.com",
    status: "active",
    balance: 250.5,
  },
  {
    customer_id: "CUST-10003",
    name: "Charlie Brown",
    email: "charlie.brown@example.com",
    status: "suspended",
    balance: 0.0,
  },
  {
    customer_id: "CUST-10004",
    name: "Diana Prince",
    email: "diana.prince@example.com",
    status: "active",
    balance: 8200.75,
  },
  {
    customer_id: "CUST-10005",
    name: "Evan Wright",
    email: "evan.wright@example.com",
    status: "active",
    balance: 50.0,
  },
];

class CustomerStore {
  private customers: Map<string, Customer>;

  constructor() {
    this.customers = new Map();
    this.reset();
  }

  public reset(): void {
    this.customers.clear();
    for (const cust of INITIAL_CUSTOMERS) {
      this.customers.set(cust.customer_id, { ...cust });
    }
  }

  public getCustomer(customerId: string): Customer | undefined {
    const customer = this.customers.get(customerId);
    if (!customer) {
      return undefined;
    }
    return { ...customer };
  }

  public processRefund(
    customerId: string,
    amount: number,
    _reason: string
  ): RefundResult | undefined {
    const customer = this.customers.get(customerId);
    if (!customer) {
      return undefined;
    }

    const refundId = crypto.randomUUID();
    // Update customer balance to reflect refund credit
    customer.balance = Math.round((customer.balance + amount) * 100) / 100;

    return {
      refund_id: refundId,
      customer_id: customerId,
      amount,
      status: "processed",
    };
  }
}

export const store = new CustomerStore();
