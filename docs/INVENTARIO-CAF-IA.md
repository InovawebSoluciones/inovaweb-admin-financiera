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

### 2A. DeepSeek — DIRECTO desde LiaForge (NO pasa por el Medidor)
**Config:** `deepseek_base_url=https://api.deepseek.com` · `deepseek_model=deepseek-v4-pro` · key `DEEPSEEK_API_KEY`.

| # | Archivo | Para qué |
|---|---|---|
| 1 | `services/email_writer.py:120` | Redacción de correos de campaña |
| 2 | `services/email_writer.py:232` | Redacción de correos de artículo |
| 3 | `services/lia_normalizer.py:154` | Mapear columnas de Excel |
| 4 | `services/brand_dna.py:104` | Brief desde web/documento |
| 5 | `services/whatsapp_writer.py:187` | Redacción de WhatsApp |
| 6 | `services/whatsapp_inbound.py:248` | Clasificar respuestas de WA |
| 7 | `services/analista_campana.py:183` | Análisis estratégico de campaña |
| 8 | `routers/lia_chat.py:393` | Chatbot Lia |
| 9 | `agente/planner.py:236` | Planner del Agente |

⚠️ **Estas 9 NO se miden en el Medidor** — el gasto real de tokens de DeepSeek no queda registrado ahí.

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

## PARTE 3 — Hallazgos abiertos (2026-07-30)

1. 🔴 **DNS interno de Docker en el 94 falla intermitentemente** — rompe LiaForge→Medidor y Medidor→su BD. Sin resolver; requiere reiniciar el daemon de Docker (18 contenedores, varios proyectos). **Pendiente de autorización.**
2. ✅ **CORREGIDO** `MEDIDOR_BASE_URL` en LiaForge: apuntaba a `http://medidor-api:8000` (nombre inexistente + red Docker distinta) → ahora `https://medidor.inovaweb.com.mx`.
3. ✅ **CORREGIDO** `DATABASE_URL` del Medidor: apuntaba a `scraping-postgres` (¡BD de otro proyecto!) → ahora `medidor_db`.
4. 🟠 **9 llamadas a DeepSeek no pasan por el Medidor** → su consumo de tokens no se mide ni se audita centralmente. Sí se cobra al cliente por servicio, pero sin trazabilidad del costo real.
5. 🟠 El Medidor tiene el proxy de DeepSeek disponible pero **LiaForge no lo usa** (llama directo).
