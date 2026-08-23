-- ===========================================================================
-- 002_tenants_rls -- close tenant enumeration
--
-- Found by tests/test_tenancy.py::test_rls_is_enabled_on_every_scoped_table,
-- which asserts that every table carrying a tenant_id column has row-level
-- security enabled. `tenants` was missed in 001 because its tenant_id is a
-- primary key rather than a foreign one, so it did not read like "scoped
-- data" -- but the table lists every customer organisation on the platform,
-- their names and their configuration.
--
-- Unpatched, a signed-in customer could enumerate the entire client roster.
-- That is a small leak with a large disclosure cost, and precisely the kind of
-- thing a structural test catches and a code review does not.
-- ===========================================================================

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;

-- A principal may read exactly the tenant they are scoped to, and nothing
-- else. Unscoped sessions see nothing, same as everywhere else.
CREATE POLICY tenants_scope ON tenants
    USING (tenant_id = app_tenant());
