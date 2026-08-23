-- ===========================================================================
-- 009_result_tables -- carry the records an answer is about, beside the answer
--
-- THE PROBLEM
--
-- Asked "show me all open P1 tickets across accounts", the system replied with
-- the DEFINITION of a P1 incident and the first-response targets. Every sentence
-- was correctly cited. Not one ticket was named.
--
-- The cause is the citation contract meeting a question it does not fit. A claim
-- must carry a verbatim quote from a document, and a database row has no quote.
-- So the model, asked for records, filled the answer with the only material it
-- COULD cite -- the policy text. The requirement that makes every other answer
-- trustworthy was actively distorting this one.
--
-- WHY A COLUMN
--
-- The fix is to stop asking a citation-bound component to narrate data it cannot
-- cite. A row needs no source because it IS the source; quoting a record against
-- itself is circular. So the rows travel beside the answer, built
-- deterministically from what the tools already returned, and the model is told
-- the table is displayed separately and to make claims only about rules.
--
-- Same shape as `action_notice` in migration 007: a fact the system knows about
-- itself, carried next to the answer rather than pushed through a model that
-- must cite everything. Persisted rather than computed on read so a replayed run
-- shows the rows as they were, not as they are now -- which is the whole point of
-- an auditable run log.
--
-- Nullable, because most answers are about a rule rather than a set.
-- ===========================================================================

ALTER TABLE runs ADD COLUMN IF NOT EXISTS tables_json jsonb;

COMMENT ON COLUMN runs.tables_json IS
    'Records the answer was about, formatted server-side. Never model output: a '
    'row is its own source, so it carries no citation and needs none.';
