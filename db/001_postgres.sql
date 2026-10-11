-- Applied by the admin-only bootstrap, never by application startup.
CREATE SCHEMA IF NOT EXISTS staff_matching;
REVOKE ALL ON SCHEMA staff_matching FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE TABLE staff_matching.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE staff_matching.tenants (
    id text PRIMARY KEY,
    revision bigint NOT NULL CHECK (revision > 0)
);
CREATE TABLE staff_matching.staff (
    tenant_id text NOT NULL REFERENCES staff_matching.tenants(id),
    id text NOT NULL,
    body jsonb NOT NULL,
    origin public.geography(Point, 4326) NOT NULL,
    skill_vector public.vector(4) NOT NULL,
    PRIMARY KEY (tenant_id, id),
    CHECK (body->>'id' = id)
);
CREATE TABLE staff_matching.orders (
    tenant_id text NOT NULL REFERENCES staff_matching.tenants(id),
    id text NOT NULL,
    body jsonb NOT NULL,
    site public.geography(Point, 4326) NOT NULL,
    skill_vector public.vector(4) NOT NULL,
    PRIMARY KEY (tenant_id, id),
    CHECK (body->>'id' = id)
);
CREATE TABLE staff_matching.evidence (
    tenant_id text NOT NULL,
    staff_id text NOT NULL,
    order_id text NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY (tenant_id, staff_id, order_id),
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_matching.staff(tenant_id, id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES staff_matching.orders(tenant_id, id),
    CHECK (body->>'staff_id' = staff_id AND body->>'order_id' = order_id)
);
CREATE TABLE staff_matching.history_source (
    tenant_id text NOT NULL,
    id text NOT NULL,
    staff_id text NOT NULL,
    order_id text NOT NULL,
    rating double precision NOT NULL CHECK (rating BETWEEN 0 AND 1),
    provenance jsonb NOT NULL CHECK (provenance->>'synthetic' = 'true'),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, staff_id, order_id)
        REFERENCES staff_matching.evidence(tenant_id, staff_id, order_id)
);
CREATE TABLE staff_matching.runs (
    tenant_id text NOT NULL REFERENCES staff_matching.tenants(id),
    id text NOT NULL,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    created_at timestamptz NOT NULL,
    input_revision bigint NOT NULL,
    config jsonb NOT NULL,
    snapshot jsonb NOT NULL,
    algorithm_config jsonb NOT NULL,
    owner_id text NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    error text,
    result jsonb,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE staff_matching.proposals (
    tenant_id text NOT NULL,
    id text NOT NULL,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    run_id text NOT NULL,
    staff_id text NOT NULL,
    order_id text NOT NULL,
    status text NOT NULL CHECK
        (status IN ('proposed', 'consented', 'declined', 'expired', 'confirmed')),
    version integer NOT NULL CHECK (version > 0),
    input_revision bigint NOT NULL,
    staff_version integer NOT NULL,
    order_version integer NOT NULL,
    evidence_version integer NOT NULL,
    expires_at timestamptz NOT NULL,
    responded_at timestamptz,
    confirmed_assignment_id text,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, run_id, staff_id, order_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES staff_matching.runs(tenant_id, id),
    FOREIGN KEY (tenant_id, staff_id, order_id)
        REFERENCES staff_matching.evidence(tenant_id, staff_id, order_id)
);
CREATE TABLE staff_matching.assignments (
    tenant_id text NOT NULL,
    id text NOT NULL,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    staff_id text NOT NULL,
    order_id text NOT NULL,
    proposal_id text,
    source_run_id text,
    occupied daterange NOT NULL CHECK (NOT isempty(occupied)),
    body jsonb NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, proposal_id),
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff_matching.staff(tenant_id, id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES staff_matching.orders(tenant_id, id),
    FOREIGN KEY (tenant_id, proposal_id) REFERENCES staff_matching.proposals(tenant_id, id),
    FOREIGN KEY (tenant_id, source_run_id) REFERENCES staff_matching.runs(tenant_id, id),
    CHECK (body->>'staff_id' = staff_id AND body->>'order_id' = order_id),
    CHECK (occupied = daterange(
        (body->'period'->>'start')::date, (body->'period'->>'end')::date, '[)')),
    EXCLUDE USING gist (tenant_id WITH =, staff_id WITH =, occupied WITH &&)
);
ALTER TABLE staff_matching.proposals ADD CONSTRAINT confirmed_assignment_fk
    FOREIGN KEY (tenant_id, confirmed_assignment_id)
    REFERENCES staff_matching.assignments(tenant_id, id);

CREATE TABLE staff_matching.response_events (
    id bigint GENERATED ALWAYS AS IDENTITY,
    tenant_id text NOT NULL,
    proposal_id text NOT NULL,
    decision text NOT NULL,
    from_status text NOT NULL,
    to_status text NOT NULL,
    proposal_version integer NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, proposal_id) REFERENCES staff_matching.proposals(tenant_id, id)
);
CREATE TABLE staff_matching.audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY,
    tenant_id text NOT NULL REFERENCES staff_matching.tenants(id),
    event text NOT NULL,
    proposal_id text NOT NULL,
    assignment_id text,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, proposal_id) REFERENCES staff_matching.proposals(tenant_id, id),
    FOREIGN KEY (tenant_id, assignment_id) REFERENCES staff_matching.assignments(tenant_id, id)
);
CREATE TABLE staff_matching.idempotency (
    tenant_id text NOT NULL REFERENCES staff_matching.tenants(id),
    operation text NOT NULL,
    key text NOT NULL,
    fingerprint text NOT NULL,
    response jsonb NOT NULL,
    PRIMARY KEY (tenant_id, operation, key)
);
CREATE INDEX runs_recent ON staff_matching.runs(tenant_id, created_at DESC, sequence DESC);
CREATE INDEX runs_leases ON staff_matching.runs(tenant_id, lease_expires_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX proposals_active ON staff_matching.proposals(tenant_id, status, expires_at);

DO $rls$
DECLARE
    table_name text;
    tenant_column text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'tenants', 'staff', 'orders', 'evidence', 'history_source', 'runs', 'proposals',
        'assignments', 'response_events', 'audit_events', 'idempotency'
    ] LOOP
        tenant_column := CASE WHEN table_name = 'tenants' THEN 'id' ELSE 'tenant_id' END;
        EXECUTE format('ALTER TABLE staff_matching.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE staff_matching.%I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY tenant_scope ON staff_matching.%I USING '
            || '(%I = nullif(current_setting(''app.tenant_id'', true), '''')) WITH CHECK '
            || '(%I = nullif(current_setting(''app.tenant_id'', true), ''''))',
            table_name, tenant_column, tenant_column
        );
    END LOOP;
