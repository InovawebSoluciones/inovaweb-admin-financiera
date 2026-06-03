-- =====================================================================
-- inovaweb-admin-financiera  -  schema inicial
-- Convenciones:
--   * dinero: BIGINT en centavos (nunca floats)
--   * timestamps: TIMESTAMPTZ con default now()
--   * ids: BIGSERIAL PK
--   * append-only en: invoices, payments, adjustments, audit_log
--     (constraints en 002_security_constraints.sql)
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- ---------------------------------------------------------------------
-- IDENTIDAD Y ROLES
-- ---------------------------------------------------------------------

CREATE TABLE roles (
    id           BIGSERIAL PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE
                  CHECK (code IN (
                    'super_admin','finanzas','lectura',
                    'cliente_titular','cliente_usuario'
                  )),
    description  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO roles (code, description) VALUES
  ('super_admin',     'acceso total interno'),
  ('finanzas',        'operador financiero interno'),
  ('lectura',         'solo lectura interna'),
  ('cliente_titular', 'titular de cuenta cliente'),
  ('cliente_usuario', 'usuario adicional de cliente');

CREATE TABLE users (
    id              BIGSERIAL PRIMARY KEY,
    email           CITEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    full_name       TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    is_internal     BOOLEAN NOT NULL DEFAULT FALSE,
    last_login_at   TIMESTAMPTZ,
    failed_attempts INT NOT NULL DEFAULT 0,
    locked_until    TIMESTAMPTZ,
    client_id       BIGINT,  -- FK definida abajo (clients aun no existe)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_roles (
    user_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id  BIGINT NOT NULL REFERENCES roles(id),
    PRIMARY KEY (user_id, role_id)
);

-- ---------------------------------------------------------------------
-- CLIENTES (cuenta comercial)
-- ---------------------------------------------------------------------

CREATE TABLE clients (
    id                  BIGSERIAL PRIMARY KEY,
    legal_name          TEXT NOT NULL,
    trade_name          TEXT,
    rfc                 TEXT NOT NULL UNIQUE,
    cfdi_use            TEXT NOT NULL DEFAULT 'G03',
    tax_regime          TEXT NOT NULL,
    zip_code            TEXT NOT NULL,
    billing_email       CITEXT NOT NULL,
    contact_phone       TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','suspended','cancelled')),
    suspended_at        TIMESTAMPTZ,
    suspended_reason    TEXT,
    -- ids externos en los 4 cores Nivel 1
    medidor_account_id  TEXT,
    hub_account_id      TEXT,
    finanzas_account_id TEXT,
    messages_account_id TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE users
  ADD CONSTRAINT fk_users_client
  FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL;

CREATE INDEX idx_clients_status ON clients(status);
CREATE INDEX idx_users_client   ON users(client_id);

-- ---------------------------------------------------------------------
-- CATALOGOS
-- ---------------------------------------------------------------------

CREATE TABLE products (
    id           BIGSERIAL PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    description  TEXT,
    sat_key      TEXT NOT NULL,    -- ClaveProdServ SAT
    unit_sat_key TEXT NOT NULL,    -- ClaveUnidad SAT
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE services (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    -- a que core pertenece el consumo medido
    source_core     TEXT NOT NULL
                      CHECK (source_core IN ('medidor','hub','messages','finanzas','internal')),
    unit            TEXT NOT NULL,           -- e.g. 'request', 'sms', 'mb'
    unit_price_cents BIGINT NOT NULL CHECK (unit_price_cents >= 0),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plans (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    description     TEXT,
    monthly_fee_cents BIGINT NOT NULL DEFAULT 0 CHECK (monthly_fee_cents >= 0),
    is_free         BOOLEAN NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- limites incluidos y precios marginales por plan
CREATE TABLE plan_items (
    id                BIGSERIAL PRIMARY KEY,
    plan_id           BIGINT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    service_id        BIGINT NOT NULL REFERENCES services(id),
    included_units    BIGINT NOT NULL DEFAULT 0 CHECK (included_units >= 0),
    overage_price_cents BIGINT CHECK (overage_price_cents >= 0),
    hard_limit_units  BIGINT,  -- NULL = sin tope
    UNIQUE (plan_id, service_id)
);

CREATE TABLE subscriptions (
    id              BIGSERIAL PRIMARY KEY,
    client_id       BIGINT NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    plan_id         BIGINT NOT NULL REFERENCES plans(id),
    status          TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','paused','cancelled')),
    started_at      DATE NOT NULL DEFAULT CURRENT_DATE,
    ends_at         DATE,
    billing_day     SMALLINT NOT NULL DEFAULT 1
                      CHECK (billing_day BETWEEN 1 AND 28),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_subs_client ON subscriptions(client_id);
CREATE INDEX idx_subs_status ON subscriptions(status);

-- ---------------------------------------------------------------------
-- PROMOCIONES
-- ---------------------------------------------------------------------

CREATE TABLE promotions (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL
                      CHECK (kind IN (
                        'coupon','seasonal','volume','referral','free_plan'
                      )),
    discount_pct    NUMERIC(5,2) CHECK (discount_pct BETWEEN 0 AND 100),
    discount_cents  BIGINT CHECK (discount_cents >= 0),
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ NOT NULL,
    max_uses        INT,
    uses_count      INT NOT NULL DEFAULT 0,
    min_amount_cents BIGINT DEFAULT 0,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to > valid_from),
    CHECK (discount_pct IS NOT NULL OR discount_cents IS NOT NULL)
);

CREATE TABLE promotion_redemptions (
    id            BIGSERIAL PRIMARY KEY,
    promotion_id  BIGINT NOT NULL REFERENCES promotions(id),
    client_id     BIGINT NOT NULL REFERENCES clients(id),
    invoice_id    BIGINT,  -- FK abajo (invoices aun no existe)
    redeemed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    amount_applied_cents BIGINT NOT NULL CHECK (amount_applied_cents >= 0)
);

-- ---------------------------------------------------------------------
-- FACTURACION (append-only)
-- ---------------------------------------------------------------------

CREATE TABLE invoices (
    id                BIGSERIAL PRIMARY KEY,
    client_id         BIGINT NOT NULL REFERENCES clients(id),
    subscription_id   BIGINT REFERENCES subscriptions(id),
    period_start      DATE NOT NULL,
    period_end        DATE NOT NULL,
    subtotal_cents    BIGINT NOT NULL CHECK (subtotal_cents >= 0),
    discount_cents    BIGINT NOT NULL DEFAULT 0 CHECK (discount_cents >= 0),
    tax_cents         BIGINT NOT NULL DEFAULT 0 CHECK (tax_cents >= 0),
    total_cents       BIGINT NOT NULL CHECK (total_cents >= 0),
    status            TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft','pending_stamp','stamped','paid','cancelled','failed')),
    -- CFDI / PAC
    uuid_cfdi         UUID,
    pac_provider      TEXT,
    xml_path          TEXT,
    pdf_path          TEXT,
    stamped_at        TIMESTAMPTZ,
    paid_at           TIMESTAMPTZ,
    cancelled_at      TIMESTAMPTZ,
    cancel_reason     TEXT,
    -- contramedida append-only: rev/correccion
    supersedes_id     BIGINT REFERENCES invoices(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id BIGINT REFERENCES users(id),
    CHECK (period_end >= period_start)
);

CREATE INDEX idx_invoices_client ON invoices(client_id);
CREATE INDEX idx_invoices_status ON invoices(status);
CREATE INDEX idx_invoices_period ON invoices(period_start, period_end);

CREATE TABLE invoice_items (
    id           BIGSERIAL PRIMARY KEY,
    invoice_id   BIGINT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    service_id   BIGINT REFERENCES services(id),
    description  TEXT NOT NULL,
    quantity     NUMERIC(18,6) NOT NULL CHECK (quantity >= 0),
    unit_price_cents BIGINT NOT NULL CHECK (unit_price_cents >= 0),
    amount_cents BIGINT NOT NULL CHECK (amount_cents >= 0),
    sat_key      TEXT NOT NULL,
    unit_sat_key TEXT NOT NULL
);

ALTER TABLE promotion_redemptions
  ADD CONSTRAINT fk_redemptions_invoice
  FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE SET NULL;

CREATE TABLE payments (
    id            BIGSERIAL PRIMARY KEY,
    client_id     BIGINT NOT NULL REFERENCES clients(id),
    invoice_id    BIGINT REFERENCES invoices(id),
    amount_cents  BIGINT NOT NULL CHECK (amount_cents > 0),
    currency      TEXT NOT NULL DEFAULT 'MXN' CHECK (currency = 'MXN'),
    method        TEXT NOT NULL
                    CHECK (method IN ('hub_card','hub_spei','manual_spei','manual_cash','credit_balance')),
    hub_payment_id TEXT,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_by_user_id BIGINT REFERENCES users(id),
    notes         TEXT
);

CREATE INDEX idx_payments_client  ON payments(client_id);
CREATE INDEX idx_payments_invoice ON payments(invoice_id);

CREATE TABLE adjustments (
    id            BIGSERIAL PRIMARY KEY,
    client_id     BIGINT NOT NULL REFERENCES clients(id),
    invoice_id    BIGINT REFERENCES invoices(id),
    amount_cents  BIGINT NOT NULL,  -- positivo = cargo, negativo = credito
    reason        TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id BIGINT NOT NULL REFERENCES users(id)
);

-- ---------------------------------------------------------------------
-- AUDIT LOG (append-only, blindado en 002)
-- ---------------------------------------------------------------------

CREATE TABLE audit_log (
    id            BIGSERIAL PRIMARY KEY,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_user_id BIGINT REFERENCES users(id),
    actor_ip      INET,
    entity_type   TEXT NOT NULL,
    entity_id     BIGINT,
    action        TEXT NOT NULL,   -- create, update, delete, login, logout, etc.
    old_values    JSONB,
    new_values    JSONB,
    request_id    TEXT
);

CREATE INDEX idx_audit_entity   ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_actor    ON audit_log(actor_user_id);
CREATE INDEX idx_audit_occurred ON audit_log(occurred_at DESC);

-- ---------------------------------------------------------------------
-- API KEYS de admin del CAF (clientes maquina)
-- ---------------------------------------------------------------------

CREATE TABLE api_keys (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,  -- sha256 del bearer token
    scope         TEXT NOT NULL CHECK (scope IN ('admin','readonly')),
    created_by_user_id BIGINT REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ,
    last_used_at  TIMESTAMPTZ
);
