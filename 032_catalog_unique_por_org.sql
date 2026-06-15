-- 032: catálogo multi-tenant — UNIQUE de `code` por organización (no global).
-- Permite que cada org use sus propios `code` (p.ej. dos orgs pueden tener
-- 'email') sin colisión. Seguro: ninguna FK referencia por `code` (todas por id);
-- el código del CAF ya resuelve servicios/planes por (code, organization_id).
-- Los codes actuales viven todos en la org 1 -> siguen siendo únicos dentro de ella.

BEGIN;

ALTER TABLE services
  DROP CONSTRAINT IF EXISTS services_code_key,
  ADD  CONSTRAINT services_org_code_key   UNIQUE (organization_id, code);

ALTER TABLE plans
  DROP CONSTRAINT IF EXISTS plans_code_key,
  ADD  CONSTRAINT plans_org_code_key      UNIQUE (organization_id, code);

ALTER TABLE products
  DROP CONSTRAINT IF EXISTS products_code_key,
  ADD  CONSTRAINT products_org_code_key   UNIQUE (organization_id, code);

ALTER TABLE promotions
  DROP CONSTRAINT IF EXISTS promotions_code_key,
  ADD  CONSTRAINT promotions_org_code_key UNIQUE (organization_id, code);

COMMIT;