END
$rls$;

-- This defense also covers writers outside the HTTP adapter. All lifecycle writers
-- take the tenant lock first, then an order lock, so capacity is checked atomically.
CREATE FUNCTION staff_matching.check_assignment_capacity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, staff_matching, pg_temp AS $capacity$
DECLARE
    order_body jsonb;
BEGIN
    PERFORM 1 FROM staff_matching.tenants WHERE id = NEW.tenant_id FOR UPDATE;
    SELECT body INTO STRICT order_body FROM staff_matching.orders
        WHERE tenant_id = NEW.tenant_id AND id = NEW.order_id FOR UPDATE;
    IF NEW.occupied != daterange(
        (order_body->'period'->>'start')::date,
        (order_body->'period'->>'end')::date, '[)'
    ) THEN
        RAISE EXCEPTION 'assignment must cover the full order period' USING ERRCODE = '23514';
    END IF;
    IF (SELECT count(*) FROM staff_matching.assignments
        WHERE tenant_id = NEW.tenant_id AND order_id = NEW.order_id AND id != NEW.id)
        >= (order_body->>'headcount')::integer THEN
        RAISE EXCEPTION 'order capacity exhausted' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$capacity$;
CREATE TRIGGER assignment_capacity BEFORE INSERT OR UPDATE
    ON staff_matching.assignments FOR EACH ROW
    EXECUTE FUNCTION staff_matching.check_assignment_capacity();
REVOKE ALL ON FUNCTION staff_matching.check_assignment_capacity() FROM PUBLIC;

-- No graph name, tenant override, or Cypher text is accepted from a caller.
-- Only this fixed read operation is available to the restricted runtime role.
CREATE FUNCTION staff_matching.feature_evidence() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, ag_catalog, staff_matching, public, pg_temp
SET row_security = on AS $features$
DECLARE
    tenant text := nullif(current_setting('app.tenant_id', true), '');
    graph_name text;
    graph_rows jsonb;
    pair_rows jsonb;
    versions jsonb;
