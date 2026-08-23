-- ===========================================================================
-- 003_health_grants -- let the runtime role answer its own readiness probe
--
-- `healthcheck()` runs on the pool, as parcelpilot_app, and reports the applied
-- schema revision. But 001 granted SELECT only on the business tables, so the
-- readiness endpoint failed with "permission denied for table
-- schema_migrations" -- a load balancer would have pulled every replica out of
-- rotation on a healthy system.
--
-- Found by running `parcelpilot db health` against a freshly bootstrapped
-- database. It did not surface earlier because the first database was created
-- before the grants existed and had accumulated looser permissions -- which is
-- exactly why bootstrap-from-scratch belongs in the test path.
--
-- SELECT only. The runtime role must never be able to rewrite migration
-- history: that is how a schema silently disagrees with the repository.
-- ===========================================================================

GRANT SELECT ON schema_migrations TO parcelpilot_app;

-- ---------------------------------------------------------------------------
-- Readiness needs to see the active index version, but the readiness probe has
-- no principal -- and RLS on index_versions correctly hides every row from an
-- unscoped session. So the probe would always report "no active index" on a
-- perfectly healthy system.
--
-- The fix is a narrow SECURITY DEFINER function rather than loosening the
-- policy. It runs as the owner (bypassing RLS), takes the tenant explicitly,
-- and returns only counts -- no document text, no account identifiers, nothing
-- that could turn a health endpoint into a data-disclosure surface.
--
-- search_path is pinned: a SECURITY DEFINER function with a mutable
-- search_path is a privilege-escalation vector, because a caller who can create
-- objects could shadow a name it resolves.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app_active_index(p_tenant text)
    RETURNS TABLE (
        index_version_id bigint,
        document_count   integer,
        chunk_count      integer,
        embedded_count   integer,
        embedding_model  text,
        activated_at     timestamptz
    )
    LANGUAGE sql
    STABLE
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $$
        SELECT index_version_id, document_count, chunk_count,
               embedded_count, embedding_model, activated_at
        FROM index_versions
        WHERE tenant_id = p_tenant AND status = 'active'
    $$;

-- EXECUTE is revoked from PUBLIC first: a SECURITY DEFINER function is
-- executable by everyone by default, and the grant should be explicit.
REVOKE ALL ON FUNCTION app_active_index(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_active_index(text) TO parcelpilot_app;
