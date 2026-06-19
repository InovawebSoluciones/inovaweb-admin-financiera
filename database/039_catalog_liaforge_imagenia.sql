-- 039: Catálogo LiaForge 2026-06-19
-- (1) Nuevo servicio imagen_ia — $12 MXN por imagen generada en módulo Brief.
-- (2) Precio validacion_pagina baja de $0.50 a $0.30.
-- NOTA: estos datos ya fueron aplicados directamente en BD por LiaForge.
--       Esta migración existe solo para trazabilidad y reproducibilidad.

INSERT INTO services (code, name, unit_price_cents, source_core, unit, app_slug, organization_id)
VALUES ('imagen_ia', 'Imagen IA para campaña', 1200, 'internal', 'imagen', 'liaforge', 1)
ON CONFLICT (code) DO UPDATE
  SET name             = EXCLUDED.name,
      unit_price_cents = EXCLUDED.unit_price_cents,
      source_core      = EXCLUDED.source_core,
      unit             = EXCLUDED.unit,
      app_slug         = EXCLUDED.app_slug;

UPDATE services
SET unit_price_cents = 30
WHERE code = 'validacion_pagina'
  AND unit_price_cents <> 30;
