-- 035: enlace org -> cliente de la organización plataforma.
-- Agrega `organizations.platform_client_id`, una FK a `clients(id)` que liga
-- cada organización con su fila "cliente" dentro de la organización plataforma
-- (org 1). Esa fila es el vehículo del meta-cobro del SaaS: la plataforma
-- factura a cada org como si fuera un cliente más dentro de la org 1, de modo
-- que el consumo agregado de la org se traduce en cargos sobre ese cliente.
-- Nullable a propósito: orgs aún sin cliente plataforma asignado quedan en NULL.
-- Additivo, idempotente y transaccional: solo ADD COLUMN IF NOT EXISTS + índice.

BEGIN;

ALTER TABLE organizations
  ADD COLUMN IF NOT EXISTS platform_client_id BIGINT REFERENCES clients(id);

CREATE INDEX IF NOT EXISTS idx_org_platform_client
  ON organizations (platform_client_id);

COMMIT;
