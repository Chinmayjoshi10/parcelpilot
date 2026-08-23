-- ===========================================================================
-- 007_action_notice -- record that a requested action did NOT happen
--
-- THE BUG
--
-- A customer asked "Issue a service credit of INR 300 for ORD-2002" and was
-- told:
--
--     "A service credit of INR 300 has been prepared for ORD-2002, and a
--      person must confirm it."
--
-- Nothing had been prepared. Customers may not propose credits, so the ledger
-- write was refused and `pending_actions` held no such row. The model inferred
-- the state from an ELIGIBLE credit verdict plus an imperative question, and the
-- citation validator passed the answer because every quote in it was real.
--
-- This is worse than an unhelpful answer. Someone told their credit is queued
-- stops chasing it, and there is nothing to chase.
--
-- WHY A COLUMN AND NOT A CLAIM
--
-- The first attempt instructed the model to say the action had not been raised.
-- That could never work, for a structural reason worth writing down: no claim
-- may exist without a verbatim quote from a source document, and no clause in
-- the corpus says "you are not authorised to request this". The instruction
-- asked for an uncitable claim, so the model dropped it silently.
--
-- A fact about our OWN system state is not a claim about the world. We know it
-- deterministically, it needs no evidence, and routing it through a component
-- that must cite everything is the wrong shape. So it is recorded beside the
-- answer, the way `refusal_reason` already is, and rendered as a system notice
-- rather than as something the assistant said.
--
-- Nullable, because the overwhelmingly common case is that no action was
-- requested at all.
-- ===========================================================================

ALTER TABLE runs ADD COLUMN IF NOT EXISTS action_notice text;

COMMENT ON COLUMN runs.action_notice IS
    'Set when the user requested an action that was NOT staged. Server-authored '
    'plain language, never a model claim: there is no clause in the corpus to '
    'cite for "you are not authorised", so this cannot be a Claim.';
