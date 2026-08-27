-- 041: cierra los huecos del módulo de distribuidores
-- (1) Auditoría en BD para las 3 tablas del módulo. La convención firme del
--     proyecto (CLAUDE.md §4) exige trigger de auditoría en toda escritura;
--     distributors, distributor_commissions y distributor_codes nunca lo
--     tuvieron. Sin esto no hay forma de responder quién cambió un % de
--     comisión o desactivó un código, ni cuándo.
-- (2) updated_at en distributor_codes: las otras dos tablas del módulo ya
--     distinguen alta de última modificación; ésta no, y ahora se puede editar.

CREATE TRIGGER audit_distributors
  AFTER INSERT OR UPDATE OR DELETE ON distributors
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_distributor_commissions
  AFTER INSERT OR UPDATE OR DELETE ON distributor_commissions
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

CREATE TRIGGER audit_distributor_codes
  AFTER INSERT OR UPDATE OR DELETE ON distributor_codes
  FOR EACH ROW EXECUTE FUNCTION trg_audit_row();

ALTER TABLE distributor_codes
  ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
