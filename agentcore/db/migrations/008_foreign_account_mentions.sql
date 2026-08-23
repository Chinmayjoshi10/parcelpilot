-- ===========================================================================
-- 008_foreign_account_mentions -- refuse questions ABOUT another customer
--
-- THE REMAINING HALF OF THE CROSS-ACCOUNT BUG
--
-- Migration 007's sibling guard (in the orchestrator) catches a question that
-- names another account's RECORD: "what is the cancellation fee on ORD-2001?"
-- asked by ACCT-001 now refuses, because the scoped lookup returns zero rows and
-- that terminates the run.
--
-- It cannot catch a question that names another COMPANY, because there is no id
-- to look up:
--
--     Q (as ACCT-001): "What cancellation terms does LumenWorks have?"
--     A: "For Northstar Logistics (account ACCT-001), any booked shipment can be
--         cancelled before pickup without a cancellation fee..."
--
-- Nothing leaked -- LumenWorks' agreement was never read, and RLS is why. But
-- the answer is still wrong for the question asked: it describes one company's
-- contract in reply to a question about another's. An earlier build merged the
-- identities outright ("LumenWorks, as Northstar Logistics, can cancel...").
--
-- WHY A SECURITY DEFINER FUNCTION
--
-- Detecting this needs one fact the request path must not be able to read: the
-- set of account names in this tenant. Granting SELECT on `accounts` tenant-wide
-- to solve a tenancy bug would be self-defeating.
--
-- So the function returns a single boolean and never any data: "does this text
-- name an account you cannot see?" The caller learns one bit, about a name it
-- supplied itself, and cannot enumerate anything -- there is no way to ask "list
-- them" or "which one".
--
-- The refusal built on top of it is worded identically whether the named company
-- is a customer here or does not exist at all, so the bit never reaches the user
-- either. It only decides whether to stop.
--
-- `search_path` is pinned. A SECURITY DEFINER function without that is a
-- privilege-escalation primitive: anyone able to create a table earlier in the
-- resolution order chooses what the definer's body actually reads.
-- ===========================================================================

CREATE OR REPLACE FUNCTION app_names_foreign_account(probe text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM accounts a
        WHERE a.tenant_id = app_tenant()
          -- Named in the text the caller supplied. Word-boundary matched so
          -- "Axis Labs" does not fire on "axis" inside another word.
          AND probe ~* ('\m' || regexp_replace(a.account_name, '([\\^$.|?*+()\[\]{}])', '\\\1', 'g') || '\M')
          -- ...and NOT one the caller may see. Reuses the same predicate the
          -- row-level policies use, so this can never disagree with them: staff
          -- see every account in the tenant and therefore trip nothing.
          AND NOT app_can_see_account(a.account_id)
    );
$$;

COMMENT ON FUNCTION app_names_foreign_account(text) IS
    'True when the supplied text names an account in this tenant that the '
    'caller cannot see. Returns one bit and never any row: it exists so a '
    'question about another customer can be refused without granting the '
    'request path tenant-wide read on accounts.';

-- EXECUTE only. The runtime role still has no SELECT on other accounts' rows.
GRANT EXECUTE ON FUNCTION app_names_foreign_account(text) TO parcelpilot_app;
