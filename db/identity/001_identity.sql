-- Apply as a database administrator under the bootstrap advisory lock.
-- Reruns preserve account/session/key data and the separate migration timestamp.
-- Authentication is cross-tenant infrastructure: ACLs protect it before tenant selection.
-- The trusted server authorizes every operation; business tables retain their own RLS.
CREATE SCHEMA IF NOT EXISTS staff_identity;
REVOKE ALL ON SCHEMA staff_identity FROM PUBLIC, staff_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA staff_identity REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA staff_identity REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA staff_identity REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE IF NOT EXISTS staff_identity.schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS staff_identity.users (
    id uuid PRIMARY KEY,
    username text NOT NULL UNIQUE CHECK (length(username) BETWEEN 1 AND 128),
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 160),
    password_hash text NOT NULL CHECK (password_hash LIKE '$argon2id$%'),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    password_changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_login_at timestamptz
);
CREATE TABLE IF NOT EXISTS staff_identity.tenants (
    id text PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 160)
);
CREATE TABLE IF NOT EXISTS staff_identity.memberships (
    user_id uuid NOT NULL REFERENCES staff_identity.users(id),
    tenant_id text NOT NULL REFERENCES staff_identity.tenants(id),
    role text NOT NULL CHECK (role IN ('admin', 'coordinator', 'viewer')),
    enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (user_id, tenant_id)
);
CREATE TABLE IF NOT EXISTS staff_identity.sessions (
    id uuid PRIMARY KEY,
    token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    user_id uuid NOT NULL REFERENCES staff_identity.users(id),
    tenant_id text NOT NULL,
    auth_method text NOT NULL CHECK (auth_method IN ('password', 'passkey')),
    authenticated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    recent_auth_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT (clock_timestamp() + interval '8 hours'),
    revoked_at timestamptz,
    FOREIGN KEY (user_id, tenant_id)
        REFERENCES staff_identity.memberships(user_id, tenant_id)
);
CREATE INDEX IF NOT EXISTS sessions_user ON staff_identity.sessions(user_id);
CREATE TABLE IF NOT EXISTS staff_identity.credentials (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES staff_identity.users(id),
    credential_id bytea NOT NULL UNIQUE CHECK (octet_length(credential_id) BETWEEN 1 AND 1024),
    public_key bytea NOT NULL CHECK (octet_length(public_key) BETWEEN 1 AND 16384),
    sign_count bigint NOT NULL CHECK (sign_count BETWEEN 0 AND 4294967295),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    device_type text NOT NULL CHECK (device_type IN ('single_device', 'multi_device')),
    backed_up boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_used_at timestamptz
);
CREATE INDEX IF NOT EXISTS credentials_user ON staff_identity.credentials(user_id);
CREATE TABLE IF NOT EXISTS staff_identity.challenges (
    id uuid PRIMARY KEY,
    purpose text NOT NULL CHECK (purpose IN ('registration', 'authentication')),
    -- Challenges are public WebAuthn protocol bytes, not session/browser bearer secrets.
    challenge bytea NOT NULL CHECK (octet_length(challenge) >= 32),
    user_id uuid REFERENCES staff_identity.users(id),
    session_id uuid UNIQUE REFERENCES staff_identity.sessions(id) ON DELETE CASCADE,
    flow_hash bytea UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT (clock_timestamp() + interval '5 minutes'),
    CHECK (
        (purpose = 'registration' AND user_id IS NOT NULL
         AND session_id IS NOT NULL AND flow_hash IS NULL)
        OR
        (purpose = 'authentication' AND user_id IS NULL
         AND session_id IS NULL AND octet_length(flow_hash) = 32 AND flow_hash IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS challenges_expiry ON staff_identity.challenges(expires_at);
CREATE TABLE IF NOT EXISTS staff_identity.rate_limits (
    kind text NOT NULL CHECK (kind IN ('password_account', 'password_source', 'anonymous')),
    -- Fixed hash buckets bound cardinality even when attackers invent accounts/sources.
    bucket integer NOT NULL CHECK (bucket BETWEEN 0 AND 4095),
    window_start timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempts integer NOT NULL CHECK (attempts BETWEEN 1 AND 1000000),
    PRIMARY KEY (kind, bucket)
);
CREATE TABLE IF NOT EXISTS staff_identity.audit_events (
    id uuid PRIMARY KEY,
    user_id uuid REFERENCES staff_identity.users(id),
    event text NOT NULL,
    actor text NOT NULL,
    credential_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS identity_audit_user ON staff_identity.audit_events(user_id, created_at);

REVOKE ALL ON ALL TABLES IN SCHEMA staff_identity FROM PUBLIC, staff_app;
GRANT USAGE ON SCHEMA staff_identity TO staff_app;
GRANT SELECT ON staff_identity.schema_migrations, staff_identity.users,
    staff_identity.tenants, staff_identity.memberships TO staff_app;
GRANT UPDATE (password_hash, password_changed_at, last_login_at)
    ON staff_identity.users TO staff_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON staff_identity.sessions,
    staff_identity.credentials, staff_identity.challenges, staff_identity.rate_limits TO staff_app;
GRANT INSERT ON staff_identity.audit_events TO staff_app;
INSERT INTO staff_identity.schema_migrations(version) VALUES (1) ON CONFLICT (version) DO NOTHING;
