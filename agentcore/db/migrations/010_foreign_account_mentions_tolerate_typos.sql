-- ===========================================================================
-- 010_foreign_account_mentions_tolerate_typos
--
-- THE THIRD TIME A GUARD FAILED ON SPELLING
--
-- Migration 008 refuses a question that names another customer. It matched the
-- account name with a word boundary, so it caught "What cancellation terms does
-- LumenWorks have?" and missed this:
--
--     Q (as ACCT-001): "what cancelation terms does lumework have"
--     A: "Northstar Logistics, operating under account ACCT-001, has a specific
--         agreement that allows cancellation of any booked shipment before
--         pickup without a cancellation fee..."
--
-- Six validated claims, six real citations, and an answer about the wrong
-- company. Nothing leaked -- LumenWorks' agreement was never read, and row-level
-- security is why -- but the reply describes one customer's contract in answer to
-- a question about another's, which is the exact failure 008 exists to prevent.
--
-- This is the same shape as the record-id defect in TECHNICAL_DECISIONS 5.13,
-- found the same way, on the same afternoon: a guard keyed on the surface form of
-- input inherits every way that form can vary. "LumenWorks" has to be typed
-- exactly, including the capital W and both plurals, or the guard silently does
-- not apply. Users type "lumework".
--
-- So the comparison stops being exact. Every word of at least four characters in
-- the caller's text is compared against each invisible account's name by trigram
-- similarity, and 0.35 or better counts as naming it. The margin is wide, not
-- marginal: on this corpus "lumework" scores 0.43 and "beakon" 0.40, while the
-- ordinary vocabulary of these questions -- cancelation, shipment, policy, terms,
-- account, logistics, enterprise -- all score 0.10 or below. A four-times gap is
-- what makes a threshold defensible rather than tuned.
--
-- SIMILARITY ALONE IS NOT ENOUGH, AND THE TEST SAID SO
--
-- An existing test asserts that "does the quill of a feather matter for
-- palletised freight?" must not fire on an account called Quillmark Retail.
-- Fuzzy matching broke it, and not marginally: "quill" against "quillmark"
-- scores 0.45 -- HIGHER than the real typo this migration exists for. No
-- threshold can separate them, because a short prefix of a name genuinely is
-- similar to it.
--
-- Length does separate them. A typo keeps roughly the length of the word it
-- fumbles: "beakon" is exactly as long as "beacon", "quilmark" and "lumenwork"
-- are one short, "lumework" two. A word that merely starts like a company name
-- is far shorter: "quill" is four short of "quillmark", "axis" four short of
-- "axislabs". So an approximate match must also be close in length, within two
-- characters, and an exact match still counts however long the name is -- which
-- is what keeps "what about Axis Labs" firing on its head word.
--
-- The cost is a known blind spot: an ABBREVIATION is not a typo, so "what does
-- Lumen charge" does not fire. That is the deliberate side to fail on. A missed
-- refusal answers a question about the wrong company; a false refusal blocks a
-- legitimate question and offers a human instead. Neither is good, and they are
-- not equally bad.
--
-- WHY NOT pg_trgm
--
-- `similarity()` would do this in one call, and pg_trgm is available here. But it
-- is an extension: `CREATE EXTENSION` needs privileges the migration role may not
-- have on a managed host, and a tenancy guard that fails to install is a tenancy
-- guard that is not running. The trigram definition is a dozen lines of SQL, so
-- it is written out. Verified against pg_trgm's `similarity()` on this corpus:
-- identical to two decimal places on every pair tested, including all of the
-- numbers quoted above.
--
-- The one-bit contract from 008 is unchanged. The function still returns a single
-- boolean about text the caller supplied itself, still reuses
-- `app_can_see_account` so it cannot disagree with the row policies, still has a
-- pinned `search_path`, and still cannot be used to enumerate anything.
-- ===========================================================================

