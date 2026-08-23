-- ===========================================================================
-- 005_operator_initiated_actions -- an action need not come from a run
--
-- 001 made pending_actions.run_id NOT NULL, on the assumption that every state
-- change originates in a question someone asked. That is wrong.
--
-- An operator acting on the proactive dashboard is proposing a change with no
-- run behind it: the detector found it deterministically, no conversation
-- happened. The frontend exposed this immediately -- there was no run_id to
-- supply, and passing a placeholder would have been a foreign-key violation.
--
-- So run_id becomes nullable, and provenance is recorded explicitly instead:
--
--   origin = 'agent'    -- proposed during a run, run_id set, justified by
--                          cited claims
--   origin = 'operator' -- proposed from the console, run_id null, justified by
--                          the detector's cited clause
--
-- That is better than a synthetic run, because "which cited answer authorised
-- this credit" and "which operator spotted it" are genuinely different
-- questions and the audit trail should be able to answer both.
-- ===========================================================================

ALTER TABLE pending_actions
    ALTER COLUMN run_id DROP NOT NULL;

ALTER TABLE pending_actions
    ADD COLUMN origin text NOT NULL DEFAULT 'agent'
        CHECK (origin IN ('agent', 'operator'));

-- An agent-proposed action must name its run; an operator-proposed one must
-- not. Enforced here so neither can be recorded without its provenance.
ALTER TABLE pending_actions
    ADD CONSTRAINT pending_actions_origin_consistent CHECK (
        (origin = 'agent' AND run_id IS NOT NULL)
        OR (origin = 'operator' AND run_id IS NULL)
    );

CREATE INDEX idx_pending_actions_origin
    ON pending_actions (tenant_id, origin, prepared_at DESC);

-- The effect tables carry run_id for the same reason and inherit the same
-- nullability.
ALTER TABLE service_credits ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE follow_ups ALTER COLUMN run_id DROP NOT NULL;