BEGIN
    IF tenant IS NULL OR NOT EXISTS (SELECT 1 FROM staff_matching.tenants WHERE id = tenant) THEN
        RAISE EXCEPTION 'tenant context required' USING ERRCODE = '42501';
    END IF;
    graph_name := 'sm_' || md5(tenant);
    EXECUTE format(
        $query$
        SELECT coalesce(jsonb_agg(jsonb_build_object(
            'staff_id', (staff_id::text)::jsonb,
            'order_id', (order_id::text)::jsonb,
            'rated_assignments', (rated_count::text)::bigint,
            'history_fit', (mean_rating::text)::double precision
        )), '[]'::jsonb)
        FROM ag_catalog.cypher(%L, %L) AS g(
            staff_id ag_catalog.agtype, order_id ag_catalog.agtype,
            rated_count ag_catalog.agtype, mean_rating ag_catalog.agtype
        )
        $query$,
        graph_name,
        'MATCH (s:Staff)-[:COMPLETED]->(h:History)-[:FOR_ORDER]->(o:Order) '
        || 'RETURN s.id, o.id, count(h), avg(h.rating)'
    ) INTO graph_rows;
    IF EXISTS (
        SELECT 1
        FROM (
            SELECT staff_id, order_id, count(*) AS rated_assignments, avg(rating) AS history_fit
            FROM staff_matching.history_source WHERE tenant_id = tenant
            GROUP BY staff_id, order_id
        ) source
        FULL JOIN jsonb_to_recordset(graph_rows) AS projected(
            staff_id text, order_id text, rated_assignments bigint, history_fit double precision
        ) USING (staff_id, order_id)
        WHERE source.rated_assignments IS DISTINCT FROM projected.rated_assignments
            OR abs(source.history_fit - projected.history_fit) > 0.0000001
    ) THEN
        RAISE EXCEPTION 'history projection differs from authoritative source; rerun admin setup'
            USING ERRCODE = '55000';
    END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object(
        'staff_id', e.staff_id, 'order_id', e.order_id,
        'spatial_distance_m', public.ST_Distance(s.origin, o.site),
        'vector_distance', nullif(
            s.skill_vector OPERATOR(public.<=>) o.skill_vector, 'NaN'::double precision),
        'rated_assignments', coalesce(g.rated_assignments, 0),
        'history_fit', g.history_fit,
        'source_history', (
            SELECT coalesce(jsonb_agg(jsonb_build_object(
                'id', h.id, 'rating', h.rating, 'provenance', h.provenance
            ) ORDER BY h.id), '[]'::jsonb)
            FROM staff_matching.history_source h
            WHERE h.tenant_id = tenant AND h.staff_id = e.staff_id AND h.order_id = e.order_id
        )
    ) ORDER BY e.order_id, e.staff_id), '[]'::jsonb)
    INTO pair_rows
    FROM staff_matching.evidence e
    JOIN staff_matching.staff s ON (s.tenant_id, s.id) = (e.tenant_id, e.staff_id)
    JOIN staff_matching.orders o ON (o.tenant_id, o.id) = (e.tenant_id, e.order_id)
    LEFT JOIN jsonb_to_recordset(graph_rows) AS g(
        staff_id text, order_id text, rated_assignments bigint, history_fit double precision
    ) ON (g.staff_id, g.order_id) = (e.staff_id, e.order_id)
    WHERE e.tenant_id = tenant;
    SELECT jsonb_object_agg(extname, extversion) INTO versions
        FROM pg_catalog.pg_extension
        WHERE extname IN ('postgis', 'vector', 'btree_gist', 'age');
    RETURN jsonb_build_object(
        'tenant_id', tenant,
        'synthetic', true,
        'extensions', versions,
        'pairs', pair_rows,
        'history_source_count',
            (SELECT count(*) FROM staff_matching.history_source WHERE tenant_id = tenant),
        'graph_history_count',
            (SELECT coalesce(sum((value->>'rated_assignments')::bigint), 0)
             FROM jsonb_array_elements(graph_rows)),
        'graph_matches_source', true,
        'evidence_explanation', jsonb_build_object(
            'spatial', 'PostGIS geography distance between fictional Tokyo points; not routing.',
            'vector', 'pgvector cosine distance over [office,forklift,packing,rare] skill levels; '
                || 'deterministic structured features, NOT OpenAI or other model embeddings.',
            'history', 'AGE Cypher count(h), avg(h.rating) over tenant-isolated synthetic '
                || 'Staff-COMPLETED-History-FOR_ORDER-Order paths. SQL history_source is authoritative.',
            'scoring', 'Verified synthetic commute uses max(15,ceil(distance_m/350+5)); '
                || 'unknown commutes remain unknown. SkillFit = 0.9*fixture + 0.1*(1-cosine_distance). '
                || 'HistoryFit and rated_assignments come from AGE aggregates.',
            'isolation', 'FORCE RLS relational tables; no direct runtime AGE access; '
                || 'fixed SECURITY DEFINER routine derives the graph from transaction tenant context.'
        )
    );
END
$features$;
REVOKE ALL ON FUNCTION staff_matching.feature_evidence() FROM PUBLIC;
INSERT INTO staff_matching.schema_migrations(version) VALUES (1);