-- Trigrams the way pg_trgm forms them: lower-cased, two leading spaces and one
-- trailing, so short strings still produce grams and word starts are weighted.
-- Jaccard over the distinct gram sets.
CREATE OR REPLACE FUNCTION app_trigram_similarity(a text, b text)
RETURNS real
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = public, pg_temp
AS $$
    WITH grams AS (
        SELECT 'a' AS side, substring(padded, i, 3) AS gram
        FROM (SELECT '  ' || lower(a) || ' ' AS padded) s,
             generate_series(1, length(s.padded) - 2) AS i
        UNION
        SELECT 'b', substring(padded, i, 3)
        FROM (SELECT '  ' || lower(b) || ' ' AS padded) s,
             generate_series(1, length(s.padded) - 2) AS i
    ),
    tally AS (
        SELECT count(*) FILTER (WHERE sides = 2) AS shared,
               count(*)                          AS total
        FROM (
            SELECT gram, count(DISTINCT side) AS sides
            FROM grams
            GROUP BY gram
        ) merged
    )
    SELECT CASE WHEN total = 0 THEN 0::real ELSE shared::real / total::real END
    FROM tally;
$$;

COMMENT ON FUNCTION app_trigram_similarity(text, text) IS
    'Jaccard similarity over padded trigrams, matching pg_trgm similarity(). '
    'Written out rather than depending on an extension, because a tenancy guard '
    'that fails to install is a tenancy guard that is not running.';


CREATE OR REPLACE FUNCTION app_names_foreign_account(probe text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    WITH raw AS (
        SELECT word, ord
        FROM regexp_split_to_table(lower(coalesce(probe, '')), '[^a-z0-9]+')
             WITH ORDINALITY AS t(word, ord)
        WHERE length(word) >= 2
    ),
    words AS (
        -- Four characters or more: shorter tokens carry too few trigrams to
        -- separate a name from a coincidence, and no account name here is
        -- shorter than "Axis".
        SELECT word FROM raw WHERE length(word) >= 4
        UNION
        -- Adjacent words joined, because a name typed with a space is still the
        -- name: "lumen works" splits into two tokens, and neither is within two
        -- characters of "lumenworks", so the length rule above rejected both.
        -- Joining them reproduces the squashed form exactly.
        --
        -- Both halves must be three characters or more. Joining across a short
        -- function word manufactures near-misses out of ordinary prose: "quill
        -- of a feather" yields "quillof", which is two characters from
        -- "quillmark" and duly fired the test that exists to stop exactly that.
        -- No account name here is built from a two-letter word.
        SELECT a.word || b.word
        FROM raw a JOIN raw b ON b.ord = a.ord + 1
        WHERE length(a.word) >= 3 AND length(b.word) >= 3
    ),
    hidden AS (
        SELECT
            -- Punctuation and spaces removed, so "LumenWorks" and "Lumen Works"
            -- compare the same way.
            lower(regexp_replace(a.account_name, '[^a-zA-Z0-9]', '', 'g')) AS squashed,
            -- The distinctive head word: someone writing about "Northstar
            -- Logistics" usually types "Northstar".
            lower(split_part(a.account_name, ' ', 1)) AS head
        FROM accounts a
        WHERE a.tenant_id = app_tenant()
          -- Reuses the row policies' own predicate, so this can never disagree
          -- with them. Staff see every account in the tenant and trip nothing.
          AND NOT app_can_see_account(a.account_id)
    )
    SELECT EXISTS (
        SELECT 1
        FROM hidden h, words w
        WHERE
            -- Written exactly, at any length: "axis" names "Axis Labs".
            w.word IN (h.squashed, h.head)
            -- Or fumbled: close in shape AND close in length, so a short prefix
            -- that merely resembles the name ("quill" of "quillmark") does not
            -- count while a real typo ("lumework" of "lumenworks") does.
            OR (
                app_trigram_similarity(w.word, h.squashed) >= 0.35
                AND abs(length(w.word) - length(h.squashed)) <= 2
            )
            OR (
                app_trigram_similarity(w.word, h.head) >= 0.35
                AND abs(length(w.word) - length(h.head)) <= 2
            )
    );
$$;

COMMENT ON FUNCTION app_names_foreign_account(text) IS
    'True when the supplied text names -- exactly or approximately -- an account '
    'in this tenant that the caller cannot see. Returns one bit and never any '
    'row, so a question about another customer can be refused without granting '
    'the request path tenant-wide read on accounts.';

GRANT EXECUTE ON FUNCTION app_trigram_similarity(text, text) TO parcelpilot_app;
GRANT EXECUTE ON FUNCTION app_names_foreign_account(text) TO parcelpilot_app;
