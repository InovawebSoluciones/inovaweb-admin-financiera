-- 038: módulo de referidos de distribuidores
-- Amplía distributors con referral_code + commission_pct,
-- añade referral_distributor_id en clients,
-- y crea la tabla append-only distributor_commissions.

ALTER TABLE distributors
  ADD COLUMN referral_code TEXT,
  ADD COLUMN commission_pct NUMERIC(5,2) NOT NULL DEFAULT 0
    CHECK (commission_pct >= 0 AND commission_pct <= 100);

-- unicidad case-insensitive del código
CREATE UNIQUE INDEX uq_distributors_referral_code
  ON distributors (UPPER(referral_code))
  WHERE referral_code IS NOT NULL;

-- cada cliente recuerda quién lo refirió
ALTER TABLE clients
  ADD COLUMN referral_distributor_id BIGINT
    REFERENCES distributors(id);

-- comisiones: append-only
CREATE TABLE distributor_commissions (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  distributor_id   BIGINT NOT NULL REFERENCES distributors(id),
  client_id        BIGINT NOT NULL REFERENCES clients(id),
  payment_hub_txn  TEXT   NOT NULL,
  base_cents       BIGINT NOT NULL CHECK (base_cents > 0),
  commission_pct   NUMERIC(5,2) NOT NULL,
  commission_cents BIGINT NOT NULL CHECK (commission_cents >= 0),
  status           TEXT   NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'paid', 'cancelled')),
  paid_at          TIMESTAMPTZ,
  paid_by_user_id  BIGINT,
  notes            TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  organization_id  BIGINT NOT NULL DEFAULT 1
);

-- idempotencia: un pago genera a lo sumo una comisión por distribuidor
CREATE UNIQUE INDEX uq_distributor_commissions_txn
  ON distributor_commissions (distributor_id, payment_hub_txn);

CREATE INDEX idx_distributor_commissions_dist
  ON distributor_commissions (distributor_id);
CREATE INDEX idx_distributor_commissions_status
  ON distributor_commissions (status);
CREATE INDEX idx_distributor_commissions_org
  ON distributor_commissions (organization_id);
