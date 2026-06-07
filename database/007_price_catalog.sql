-- =====================================================================
-- inovaweb-admin-financiera  -  007: tabla de precios al publico (rating)
--
-- La capa de tarificacion vive en el CAF (capa comercial), NO en Finanzas
-- (que solo registra montos ya calculados). Convierte CANTIDAD medida por los
-- cores (tokens de IA / mensajes por canal) en PESOS al precio publico.
--
-- Precision SUB-CENTAVO: el precio unitario se guarda en MICRO-PESOS
-- (1 peso = 1,000,000 micros) porque un token de IA vale fracciones de centavo.
-- El total del periodo se agrega en micros y se redondea UNA sola vez a
-- centavos (evita la sub-facturacion por redondear cada operacion a 0 —
-- leccion del hallazgo A04 de Scraping: semantic_search.py:384).
--
-- Se guarda tambien el COSTO del proveedor (cost_price_micros) para calcular
-- margen = precio - costo (COGS por unidad).
--
-- Idempotente: IF NOT EXISTS + ON CONFLICT.
-- OJO: los precios sembrados son PLACEHOLDERS — el negocio/contador los fija.
-- =====================================================================

CREATE TABLE IF NOT EXISTS price_catalog (
    id                  BIGSERIAL PRIMARY KEY,
    -- dominio de medicion: 'ia' (Medidor, por token) | 'message' (Centro, por canal)
    meter               TEXT NOT NULL CHECK (meter IN ('ia','message')),
    -- unidad: 'token' para ia; 'email'|'whatsapp'|'sms'|'instagram'|'messenger' para message
    unit_code           TEXT NOT NULL,
    description         TEXT NOT NULL,
    -- precio al PUBLICO por unidad, en micro-pesos (1 peso = 1,000,000)
    public_price_micros BIGINT NOT NULL CHECK (public_price_micros >= 0),
    -- COSTO del proveedor por unidad (para margen). 0 si aun no se carga.
    cost_price_micros   BIGINT NOT NULL DEFAULT 0 CHECK (cost_price_micros >= 0),
    currency            TEXT NOT NULL DEFAULT 'MXN',
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    valid_from          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Un solo precio ACTIVO por (meter, unit_code). Cambios de precio = nueva fila
-- + desactivar la anterior (versionado por valid_from).
CREATE UNIQUE INDEX IF NOT EXISTS uq_price_active
    ON price_catalog (meter, unit_code) WHERE is_active;

-- ---------------------------------------------------------------------
-- SEED placeholders (valores de EJEMPLO — el negocio/contador los corrige)
-- ---------------------------------------------------------------------
INSERT INTO price_catalog (meter, unit_code, description, public_price_micros, cost_price_micros) VALUES
  ('ia',      'token',     'IA — por token (PLACEHOLDER)',            60,        30),
  ('message', 'email',     'Mensaje — email (PLACEHOLDER)',           1000000,   200000),
  ('message', 'whatsapp',  'Mensaje — WhatsApp (PLACEHOLDER)',        1500000,   800000),
  ('message', 'sms',       'Mensaje — SMS (PLACEHOLDER)',             800000,    400000),
  ('message', 'instagram', 'Mensaje — Instagram (PLACEHOLDER)',       1000000,   300000),
  ('message', 'messenger', 'Mensaje — Messenger (PLACEHOLDER)',       1000000,   300000)
ON CONFLICT (meter, unit_code) WHERE is_active DO NOTHING;
