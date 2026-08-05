-- 2026-08-05_busqueda_plus.sql
-- Alta de la Busqueda Plus (Perplexity Agent API + people_search) en LiaForge.
--
-- Cobro POR TOKEN. Costo MEDIDO en una prueba de 10 empresas reales
-- (422,791 tokens = $4.54 MXN = 10.73 micros por token). Margen 3x por orden
-- de Conrado (2026-08-05).
--
-- Un token de REDACCION (DeepSeek, ~4 micros) y uno de BUSQUEDA (~11 micros)
-- no cuestan lo mismo, asi que no pueden compartir precio de venta: por eso
-- una unidad propia y no reusar 'token'.
BEGIN;

INSERT INTO price_catalog (meter, unit_code, description, public_price_micros,
                           cost_price_micros, currency, is_active, organization_id)
SELECT 'ia', 'token_busqueda',
       'Busqueda Plus - por token (Perplexity Agent, margen 3x)',
       32, 11, 'MXN', true, 1
WHERE NOT EXISTS (SELECT 1 FROM price_catalog
                  WHERE meter='ia' AND unit_code='token_busqueda');

-- unit='token_busqueda' es lo que hace que /charge tarifique por micros contra
-- price_catalog. unit_price_cents queda en 0 a proposito: NO se usa para las
-- unidades token*.
INSERT INTO services (code, name, source_core, unit, unit_price_cents,
                      is_active, organization_id)
SELECT 'busqueda_plus', 'Busqueda Plus (contactos con nombre y puesto)',
       'medidor', 'token_busqueda', 0, true, 1
WHERE NOT EXISTS (SELECT 1 FROM services
                  WHERE code='busqueda_plus' AND organization_id=1);

COMMIT;
