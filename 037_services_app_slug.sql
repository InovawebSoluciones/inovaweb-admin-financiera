-- 037: app_slug en services — identifica a qué app pertenece cada servicio cobrable.
-- Additivo, idempotente, transaccional. Sin ON CONFLICT (ALTER TABLE).

BEGIN;

ALTER TABLE services
  ADD COLUMN IF NOT EXISTS app_slug TEXT;

-- Actualizar filas existentes según pertenencia conocida.
UPDATE services SET app_slug = 'liaforge'
  WHERE code IN ('agente_corrida','descubrimiento','descubrimiento_local',
                 'email','geocoding','scraping',
                 'validacion_dns','validacion_email','validacion_pagina');

UPDATE services SET app_slug = 'swigg'
  WHERE code IN ('envio_video','guion_ia','video_producido','vista_video');

UPDATE services SET app_slug = 'caf'
  WHERE code IN ('saas_transaccion');

COMMIT;
