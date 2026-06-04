-- =====================================================================
-- inovaweb-admin-financiera  -  seed: planes del piloto Scraping
-- Modelo PREPAGO.
--
-- NOTAS (importante para quien lea/ajuste esto):
--   * Cifras de REFERENCIA: se ajustaran despues con los numeros finales.
--   * "Free / Entrada" TIENE precio (no es gratis); is_free=false a proposito.
--     El nombre es comercial, no implica costo cero.
--   * Planes especiales (a la medida) = contacto directo. NO se siembran aqui
--     porque no tienen precio publico.
--   * Modelo prepago: el precio del plan = credito que se acredita a la wallet
--     del Medidor al contratar. Sin excedente: al agotarse el saldo se recontrata.
--   * monthly_fee_cents = ese credito prepago (centavos MXN, BIGINT, sin floats).
--   * plan_items se deja VACIO por ahora: el desglose tokens/creditos/mensajes
--     lo definira el usuario despues. Sin cuotas fijas por servicio todavia.
--
-- Idempotente: correrlo N veces no duplica ni rompe (ON CONFLICT (code)).
-- =====================================================================

INSERT INTO plans (code, name, description, monthly_fee_cents, is_free, is_active) VALUES
  ('free',    'Free / Entrada', 'Credito prepago de $100 MXN (cifra de referencia).', 10000, false, true),
  ('basico',  'Basico',         'Credito prepago de $99 MXN (cifra de referencia).',   9900, false, true),
  ('medio',   'Medio',          'Credito prepago de $200 MXN (cifra de referencia).', 20000, false, true),
  ('premium', 'Premium',        'Credito prepago de $400 MXN (cifra de referencia).', 40000, false, true)
ON CONFLICT (code) DO UPDATE SET
  name              = EXCLUDED.name,
  description       = EXCLUDED.description,
  monthly_fee_cents = EXCLUDED.monthly_fee_cents,
  is_free           = EXCLUDED.is_free,
  is_active         = EXCLUDED.is_active,
  updated_at        = now();
