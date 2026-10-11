-- Additive, independently versioned SaaS schema. Apply as setup administrator.
-- staff_app and staff_worker must already exist, without ownership/BYPASSRLS.
CREATE SCHEMA IF NOT EXISTS staff_saas;
REVOKE ALL ON SCHEMA staff_saas FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA staff_saas REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA staff_saas REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE IF NOT EXISTS staff_saas.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS staff_saas.tenants (
    tenant_id text PRIMARY KEY,
    dataset_version text NOT NULL,
    network_version text NOT NULL,
    cursor_secret bytea NOT NULL CHECK (octet_length(cursor_secret) = 32)
);
CREATE TABLE IF NOT EXISTS staff_saas.dataset_manifests (
    tenant_id text NOT NULL REFERENCES staff_saas.tenants(tenant_id),
    version text NOT NULL,
    counts jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, version)
);
CREATE TABLE IF NOT EXISTS staff_saas.companies (
    tenant_id text NOT NULL REFERENCES staff_saas.tenants(tenant_id),
    id text NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS staff_saas.sites (
    tenant_id text NOT NULL,
    id text NOT NULL,
    company_id text NOT NULL,
    body jsonb NOT NULL,
    location public.geography(Point, 4326) NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, id, company_id),
    FOREIGN KEY (tenant_id, company_id) REFERENCES staff_saas.companies(tenant_id, id)
);
CREATE INDEX IF NOT EXISTS saas_sites_location ON staff_saas.sites USING gist(location);
CREATE TABLE IF NOT EXISTS staff_saas.jobs (
    tenant_id text NOT NULL,
    id text NOT NULL,
    company_id text NOT NULL,
    site_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    body jsonb NOT NULL,
    features public.vector(32) NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, id, company_id),
    FOREIGN KEY (tenant_id, site_id, company_id)
        REFERENCES staff_saas.sites(tenant_id, id, company_id),
    FOREIGN KEY (tenant_id, company_id) REFERENCES staff_saas.companies(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS staff_saas.people (
    tenant_id text NOT NULL REFERENCES staff_saas.tenants(tenant_id),
    id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    summary jsonb NOT NULL,
    history jsonb NOT NULL,
    private_location public.geography(Point, 4326) NOT NULL,
    private_address text NOT NULL,
    contact_email text NOT NULL,
    features public.vector(32) NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS saas_people_location ON staff_saas.people USING gist(private_location);
CREATE INDEX IF NOT EXISTS saas_people_skills ON staff_saas.people USING gin((summary->'skills'));
CREATE TABLE IF NOT EXISTS staff_saas.simulation_grants (
    tenant_id text NOT NULL REFERENCES staff_saas.tenants(tenant_id),
    user_id text NOT NULL,
    PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE IF NOT EXISTS staff_saas.runs (
    tenant_id text NOT NULL REFERENCES staff_saas.tenants(tenant_id),
    id text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    progress integer NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    attempt integer NOT NULL DEFAULT 0,
    error text,
    request jsonb NOT NULL,
    coverage jsonb NOT NULL DEFAULT '{}',
    recommended_count integer NOT NULL DEFAULT 0,
    solver_status text,
    objective_bound double precision,
    model_version text NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS saas_one_active_run
    ON staff_saas.runs(tenant_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS staff_saas.candidates (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    job_id text NOT NULL,
    person_id text NOT NULL,
    rank integer NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY (tenant_id, run_id, job_id, person_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES staff_saas.runs(tenant_id, id),
    FOREIGN KEY (tenant_id, job_id) REFERENCES staff_saas.jobs(tenant_id, id),
    FOREIGN KEY (tenant_id, person_id) REFERENCES staff_saas.people(tenant_id, id)
);
CREATE INDEX IF NOT EXISTS saas_candidate_page
    ON staff_saas.candidates(tenant_id, run_id, job_id, rank, person_id);
CREATE TABLE IF NOT EXISTS staff_saas.introductions (
    tenant_id text NOT NULL,
    id text NOT NULL,
    run_id text NOT NULL,
    job_id text NOT NULL,
    person_id text NOT NULL,
    company_id text NOT NULL,
    candidate_alias text NOT NULL,
    status text NOT NULL CHECK (status IN (
        'shortlisted','requested','consented','declined','interview_proposed',
        'interview_accepted','placed','cancelled','expired'
    )),
    version integer NOT NULL DEFAULT 1,
    job_version integer NOT NULL,
    person_version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    simulated boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, id, job_id, person_id, company_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES staff_saas.runs(tenant_id, id),
    FOREIGN KEY (tenant_id, job_id, company_id)
        REFERENCES staff_saas.jobs(tenant_id, id, company_id),
    FOREIGN KEY (tenant_id, person_id) REFERENCES staff_saas.people(tenant_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS saas_active_introduction
    ON staff_saas.introductions(tenant_id, job_id, person_id)
    WHERE status NOT IN ('declined','cancelled','expired');
CREATE TABLE IF NOT EXISTS staff_saas.placements (
    tenant_id text NOT NULL,
    introduction_id text NOT NULL,
    job_id text NOT NULL,
    person_id text NOT NULL,
    job_snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, introduction_id),
    UNIQUE (tenant_id, job_id, person_id),
    FOREIGN KEY (tenant_id, introduction_id) REFERENCES staff_saas.introductions(tenant_id, id),
    FOREIGN KEY (tenant_id, job_id) REFERENCES staff_saas.jobs(tenant_id, id),
    FOREIGN KEY (tenant_id, person_id) REFERENCES staff_saas.people(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS staff_saas.appointments (
    tenant_id text NOT NULL,
    id text NOT NULL,
    introduction_id text NOT NULL,
    job_id text NOT NULL,
    person_id text NOT NULL,
    company_id text NOT NULL,
    start_at timestamptz NOT NULL,
    end_at timestamptz NOT NULL,
    site_id text,
    online boolean NOT NULL,
    status text NOT NULL CHECK (status IN ('proposed','accepted','cancelled')),
    version integer NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, introduction_id, job_id, person_id, company_id)
        REFERENCES staff_saas.introductions(tenant_id, id, job_id, person_id, company_id),
    FOREIGN KEY (tenant_id, site_id, company_id)
        REFERENCES staff_saas.sites(tenant_id, id, company_id),
    CHECK (end_at > start_at AND end_at - start_at <= interval '4 hours'),
    CHECK (online = (site_id IS NULL)),
    EXCLUDE USING gist (
        tenant_id WITH =, person_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&
    ) WHERE (status != 'cancelled'),
    EXCLUDE USING gist (
        tenant_id WITH =, site_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&
    ) WHERE (status != 'cancelled' AND site_id IS NOT NULL),
    -- No separate resource ID exists in v1: one online interview room/company.
    EXCLUDE USING gist (
        tenant_id WITH =, company_id WITH =, tstzrange(start_at, end_at, '[)') WITH &&
    ) WHERE (status != 'cancelled' AND online)
);
CREATE UNIQUE INDEX IF NOT EXISTS saas_active_appointment
    ON staff_saas.appointments(tenant_id, introduction_id) WHERE status != 'cancelled';
CREATE TABLE IF NOT EXISTS staff_saas.idempotency (
    tenant_id text NOT NULL REFERENCES staff_saas.tenants(tenant_id),
    user_id text NOT NULL,
    key text NOT NULL,
    operation text NOT NULL,
    fingerprint text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, user_id, key)
);
CREATE INDEX IF NOT EXISTS saas_mutation_rate
    ON staff_saas.idempotency(tenant_id,user_id,created_at);
CREATE TABLE IF NOT EXISTS staff_saas.audit_events (
    tenant_id text NOT NULL REFERENCES staff_saas.tenants(tenant_id),
    id text NOT NULL,
    actor text NOT NULL,
    event text NOT NULL,
    entity_id text NOT NULL,
    simulated boolean NOT NULL DEFAULT true CHECK (simulated),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS staff_saas.outbox (
    tenant_id text NOT NULL REFERENCES staff_saas.tenants(tenant_id),
    id text NOT NULL,
    body jsonb NOT NULL,
    delivered_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS saas_pending_outbox ON staff_saas.outbox(tenant_id, created_at)
    WHERE delivered_at IS NULL;
CREATE TABLE IF NOT EXISTS staff_saas.notifications (
    tenant_id text NOT NULL REFERENCES staff_saas.tenants(tenant_id),
    id text NOT NULL,
    audience text NOT NULL CHECK (audience IN ('management','company','person')),
    recipient_id text NOT NULL,
    introduction_id text,
    body jsonb NOT NULL,
    read boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, introduction_id) REFERENCES staff_saas.introductions(tenant_id, id)
);
CREATE INDEX IF NOT EXISTS saas_inbox_page
    ON staff_saas.notifications(tenant_id, audience, id);

-- Global dispatch is infrastructure, not a tenant business table. It contains
-- ONLY tenant/run IDs, timestamps, lease/attempt metadata: never profiles,
-- candidates, results, identity data, addresses, or notice payloads.
-- Neither runtime role can read/write it directly; fixed-search-path functions
-- are the only ACL boundary for discovering authorized worker tenant context.
CREATE TABLE IF NOT EXISTS staff_saas.dispatch (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending','leased','done','cancelled')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_until timestamptz,
    token text,
    attempt integer NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, run_id)
);
CREATE TABLE IF NOT EXISTS staff_saas.outbox_dispatch (
    tenant_id text PRIMARY KEY,
    touched_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

DO $$
DECLARE name text;
BEGIN
    FOREACH name IN ARRAY ARRAY[
        'tenants','dataset_manifests','companies','sites','jobs','people','simulation_grants',
        'runs','candidates','introductions','placements','appointments','idempotency',
        'audit_events','outbox','notifications'
    ] LOOP
        EXECUTE format('ALTER TABLE staff_saas.%I ENABLE ROW LEVEL SECURITY', name);
        EXECUTE format('ALTER TABLE staff_saas.%I FORCE ROW LEVEL SECURITY', name);
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_policies
            WHERE schemaname = 'staff_saas' AND tablename = name AND policyname = 'tenant_only'
        ) THEN
            EXECUTE format(
                'CREATE POLICY tenant_only ON staff_saas.%I USING '
                '(tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK '
                '(tenant_id = current_setting(''app.tenant_id'', true))', name
            );
        END IF;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION staff_saas.enqueue(p_tenant text, p_run text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, staff_saas AS $$
BEGIN
    IF p_tenant IS DISTINCT FROM current_setting('app.tenant_id', true) THEN
        RAISE EXCEPTION 'tenant context required' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_advisory_xact_lock(68127031);
    IF (SELECT count(*) FROM staff_saas.dispatch WHERE state IN ('pending','leased')) >= 100 THEN
        RETURN false;
    END IF;
    INSERT INTO staff_saas.dispatch(tenant_id, run_id, state) VALUES (p_tenant, p_run, 'pending');
    RETURN true;
END $$;

CREATE OR REPLACE FUNCTION staff_saas.claim(p_token text)
RETURNS TABLE(tenant_id text, run_id text, attempt integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, staff_saas AS $$
DECLARE selected record;
BEGIN
    SELECT d.* INTO selected FROM staff_saas.dispatch d
    WHERE d.state = 'pending' OR (d.state = 'leased' AND d.lease_until < clock_timestamp())
    ORDER BY coalesce(d.lease_until, d.created_at), d.tenant_id
    FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN; END IF;
    UPDATE staff_saas.dispatch d
    SET state = 'leased', token = p_token, attempt = d.attempt + 1,
        lease_until = clock_timestamp() + interval '90 seconds'
    WHERE d.tenant_id = selected.tenant_id AND d.run_id = selected.run_id;
    RETURN QUERY SELECT selected.tenant_id::text, selected.run_id::text, selected.attempt + 1;
END $$;

CREATE OR REPLACE FUNCTION staff_saas.lock_lease(
    p_tenant text, p_run text, p_token text, p_attempt integer
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, staff_saas AS $$
BEGIN
    IF p_tenant IS DISTINCT FROM current_setting('app.tenant_id', true) THEN RETURN false; END IF;
    PERFORM 1 FROM staff_saas.dispatch d WHERE d.tenant_id = p_tenant AND d.run_id = p_run
        AND d.state = 'leased' AND d.token = p_token AND d.attempt = p_attempt
        AND d.lease_until > clock_timestamp() FOR UPDATE;
    RETURN FOUND;
END $$;

CREATE OR REPLACE FUNCTION staff_saas.heartbeat(
    p_tenant text, p_run text, p_token text, p_attempt integer
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, staff_saas AS $$
BEGIN
    IF NOT staff_saas.lock_lease(p_tenant, p_run, p_token, p_attempt) THEN RETURN false; END IF;
    UPDATE staff_saas.dispatch SET lease_until = clock_timestamp() + interval '90 seconds'
        WHERE tenant_id = p_tenant AND run_id = p_run;
    RETURN true;
END $$;

CREATE OR REPLACE FUNCTION staff_saas.finish(
    p_tenant text, p_run text, p_token text, p_attempt integer, p_retry boolean
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, staff_saas AS $$
BEGIN
    IF NOT staff_saas.lock_lease(p_tenant, p_run, p_token, p_attempt) THEN RETURN false; END IF;
    UPDATE staff_saas.dispatch
        SET state = CASE WHEN p_retry THEN 'pending' ELSE 'done' END,
            created_at = clock_timestamp(), lease_until = NULL, token = NULL
        WHERE tenant_id = p_tenant AND run_id = p_run;
    RETURN true;
END $$;

CREATE OR REPLACE FUNCTION staff_saas.cancel_dispatch(p_tenant text, p_run text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, staff_saas AS $$
BEGIN
    IF p_tenant IS DISTINCT FROM current_setting('app.tenant_id', true) THEN
        RAISE EXCEPTION 'tenant context required' USING ERRCODE = '42501';
    END IF;
    UPDATE staff_saas.dispatch SET state = 'cancelled', token = NULL, lease_until = NULL
    WHERE tenant_id = p_tenant AND run_id = p_run AND state IN ('pending','leased');
END $$;

CREATE OR REPLACE FUNCTION staff_saas.wake_outbox(p_tenant text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, staff_saas AS $$
BEGIN
    IF p_tenant IS DISTINCT FROM current_setting('app.tenant_id', true) THEN
        RAISE EXCEPTION 'tenant context required' USING ERRCODE = '42501';
    END IF;
    INSERT INTO staff_saas.outbox_dispatch(tenant_id) VALUES (p_tenant)
        ON CONFLICT (tenant_id) DO NOTHING;
END $$;

CREATE OR REPLACE FUNCTION staff_saas.next_outbox()
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, staff_saas AS $$
DECLARE selected text;
BEGIN
    SELECT tenant_id INTO selected FROM staff_saas.outbox_dispatch
        ORDER BY touched_at, tenant_id FOR UPDATE SKIP LOCKED LIMIT 1;
    IF selected IS NOT NULL THEN
        UPDATE staff_saas.outbox_dispatch SET touched_at = clock_timestamp()
            WHERE tenant_id = selected;
    END IF;
    RETURN selected;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA staff_saas FROM PUBLIC, staff_app, staff_worker;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA staff_saas FROM PUBLIC, staff_app, staff_worker;
GRANT USAGE ON SCHEMA staff_saas TO staff_app, staff_worker;
GRANT SELECT ON staff_saas.schema_migrations, staff_saas.tenants,
    staff_saas.dataset_manifests, staff_saas.companies, staff_saas.sites,
    staff_saas.jobs, staff_saas.people, staff_saas.runs, staff_saas.candidates,
    staff_saas.introductions, staff_saas.placements, staff_saas.appointments,
    staff_saas.notifications TO staff_app, staff_worker;
GRANT SELECT ON staff_saas.simulation_grants, staff_saas.idempotency TO staff_app;
-- SELECT FOR UPDATE requires an UPDATE privilege; immutable tenant IDs remain
-- constrained by their foreign keys and the tenant WITH CHECK policy.
GRANT UPDATE (tenant_id) ON staff_saas.tenants TO staff_app, staff_worker;
GRANT INSERT, UPDATE ON staff_saas.runs, staff_saas.introductions,
    staff_saas.appointments TO staff_app;
GRANT INSERT ON staff_saas.placements, staff_saas.idempotency,
    staff_saas.audit_events, staff_saas.outbox TO staff_app;
GRANT UPDATE (read) ON staff_saas.notifications TO staff_app;
GRANT UPDATE ON staff_saas.runs TO staff_worker;
GRANT INSERT, DELETE ON staff_saas.candidates TO staff_worker;
GRANT SELECT, UPDATE ON staff_saas.outbox TO staff_worker;
GRANT INSERT ON staff_saas.notifications, staff_saas.audit_events TO staff_worker;
GRANT EXECUTE ON FUNCTION staff_saas.enqueue(text,text),
    staff_saas.cancel_dispatch(text,text), staff_saas.wake_outbox(text) TO staff_app;
GRANT EXECUTE ON FUNCTION staff_saas.claim(text), staff_saas.lock_lease(text,text,text,integer),
    staff_saas.heartbeat(text,text,text,integer),
    staff_saas.finish(text,text,text,integer,boolean), staff_saas.next_outbox() TO staff_worker;
INSERT INTO staff_saas.schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;
