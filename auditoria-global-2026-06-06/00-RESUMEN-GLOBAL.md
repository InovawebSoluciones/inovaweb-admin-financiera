# Resumen General — Auditoría Global de la Plataforma Inovaweb

**Fecha:** 2026-06-06 · **Reside en:** CAF (capa Nivel 2 que administra toda la
plataforma y tiene impacto directo con Scraping) · **Alcance:** los 6 proyectos.

## Preámbulo

Se realizó una **auditoría exhaustiva** de la plataforma Inovaweb cubriendo, para
cada proyecto: **revisión de código** (correctitud, idempotencia, manejo de errores),
**base de datos** (centavos, append-only, auditoría), **seguridad OWASP** y —de forma
transversal— la **consistencia de los contratos de integración entre proyectos**.

**Transferencia de resultados:** los resultados se entregan a cada módulo dentro de
su propio repositorio, en la carpeta `auditoria-global-2026-06-06/`:
- Cada **core** y **Scraping** llevan **solo su reporte individual** (`REPORTE-<modulo>.md`).
- El **CAF** lleva **este resumen general** (`00-RESUMEN-GLOBAL.md`) **más** su reporte
  individual (`REPORTE-CAF.md`).

## 1. Arquitectura (3 niveles)

```
Nivel 3  Scraping Universidades        (app cliente; consume el Medidor, scope CLIENT)
Nivel 2  CAF (admin-financiera)        (orquesta los 4 cores; UI + auth humano)
Nivel 1  medidor_ia · hub-pasarelas · finanzas-core · centro-mensajes  (cores API-only)
```

Principios verificados en los 6: **dinero en centavos BIGINT (sin floats)** y
**append-only por triggers SQL** en tablas financieras. ✅

## 2. Tabla resumen

| Proyecto | Rev. Código | OWASP | Docs estándar | Commit |
|---|---|---|---|---|
| medidor_ia *(delta)* | OK | PASS | completas + GUIA-DEV nuevo; rutas de doc corregidas | listo |
| hub-pasarelas | OK | PASS (WARN) | ADR/RUNBOOK/DEPLOY/OWASP/CHANGELOG/GUIA-DEV nuevos | listo |
| finanzas-core *(delta)* | OK | PASS | completas + GUIA-DEV nuevo | listo |
| centro-mensajes | OK | PASS (WARN) | ADR/RUNBOOK/DEPLOY/OWASP/CHANGELOG/GUIA-DEV nuevos | listo |
| **CAF** | OK + **C1 corregido** | PASS + obs. | ARQUITECTURA-GLOBAL/OWASP/GUIA-DEV/GUIA-OPER/GUIA-CLI/RESUMEN nuevos | ⚠️ **condicionado a QA** |
| scraping | OK + 1 FAIL negocio | PASS (1 FAIL) | OWASP/GUIA-DEV/GUIA-USUARIO nuevos | listo |

## 3. Hallazgo CRÍTICO (corregido)

**C1 — Ruta de acreditación del CAF al Medidor.** El CAF acreditaba en
`/admin/v1/wallets/{id}/credit` (inexistente); el Medidor expone credit en
`/v1/wallets/{id}/credit`. Toda recarga habría dado 404. **Causa raíz:** la doc del
Medidor publicaba la ruta errónea. **Corregido** en el código del CAF (credit → `/v1`;
compensación `delete` → `suspend`) y en los docs del Medidor. **Pendiente: correr
pytest en Docker/VPS antes del commit del CAF.**

## 4. Discrepancias de integración (diseño ↔ realidad)

| ID | Resumen | Módulos | Estado |
|----|---------|---------|--------|
| C1 | credit del CAF en ruta inexistente | CAF, Medidor | ✅ corregido (QA pendiente) |
| D1 | Identidad CAF↔Scraping no cableada; `medidor_wallet_id` sin ruta que lo pueble; `link-caf` inexistente | CAF, Scraping | abierto |
| D2 | El Hub acredita al Medidor directo (no vía webhook al CAF); riesgo de doble crédito si se cablea el CAF | Hub, CAF | documentado |
| D3 | `source_slug=scraping_*` incompatible con el conjunto cerrado de Finanzas | Scraping, Finanzas | abierto (integración diferida) |
| D4 | Plantillas `caf-*` no sembradas en Mensajes (→404) | CAF, Mensajes | abierto |
| D5 | WhatsApp/SMS del Centro de Mensajes en 501; el CAF lo asume | CAF, Mensajes | abierto |

**Hallazgos de negocio (Scraping):** redondeo de micro-costos a 0¢ → sub-facturación
(`semantic_search.py:384`); el modo `enriquecer` no factura consumo de IA.

## 5. Notas operativas

- **Reverse proxy:** Nginx reemplazó a Caddy; los repos aún traen `Caddyfile`. Los
  docs de deploy quedaron con `[TODO: verificar config Nginx real en VPS]`. Verificar
  que HSTS/CSP/límites de body estén replicados en Nginx.
- **Entorno:** los `.venv` en OneDrive aparecen "solo nube"; correr tests en el host
  real / Docker.
- **Regla de la auditoría:** no se modificó código salvo el fix C1 (autorizado); los
  hallazgos de negocio del Scraping quedaron **documentados, no corregidos**.

## 6. Commits a ejecutar (uno por proyecto)

```bash
# medidor_ia
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\medidor_ia" && git add . && git commit -m "docs: auditoria global delta + GUIA-DESARROLLADOR + fix ruta credit en docs 2026-06-06" && git push origin main

# hub-pasarelas
cd "C:\Users\conra\inovaweb-hub-pasarelas" && git add . && git commit -m "docs: auditoria global + documentacion completa 2026-06-06" && git push origin main

# finanzas-core
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-finanzas-core" && git add . && git commit -m "docs: auditoria global delta + GUIA-DESARROLLADOR 2026-06-06" && git push origin main

# centro-mensajes
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-centro-mensajes" && git add . && git commit -m "docs: auditoria global + documentacion completa 2026-06-06" && git push origin main

# scraping_comercial
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial" && git add . && git commit -m "docs: auditoria global + OWASP + guias 2026-06-06" && git push origin main

# CAF  (⚠️ correr pytest en Docker/VPS ANTES — se tocó código en el fix C1)
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera" && git add . && git commit -m "fix(medidor-client): ruta credit /v1 + compensacion suspend; docs: auditoria global completa 2026-06-06" && git push origin main
```

---

*Auditoría global Inovaweb — 2026-06-06. Verificada contra el código fuente de cada repositorio.*
