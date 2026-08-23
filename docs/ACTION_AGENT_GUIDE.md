# ParcelPilot Action Agent & Action Ledger — Architecture & Validation Guide

## 1. How the Action Agent & Ledger Works

In high-stakes enterprise systems, an AI agent **must never execute state-changing actions directly or autonomously**. If an LLM executes a database mutation directly during a ReAct loop, a prompt injection or hallucination could move money or delete records without oversight.

ParcelPilot solves this with a **Server-Side Action Ledger** with **Human-in-the-Loop Confirmation**.

```
User Query: "Issue a ₹300 credit for ORD-2002"
    │
    ▼
1. PREPARE PHASE (Agent Step)
   • Tool `prepare_action` is invoked.
   • Computes payload & SHA-256 digest (`payload_sha256`).
   • Writes row to `pending_actions` table with status = 'pending'.
   • Returns `action_id`, human summary, and citation justification.
   • Target tables (`service_credits`, `orders`, `tickets`) ARE NOT MUTATED YET.
   • Engine responds with status = `AWAITING_CONFIRMATION`.
    │
    ▼
2. CONFIRMATION GATE (Human-in-the-Loop)
   • UI renders an Action Confirmation Card showing summary & evidence.
   • The client receives ONLY `action_id`. It NEVER receives or holds the payload!
   • The user clicks [Confirm] or [Reject].
    │
    ▼
3. EXECUTION PHASE (Server Ledger Step)
   • Client calls `POST /api/actions/{action_id}/confirm` sending ONLY `action_id`.
   • Server verifies:
     a) Role Permission (RBAC re-checked at confirm time).
     b) Expiry Check (`expires_at > now()`, TTL 15 mins).
     c) SHA-256 Digest Verification (Ensures DB row wasn't tampered with).
     d) Exactly-Once Execution (`WHERE status = 'pending'`).
   • Target table is updated (`INSERT INTO service_credits` / `UPDATE tickets`).
   • Immutable audit record is appended to `audit_log`.
   • Action status set to `'executed'`.
```

---

## 2. Action Types & Role Permission Matrix

| Action Type | What It Changes in DB | Who Can Prepare | Who Can Confirm |
|---|---|---|---|
| `ESCALATE_TICKET` | `UPDATE tickets SET status = 'escalated'` | All Roles | `SUPPORT_AGENT`, `OPERATIONS_ADMIN` |
| `CREATE_FOLLOW_UP` | `INSERT INTO follow_ups (...)` | All Roles | `SUPPORT_AGENT`, `OPERATIONS_ADMIN` |
| `ISSUE_SERVICE_CREDIT` | `INSERT INTO service_credits (...)` | `SUPPORT_AGENT`, `OPERATIONS_ADMIN` | `OPERATIONS_ADMIN` Only |
| `UPDATE_ORDER_STATUS` | `UPDATE orders SET status = 'CANCELLED'` | `SUPPORT_AGENT`, `OPERATIONS_ADMIN` | `OPERATIONS_ADMIN` Only |

---

## 3. Four Security Guarantees of the Action Ledger

1. **Client Never Holds Payload**: Confirmation takes `action_id` only. The client cannot tamper with amounts, account IDs, or parameters.
2. **Exactly-Once Execution**: Atomic PostgreSQL conditional update (`WHERE status = 'pending'`). Concurrent confirms race; only one wins. Subsequent calls return HTTP 409 Conflict.
3. **Re-Authorisation at Confirm Time**: Role permissions are checked when prepared AND re-checked at confirmation.
4. **Payload Digest Verification**: `payload_sha256` is re-hashed at confirmation time. If the row was modified, execution is refused.

---

# 4. Step-by-Step Guide: How to Validate It Works

### Step 1: Validate Ticket Escalation Action

1. **Log in as**: `support_agent` or `operations_admin`.
2. **Ask Chatbot**: `"Escalate ticket TKT-501"`
3. **Verify Preparation**:
   * Chatbot responds with a card: `"I have prepared an escalation for ticket TKT-501..."`
   * Check UI: Displays **[Confirm]** and **[Reject]** buttons.
   * **Database Check 1**: Run `SELECT status FROM tickets WHERE ticket_id = 'TKT-501';`
     * *Expected*: Status is still `'open'`. No state change has occurred yet!
   * **Database Check 2**: Run `SELECT action_id, status, summary FROM pending_actions WHERE status = 'pending';`
     * *Expected*: Row exists with status `'pending'` and summary matching the UI.
4. **Click [Confirm]**:
   * Chatbot UI updates to `"Action executed successfully"`.
5. **Verify Execution**:
   * **Database Check 3**: Run `SELECT status FROM tickets WHERE ticket_id = 'TKT-501';`
     * *Expected*: Status is now `'escalated'`!
   * **Database Check 4**: Run `SELECT * FROM audit_log ORDER BY occurred_at DESC LIMIT 1;`
     * *Expected*: Audit entry logged with actor ID, event `'action.escalate_ticket'`, and detail payload.

---

### Step 2: Validate Service Credit Issuance (Money Action & RBAC Gate)

1. **Log in as**: `support_agent`
2. **Ask Chatbot**: `"Issue a service credit of ₹300 for order ORD-2002"`
3. **Verify Preparation**:
   * Agent computes credit eligibility, cites LumenWorks contract, and prepares `issue_service_credit`.
   * **Database Check**: `SELECT count(*) FROM service_credits WHERE order_id = 'ORD-2002';`
     * *Expected*: Count is `0`.
4. **Test RBAC Restriction (Support Agent cannot confirm money)**:
   * Try confirming as `support_agent`.
   * *Expected*: API returns HTTP 403 Forbidden ("role support_agent may not confirm issue_service_credit").
5. **Log in as**: `operations_admin`
6. **Click [Confirm]**:
   * Action executes.
   * **Database Check**: `SELECT credit_id, amount, currency, account_id FROM service_credits WHERE order_id = 'ORD-2002';`
     * *Expected*: Row exists with `amount = 300.00`, `currency = 'INR'`, `account_id = 'ACCT-002'`.

---

### Step 3: Validate Action Rejection

1. **Log in as**: `operations_admin`
2. **Ask Chatbot**: `"Cancel order ORD-1001"`
3. **Verify Preparation**: Action prepared with status `'pending'`.
4. **Click [Reject]** (or send `POST /api/actions/{action_id}/reject` with reason `"Customer changed mind"`):
5. **Verify Rejection**:
   * **Database Check 1**: `SELECT status FROM pending_actions WHERE action_id = '{action_id}';`
     * *Expected*: Status is `'rejected'`.
   * **Database Check 2**: `SELECT status FROM orders WHERE order_id = 'ORD-1001';`
     * *Expected*: Order status remains `'BOOKED'`. It was NOT cancelled.

---

### Step 4: Validate Idempotency & Replay Protection

1. Take any already confirmed `action_id`.
2. Send duplicate confirmation: `POST /api/actions/{action_id}/confirm`
3. **Expected Result**:
   * API returns HTTP 409 Conflict (`ActionAlreadySettled: "this action is already executed"`).
   * No duplicate database row is inserted into `service_credits` or `audit_log`.
