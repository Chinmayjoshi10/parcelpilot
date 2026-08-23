-- ===========================================================================
-- 004_action_effects -- where confirmed actions actually land
--
-- The ledger in 001 records what a human APPROVED. These tables record what
-- was DONE. Keeping them separate matters: an approval that was never executed
-- and an execution that was never approved are different incidents, and a
-- single table cannot distinguish them.
--
-- Both carry the originating action_id, so every credit and every follow-up
-- traces back to the approval that authorised it and, through that, to the run
-- and the cited clause that justified it. That chain is the difference between
-- "the system issued a credit" and "here is who approved it, when, on what
-- evidence".
-- ===========================================================================

CREATE TABLE service_credits (
    credit_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    account_id    text NOT NULL,
    order_id      text,
    amount        numeric(14, 2) NOT NULL CHECK (amount > 0),
    currency      text NOT NULL,
    reason        text NOT NULL,
    -- The approval that authorised this. Unique, so one approval can never
    -- produce two credits even if execution is retried.
    action_id     uuid NOT NULL UNIQUE,
    -- The run whose cited answer justified it.
    run_id        uuid,
    issued_by     text NOT NULL,
    issued_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, account_id)
        REFERENCES accounts (tenant_id, account_id) ON DELETE CASCADE
);

CREATE INDEX idx_service_credits_account
    ON service_credits (tenant_id, account_id, issued_at DESC);

-- Supports the monthly aggregate cap in the Northstar agreement. The policy
-- engine reports the cap; enforcing it needs this month's issued total, which
-- is this index.
CREATE INDEX idx_service_credits_month
    ON service_credits (tenant_id, account_id, issued_at);

CREATE TABLE follow_ups (
    follow_up_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    account_id   text NOT NULL,
    subject      text NOT NULL,
    body         text,
    due_at       timestamptz,
    assigned_to  text,
    status       text NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open', 'done', 'cancelled')),
    action_id    uuid NOT NULL UNIQUE,
    run_id       uuid,
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, account_id)
        REFERENCES accounts (tenant_id, account_id) ON DELETE CASCADE
);

CREATE INDEX idx_follow_ups_scope
    ON follow_ups (tenant_id, account_id, status, created_at DESC);

-- ---------------------------------------------------------------------------
-- Row-level security, same shape as everything else
-- ---------------------------------------------------------------------------

ALTER TABLE service_credits ENABLE ROW LEVEL SECURITY;
ALTER TABLE follow_ups      ENABLE ROW LEVEL SECURITY;

CREATE POLICY service_credits_scope ON service_credits
    USING (tenant_id = app_tenant() AND app_can_see_account(account_id));

CREATE POLICY follow_ups_scope ON follow_ups
    USING (tenant_id = app_tenant() AND app_can_see_account(account_id));

-- ---------------------------------------------------------------------------
-- Grants
--
-- INSERT and SELECT only. No UPDATE, no DELETE: a credit that was issued
-- happened, and editing history is exactly what the audit trail exists to
-- prevent. Reversing a credit is a new, separately-approved action -- which
-- leaves both events visible.
--
-- follow_ups does get UPDATE, because closing one is a legitimate state change
-- rather than a rewrite of what occurred.
-- ---------------------------------------------------------------------------

GRANT SELECT, INSERT ON service_credits TO parcelpilot_app;
GRANT SELECT, INSERT, UPDATE ON follow_ups TO parcelpilot_app;
REVOKE UPDATE, DELETE ON service_credits FROM parcelpilot_app;
