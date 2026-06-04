-- =====================================================================
-- inovaweb-admin-financiera  -  004: idempotencia a nivel BD de payments
--
-- FIX-1 (QA/Seguridad TASK-15b): la garantia de no-duplicado del pago debe
-- vivir en la BD, no en un SELECT previo (race condition entre dos webhooks
-- concurrentes con el mismo hub_transaction_id).
--
-- Indice UNICO PARCIAL sobre payments.hub_payment_id cuando NO es NULL:
--   * Es un INDICE, no una restriccion que mute filas -> NO viola el
--     append-only blindado en 002_security_constraints.sql.
--   * Permite multiples filas con hub_payment_id NULL (pagos manuales,
--     ajustes internos que no provienen del Hub).
--   * Habilita el patron INSERT ... ON CONFLICT (hub_payment_id) DO NOTHING
--     en services.prepago.process_paid_event para reclamar el pago de forma
--     atomica: el primer webhook gana, el replay no inserta fila duplicada.
--
-- Idempotente: IF NOT EXISTS -> correrlo N veces no rompe.
-- =====================================================================

CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_hub
    ON payments(hub_payment_id)
    WHERE hub_payment_id IS NOT NULL;
