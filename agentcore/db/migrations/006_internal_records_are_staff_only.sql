-- ===========================================================================
-- 006_internal_records_are_staff_only -- close an internal-data leak
--
-- THE BUG
--
-- Staff act tenant-wide, so their runs, conversations, steps and retrieval
-- candidates carry account_id = NULL. The policies in 001 read:
--
--     account_id IS NULL OR app_can_see_account(account_id)
--
-- `IS NULL` was meant to say "this is a tenant-wide record". What it actually
-- said was "visible to EVERYONE in the tenant" -- so any customer could read an
-- operations user's run: their question, the answer, the reasoning trace, and
-- the retrieval candidates spanning every account.
--
-- Found by auditing what a customer principal could actually reach through the
-- API rather than by reading the policy: `GET /api/chat/{ops_run_id}` returned
-- 200 for a customer. The policy looked correct in isolation; only exercising
-- it revealed that NULL was doing the opposite of what was intended.
--
-- THE FIX
--
-- A NULL account is an INTERNAL record, so it requires staff. Customers see
-- exactly the rows carrying their own account and nothing else.
--
-- Note what does NOT change: documents and chunks keep
-- `owner_account_id IS NULL OR ...`, because there NULL means "a general policy
-- document" and every customer is supposed to read those. The same SQL shape
-- meant two different things in two places, which is precisely how the mistake
-- survived review.
-- ===========================================================================

DROP POLICY IF EXISTS runs_scope ON runs;
CREATE POLICY runs_scope ON runs
    USING (
        tenant_id = app_tenant()
        AND CASE
            -- Internal, tenant-wide run: staff only.
            WHEN account_id IS NULL THEN app_is_staff()
            ELSE app_can_see_account(account_id)
        END
    );

DROP POLICY IF EXISTS run_steps_scope ON run_steps;
CREATE POLICY run_steps_scope ON run_steps
    USING (
        tenant_id = app_tenant()
        AND CASE
            WHEN account_id IS NULL THEN app_is_staff()
            ELSE app_can_see_account(account_id)
        END
    );

DROP POLICY IF EXISTS retrieval_candidates_scope ON retrieval_candidates;
CREATE POLICY retrieval_candidates_scope ON retrieval_candidates
    USING (
        tenant_id = app_tenant()
        AND CASE
            WHEN account_id IS NULL THEN app_is_staff()
            ELSE app_can_see_account(account_id)
        END
    );

DROP POLICY IF EXISTS conversations_scope ON conversations;
CREATE POLICY conversations_scope ON conversations
    USING (
        tenant_id = app_tenant()
        AND CASE
            WHEN account_id IS NULL THEN app_is_staff()
            ELSE app_can_see_account(account_id)
        END
    );

-- audit_log rows recorded by staff also carry a NULL account. The audit
-- endpoint is already staff-gated in the API, but the policy should not be the
-- weaker of the two: defence at the data layer must not depend on a decorator
-- someone might omit on a future endpoint.
DROP POLICY IF EXISTS audit_log_read ON audit_log;
CREATE POLICY audit_log_read ON audit_log FOR SELECT
    USING (
        tenant_id = app_tenant()
        AND CASE
            WHEN account_id IS NULL THEN app_is_staff()
            ELSE app_can_see_account(account_id)
        END
    );
