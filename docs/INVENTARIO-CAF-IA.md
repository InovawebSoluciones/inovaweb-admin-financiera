# INVENTARIO — Llamadas al CAF y a proveedores de IA
**Fecha:** 2026-07-30 · **Servidor de producción:** 94.72.120.251 · Verificado en código vivo.

---

## PARTE 1 — Quién llama al CAF (cobros y candados de saldo)

**Base URL:** `CAF_BASE_URL` (default `http://host.docker.internal:8006`) · **Auth:** Bearer legacy.

### Endpoints del CAF que consume LiaForge
| Endpoint | Para qué | Archivo |
|---|---|---|
| `GET /api/v2/clients/{id}/prepaid-balance` | Saldo del monedero | `services/plan_limits.py:120` |
| `GET /api/v2/clients/{id}/plan-limits` | Tope del plan | `services/plan_limits.py:75` |
| `GET /api/v2/services` | Catálogo de precios | `services/plan_limits.py:102` |
| `POST /api/v2/clients/{id}/charge` | **Cobro real** (debita) | `services/billing.py:85` |
| `POST /api/v2/apps/onboard` | Alta de cliente al registrarse | `routers/auth.py:255` |
| `GET /api/v2/plans` | Planes disponibles | `routers/auth.py:463` |

### Los 15 servicios que LiaForge cobra al CAF (`service_code`)
| service_code | Dónde se cobra | Archivo |
|---|---|---|
| `email` | Cada correo enviado | `services/email_sender.py:279` |
| `descubrimiento` | Descubrir empresas con IA | `services/semantic_search.py:275` |
| `descubrimiento_local` | Descubrir por zona | `routers/semantic_search.py:199` |
| `descubrimiento_archivo` | Descubrir desde archivo (gate) | `routers/semantic_search.py:432` |
| `validacion_email` | Verificar buzón | `services/semantic_search.py:579`, `routers/universidades.py:360` |
| `validacion_dns` | Verificar dominio | `workers/tasks.py:445` |
| `validacion_pagina` | Verificar sitio web | `workers/tasks.py:339` |
| `scraping` | Rastreo web (gate) | `workers/jobs.py:96`, `workers/tasks.py:334` |
| `geocoding` | Ubicar en mapa | `services/semantic_search.py:742`, `workers/tasks.py:727` |
| `norm_excel` | Normalizar Excel con IA | `routers/jobs.py:565` |
| `imagen_ia` | Generar imagen | `routers/briefs.py:351` |
| `analisis_ia` | Análisis de campaña | `routers/campanas.py:397` |
| `agente_corrida` | Corrida del Agente | `routers/agente.py:272` |
| `dato_email_proveedor` | Email de proveedor externo | `routers/data_providers.py:184` |
| (WhatsApp) | Mensaje WA | `workers/tasks_whatsapp.py:311` |

**Mecanismo:** `assert_within_limit()` (candado preventivo: 402 si no alcanza) → operación → `cobrar()` (debita con clave de idempotencia).

### Salientes del CAF hacia sus cores
| Cliente | Endpoints | Archivo |
|---|---|---|
| Medidor | `/v1/usage`, `/v1/wallets`, `/v1/wallets/{id}/balance`, `/credit`, `/admin/v1/wallets/{id}/suspend` | `core/clients/medidor_client.py` |
| Hub | `/hub/v1/charge`, `/admin/hub/v1/gateway-config`, `/gateway-default` | `core/clients/hub_client.py` |
| Finanzas | `/v1/ledger/balance`, `/entries`, `/totals` | `core/clients/finanzas_client.py` |
| Mensajes | `/v1/messages/email`, `/whatsapp`, `/v1/reports/usage` | `core/clients/messages_client.py` |
| PAC (Facturama) | timbrado CFDI | `core/clients/pac_client.py` |
| Scraping | `POST /companies/{id}/link-caf` | `core/clients/scraping_client.py` |

