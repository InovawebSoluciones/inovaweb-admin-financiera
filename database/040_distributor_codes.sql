-- 040: códigos secundarios de distribuidor (uno por vendedor)
-- El distribuidor ya tiene su propio referral_code (038). Esta migración
-- permite añadirle códigos HIJOS -- uno por cada vendedor -- que resuelven
-- al MISMO distributor_id. La comisión sigue viviendo únicamente en
-- distributors.commission_pct: un código hijo no crea ni modifica ninguna
-- fila de distributor_commissions, solo cambia a qué distribuidor se
-- vincula el cliente referido (igual que si hubiera usado el código
-- principal del distribuidor).

CREATE TABLE distributor_codes (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  distributor_id  BIGINT NOT NULL REFERENCES distributors(id),
  label           TEXT   NOT NULL,   -- nombre del vendedor, solo para identificar el código
  code            TEXT   NOT NULL,
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  organization_id BIGINT NOT NULL DEFAULT 1
);

-- unicidad case-insensitive del código, GLOBAL (mismo espacio que
-- distributors.referral_code: un código no puede repetirse entre sí ni
-- colisionar con el código principal de otro distribuidor)
CREATE UNIQUE INDEX uq_distributor_codes_code
  ON distributor_codes (UPPER(code));

CREATE INDEX idx_distributor_codes_dist
  ON distributor_codes (distributor_id);
CREATE INDEX idx_distributor_codes_org
  ON distributor_codes (organization_id);
