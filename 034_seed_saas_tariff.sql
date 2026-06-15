-- 034 (seed, additivo/idempotente): tarifa del propio SaaS del CAF.
-- Vive en el catálogo de la org plataforma (organization_id = 1 = Inovaweb).
-- Modelo híbrido confirmado por el usuario: plan mensual Y/O pago por transacción,
-- anclado en 99. Ajustar montos aquí (centavos BIGINT) si cambia el precio.

BEGIN;

-- Componente "por transacción": $0.99 (99¢) por transacción facturable del motor.
INSERT INTO services (code, name, source_core, unit, unit_price_cents, is_active, organization_id)
VALUES ('saas_transaccion', 'Transacción CAF (SaaS)', 'internal', 'transaccion', 99, true, 1)
ON CONFLICT (organization_id, code)
  DO UPDATE SET unit_price_cents = EXCLUDED.unit_price_cents, name = EXCLUDED.name;

-- Componente "plan mensual": $99/mes (9900¢).
INSERT INTO plans (code, name, description, monthly_fee_cents, is_free, is_active, app_slug, organization_id)
VALUES ('caf_saas', 'CAF SaaS',
        'Motor de medición y cobro multi-tenant. Plan mensual y/o pago por transacción.',
        9900, false, true, 'caf', 1)
ON CONFLICT (organization_id, code)
  DO UPDATE SET monthly_fee_cents = EXCLUDED.monthly_fee_cents,
                name = EXCLUDED.name, description = EXCLUDED.description;

COMMIT;
