-- =====================================================================
-- inovaweb-admin-financiera  -  006: idempotencia de onboarding (clients)
--
-- H1 (hardening #19/22, TASK-15b): onboard_client recibe un request_id pero
-- no lo usaba para deduplicar el alta. Un reintento del operador / del API
-- (timeout + retry) podia crear un segundo cliente con la misma RFC u otro
-- duplicado parcial.
--
-- Solucion (consistente con el patron de 004_payments_idempotency.sql):
--   * columna request_id TEXT en clients (nullable: las altas historicas y
--     las que no traen request_id quedan en NULL).
--   * INDICE UNICO PARCIAL sobre request_id cuando NO es NULL:
--       - Es un INDICE, no una restriccion que mute filas -> NO viola el
--         append-only / auditoria blindados en 002_security_constraints.sql.
--       - Permite multiples filas con request_id NULL.
--       - Habilita el check-before-insert + ON CONFLICT en
--         services.onboarding.onboard_client para devolver el cliente
--         existente en vez de crear otro ante un reintento.
--
-- Idempotente: ADD COLUMN IF NOT EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS
-- -> correrlo N veces no rompe.
-- =====================================================================

ALTER TABLE clients ADD COLUMN IF NOT EXISTS request_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_clients_request_id
    ON clients(request_id)
    WHERE request_id IS NOT NULL;