### Entrante al CAF
- **Hub → CAF**: webhook `payment.paid` (`CAF_WEBHOOK_URL`, firmado HMAC) — `hub/app/routers/webhooks_router.py`.

---

## PARTE 2 — Llamadas a proveedores de IA

### 2A. DeepSeek — VÍA el proxy del Medidor (corregido 2026-07-30)
**Ruta:** LiaForge → `{MEDIDOR_BASE_URL}/llm/deepseek/v1/chat/completions` → `api.deepseek.com`
**Helper único:** `services/llm_client.py` → `deepseek_client()`. Fail-open: si no hay Medidor configurado, cae a directo (pierde medición, no rompe).

| # | Archivo | Para qué |
|---|---|---|
| 1 | `services/email_writer.py` (campaña) | Redacción de correos de campaña |
| 2 | `services/email_writer.py` (clasif. mercado) | Clasificar público del artículo |
| 3 | `services/email_writer.py` (artículo) | Redacción de correos de artículo |
| 4 | `services/lia_normalizer.py` | Mapear columnas de Excel |
| 5 | `services/brand_dna.py` | Brief desde web/documento |
| 6 | `services/whatsapp_writer.py` | Redacción de WhatsApp (timeout 30s) |
| 7 | `services/whatsapp_inbound.py` | Clasificar respuestas de WA |
| 8 | `services/analista_campana.py` | Análisis estratégico de campaña |
| 9 | `routers/lia_chat.py` | Chatbot Lia |
| 10 | `agente/planner.py` | Planner del Agente |

✅ **Las 10 ya registran tokens y costo real en `events`.** Antes iban directo a `api.deepseek.com` y su consumo era invisible (0 eventos en 30 días).

**Medición verificada (2026-07-30):** un correo de campaña = 3,174 tokens entrada ($0.10) + 3,139 salida ($0.19) = **$0.29 MXN de costo real**, contra $1.00 que se cobra → margen ~3.4x.

### 2B. Perplexity — VÍA el proxy del Medidor (sí se mide)
**Ruta:** LiaForge → `{medidor_base_url}/llm/perplexity/v1/chat/completions` → `https://api.perplexity.ai`.
- **Modelo:** `sonar-pro` (`llm_model_discovery`) · **Archivo:** `services/llm_provider.py:95`
- **Usos:** descubrir empresas (`search_accounts`) y enriquecer contactos (`enrich_contact`)
- **Costo interno registrado** en `search_sessions.cost_usd` — promedio real **$0.0075 USD/cuenta**
- **Tarifa upstream:** entrada $3/M tokens, salida $15/M, búsqueda $5/1000

### 2C. Proxies que el Medidor expone
| Ruta | Upstream | Modelo |
|---|---|---|
| `/llm/perplexity/v1/chat/completions` | `api.perplexity.ai` | sonar-pro |
| `/llm/deepseek/v1/chat/completions` | `api.deepseek.com` | (cliente elige) |
| `/v1/images/generations` | `api.openai.com` | `gpt-image-1` |

---

## PARTE 3 — Precios por consumo (ya existía, ahora en uso)

`llm_pricing` (BD `medidor_ia`) tiene precios por millón de tokens en MXN, separando entrada/salida:

| Proveedor / modelo | Entrada | Salida |
|---|---|---|
| deepseek-v4-pro | $30.45/M | $60.90/M |
| deepseek-v4-flash | $2.45/M | $4.90/M |
| deepseek-chat / reasoner | $2.45/M | $4.90/M |
| perplexity sonar-pro | $51/M | $255/M |
| openai gpt-image-1 | $0.25/imagen | — |

`events` guarda por llamada: `provider_units` (tokens), `llm_pricing_id`, `cost_cents`. El **Medidor solo mide**; el cobro al cliente sigue siendo del CAF (precio plano por `service_code`).

---

## PARTE 4 — Hallazgos abiertos

