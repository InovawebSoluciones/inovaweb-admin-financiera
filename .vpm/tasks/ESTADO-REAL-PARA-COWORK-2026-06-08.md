# Arranque para Cowork — estado REAL al 2026-06-08

> Lee esto + `CLAUDE.md §12` (fuente de verdad) ANTES de planear nada. Una §12
> previa ("v3 traslada formal") estaba **DESFASADA** (decia pendientes cosas ya
> hechas y que se corrio el traslada — falso). Ya corregida.

## ✅ YA HECHO Y EN PROD (NO re-ejecutar, NO marcar como pendiente)
- **Flujo de pago E2E funcionando** (verificado): recarga -> Hub (pasarela) -> webhook
  -> CAF acredita Medidor + asienta Finanzas. Saldo real $500, asiento Finanzas hub/credit.
- **Cores cableados:** 4 claves acuñadas en el `.env` del VPS del CAF (Medidor/Hub/Centro/
  Finanzas). Onboarding real (cliente Inovaweb id=5, wallet ligada a company Scraping).
- **D1 y D2 resueltos** y desplegados. Tarificacion viva (price_catalog + pricing + billing).
- **Scraping corregido (2026-06-08):** alembic reparado (`0001_base` creado -> `stamp head`
  = 0005), `medidor_client.py` en el VPS, endpoints link-caf/usage/email-usage vivos,
  esquema BIGINT. Backend healthy. Commits: scraping `761484b`, CAF/Centro pusheados.

## ⚠️ METODOS DE DEPLOY (no asumir git en todo)
- **CAF:** VPS es git -> `git pull` + rebuild. OK.
- **Scraping:** VPS **NO es git** y **no tiene acceso GitHub** (deploy key es de *microfichas*).
  Deploy = **scp desde el repo local** (no `git pull`). `app/` bind-mounted; `alembic` NO.
- **Hub:** editado/commiteado SOLO en el VPS; falta push a GitHub.

## ⏳ PENDIENTES (lo unico que queda)
1. **P1 — Endpoint `POST /v1/messages/record` en Centro + nodo n8n** (registrar envios
   email/whatsapp/sms para contarlos/facturarlos; tenant via X-API-Key, no en body;
   idempotente por source_ref). Diseño acordado con el usuario ("Opcion B"). DIFERIDO.
2. **P2 — Smoke real del consumo:** IA real (Scraping) + mensajes -> CAF tarifica ->
   debito en Finanzas -> saldo baja -> cuadre. (Requiere P3.)
3. **P3 — Proveedor de email en el Centro** (`tenant_channel_credentials` vacio): pedir al
   usuario credencial **Resend** o **SMTP M365**. Sin esto no se entregan correos reales.
4. **P4 — Push del Hub a GitHub** (hoy solo en el VPS).
5. **P5 — Deploy key de `scraping-inovaweb` en el VPS** (accion del usuario en GitHub) para
   habilitar `git pull` de Scraping. Mientras tanto: scp.
6. **P6 — Docs formales atrasados:** correr `traslada`/`inovaweb-documentacion` para poner
   al dia README/ADR/RUNBOOK/DEPLOY/CHANGELOG/OWASP. Hoy NO confiar en ellos.

## Reglas
- Fuente de verdad = `CLAUDE.md §12` + memoria del proyecto. NO los docs formales (atrasados).
- Claves/credenciales reales viven en `.env` del VPS, **nunca** en git/specs.
- No re-acuñar claves ni re-desplegar el flujo de pago: ya funciona.
