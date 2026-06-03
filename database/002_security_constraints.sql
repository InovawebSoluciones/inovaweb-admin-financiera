-- =====================================================================
-- inovaweb-admin-financiera  -  security constraints
-- 1) append-only en tablas financieras y auditoria
-- 2) auditoria automatica de cambios en entidades sensibles
-- =====================================================================

-- ---------------------------------------------------------------------
-- APPEND-ONLY
-- audit_log y payments: NO UPDATE, NO DELETE jamas.
-- invoices y adjustments: NO DELETE. UPDATE solo a campos no-financieros
--   acotados (status, paths, stamp data) via lista blanca.
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_block_update_delete()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'append-only: % on % no permitido', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

-- audit_log: bloqueo total
CREATE TRIGGER audit_log_no_update
  BEFORE UPDATE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION trg_block_update_delete();
CREATE TRIGGER audit_log_no_delete
  BEFORE DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION trg_block_update_delete();

-- payments: bloqueo total (correcciones = adjustments)
CREATE TRIGGER payments_no_update
  BEFORE UPDATE ON payments
  FOR EACH ROW EXECUTE FUNCTION trg_block_update_delete();
CREATE TRIGGER payments_no_delete
  BEFORE DELETE ON payments
  FOR EACH ROW EXECUTE FUNCTION trg_block_update_delete();

-- adjustments: bloqueo total (correcciones = nuevos adjustments con motivo)
CREATE TRIGGER adjustments_no_update
  BEFORE UPDATE ON adjustments
  FOR EACH ROW EXECUTE FUNCTION trg_block_update_delete();
CREATE TRIGGER adjustments_no_delete
  BEFORE DELETE ON adjustments
  FOR EACH ROW EXECUTE FUNCTION trg_block_update_delete();

-- invoices: NO DELETE jamas. UPDATE solo campos operativos (no monetarios).
CREATE OR REPLACE FUNCTION trg_invoices_lock_financials()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.client_id        IS DISTINCT FROM OLD.client_id        OR
     NEW.subscription_id  IS DISTINCT FROM OLD.subscription_id  OR
     NEW.period_start     IS DISTINCT FROM OLD.period_start     OR
     NEW.period_end       IS DISTINCT FROM OLD.period_end       OR
     NEW.subtotal_cents   IS DISTINCT FROM OLD.subtotal_cents   OR
     NEW.discount_cents   IS DISTINCT FROM OLD.discount_cents   OR
     NEW.tax_cents        IS DISTINCT FROM OLD.tax_cents        OR
     NEW.total_cents      IS DISTINCT FROM OLD.total_cents      OR
     NEW.supersedes_id    IS DISTINCT FROM OLD.supersedes_id    OR
     NEW.created_at       IS DISTINCT FROM OLD.created_at       OR
     NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id
  THEN
    RAISE EXCEPTION 'invoice %: campos financieros inmutables; emite nueva factura supersedes_id', OLD.id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER invoices_lock_financials
  BEFORE UPDATE ON invoices
  FOR EACH ROW EXECUTE FUNCTION trg_invoices_lock_financials();

CREATE TRIGGER invoices_no_delete
  BEFORE DELETE ON invoices
  FOR EACH ROW EXECUTE FUNCTION trg_block_update_delete();

CREATE TRIGGER invoice_items_no_delete
  BEFORE DELETE ON invoice_items
  FOR EACH ROW EXECUTE FUNCTION trg_block_update_delete();

CREATE TRIGGER invoice_items_no_update
  BEFORE UPDATE ON invoice_items
  FOR EACH ROW EXECUTE FUNCTION trg_block_update_delete();

-- ---------------------------------------------------------------------
-- AUDITORIA AUTOMATICA
-- El actor (user_id, ip, request_id) viene de variables de sesion
-- seteadas por el middleware antes de cada transaccion:
--   SET LOCAL app.actor_user_id = '...';
--   SET LOCAL app.actor_ip      = '...';
--   SET LOCAL app.request_id    = '...';
-- ---------------------------------------------------------------------

CREATE OR REPLACE FUNCTION trg_audit_row()
RETURNS TRIGGER AS $$
DECLARE
  v_actor BIGINT;
  v_ip    TEXT;
  v_req   TEXT;
  v_old   JSONB;
  v_new   JSONB;
  v_eid   BIGINT;
BEGIN
  BEGIN v_actor := current_setting('app.actor_user_id', true)::BIGINT;
  EXCEPTION WHEN OTHERS THEN v_actor := NULL; END;

  BEGIN v_ip := current_setting('app.actor_ip', true);
  EXCEPTION WHEN OTHERS THEN v_ip := NULL; END;

  BEGIN v_req := current_setting('app.request_id', true);
  EXCEPTION WHEN OTHERS THEN v_req := NULL; END;

  IF TG_OP = 'DELETE' THEN
    v_old := to_jsonb(OLD); v_new := NULL; v_eid := (v_old->>'id')::BIGINT;
  ELSIF TG_OP = 'UPDATE' THEN
    v_old := to_jsonb(OLD); v_new := to_jsonb(NEW); v_eid := (v_new->>'id')::BIGINT;
  ELSE
    v_old := NULL; v_new := to_jsonb(NEW); v_eid := (v_new->>'id')::BIGINT;
  END IF;

  INSERT INTO audit_log
    (actor_user_id, actor_ip, entity_type, entity_id, action, old_values, new_values, request_id)
  VALUES
    (v_actor,
     CASE WHEN v_ip IS NOT NULL AND v_ip <> '' THEN v_ip::INET ELSE NULL END,
     TG_TABLE_NAME, v_eid, lower(TG_OP), v_old, v_new, v_req);

  IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$$ LANGUAGE plpgsql;

-- entidades auditadas
CREATE TRIGGER audit_clients
  AFTER INSERT OR UPDATE OR DELETE ON clients
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_users
  AFTER INSERT OR UPDATE OR DELETE ON users
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_subscriptions
  AFTER INSERT OR UPDATE OR DELETE ON subscriptions
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_invoices
  AFTER INSERT OR UPDATE ON invoices
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_payments
  AFTER INSERT ON payments
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_adjustments
  AFTER INSERT ON adjustments
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_plans
  AFTER INSERT OR UPDATE OR DELETE ON plans
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_promotions
  AFTER INSERT OR UPDATE OR DELETE ON promotions
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_api_keys
  AFTER INSERT OR UPDATE OR DELETE ON api_keys
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();