### 🔴 Servicios con IA que NO cobran nada (pendiente definir precio)
| Función | Archivo | Cobro actual |
|---|---|---|
| Chatbot Lia | `routers/lia_chat.py` | **0** |
| Brief desde web | `routers/briefs.py` → `analizar-sitio` | **0** |
| Brief desde documento | `routers/briefs.py` → `analizar-documento` | **0** |
| Clasificar respuesta WhatsApp | `routers/whatsapp_inbox.py` | **0** |
| Clasificar mercado de artículo | `workers/tasks.py:904` | **0** (absorbido en el envío) |

### ✅ COBRO POR CONSUMO implementado 2026-07-30 (`analisis_ia`, `agente_corrida`)
Antes: precio PLANO de 10 cr sin importar el tamaño. Ahora: **1 unidad = 1,000 tokens** (entrada+salida), redondeo hacia arriba, mínimo 1.

**⚠️ LA RELACIÓN TOKENS→CRÉDITO YA ESTABA ESTABLECIDA — no inventar precios.** Vive en `price_catalog` (BD del CAF):

| meter | unit_code | public_price_micros | cost_price_micros |
|---|---|---|---|
| `ia` | `token` | **60** | 30 |

60 micros de peso por token ⇒ **1 crédito = 166.67 tokens** ⇒ **6 créditos por cada 1,000 tokens** (margen 2x sobre el costo). Por eso `services.unit_price_cents = 6` para ambos servicios.

| service_code | unit | precio | Fuente de los tokens |
|---|---|---|---|
| `analisis_ia` | `1000_tokens` | 6 cr | `resp.usage` → `analizar_campana()` devuelve `tokens` → `routers/campanas.py` hace `ceil(tokens/1000)` |
| `agente_corrida` | `1000_tokens` | 6 cr | `DeepSeekPlanner.ultimo_uso_tokens` → `routers/agente.py` hace `ceil(tokens/1000)`. Las herramientas del plan siguen cobrando su propio servicio. |

**Verificado:** un análisis real consumió **3,395 tokens** → 4 unidades → **24 cr** ($0.24). Antes: 10 cr fijos.

**Arquitectura de precios (documentada en `caf/services/billing.py:233`):** *"el Medidor mide la CANTIDAD (tokens); el CAF aplica el PRECIO PÚBLICO (`price_catalog`). El costo crudo del Medidor NO se factura — es solo para margen/COGS."* Es decir: `llm_pricing`/`service_catalog` del Medidor = **COSTO** (COGS, ~$30.45/M entrada en v4-pro); `price_catalog` del CAF = **PRECIO DE VENTA**.

### ✅ Corregido 2026-07-30
1. **`MEDIDOR_BASE_URL`** apuntaba a `http://medidor-api:8000` (contenedor inexistente — el real es `medidor_api` — y en otra red Docker) → `https://medidor.inovaweb.com.mx`.
2. **`medidor_api` no estaba en la red de su base de datos.** Su BD (`medidor_ia`) vive DENTRO de `scraping-postgres`, en la red `scraping-universidades_default`, pero el contenedor solo estaba en `medidor_ia_default` → **no podía resolver su propio Postgres** (`gaierror`), así que TODA ruta con auth devolvía 500. Fix: `docker network connect scraping-universidades_default medidor_api`. **Esta era la causa de los fallos de enriquecimiento de todo el día.**
   **PERSISTIDO** en `/opt/medidor_ia/docker-compose.yml` (red externa `scraping_net` → `scraping-universidades_default`); verificado que sobrevive `docker compose up`. Respaldo: `docker-compose.yml.bak-net-*`.
3. Las 10 llamadas a DeepSeek ruteadas por el proxy del Medidor (ver Parte 2A).

### ⚠️ Nota
El proxy del Medidor tiene tope de **60 llamadas/min por tenant** (`PROXY_TENANT_RATE_PER_MIN`, puesto en la auditoría FEA del 2026-07-27). Con lotes grandes de correos podría topar; ajustar si hace falta.
