-- AGE's parser requires a dollar-quoted Cypher argument even inside EXECUTE.
-- Replacing only the feature routine preserves all relational and graph data.
CREATE OR REPLACE FUNCTION staff_matching.feature_evidence() RETURNS jsonb
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
        FROM ag_catalog.cypher(%L, $history$
            MATCH (s:Staff)-[:COMPLETED]->(h:History)-[:FOR_ORDER]->(o:Order)
            RETURN s.id, o.id, count(h), avg(h.rating)
        $history$) AS g(
            staff_id ag_catalog.agtype, order_id ag_catalog.agtype,
            rated_count ag_catalog.agtype, mean_rating ag_catalog.agtype
        )
        $query$,
        graph_name
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
INSERT INTO staff_matching.schema_migrations(version) VALUES (2);
