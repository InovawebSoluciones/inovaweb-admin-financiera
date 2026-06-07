# CIERRE DE SESIÓN — 2026-06-07

**Repo CAF:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\`
**Repo Scraping:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\`

NO hagas commit, NO hagas push, NO levantes Docker.

---

## PASO 1 — Revisión de coherencia final

Lee los siguientes archivos y verifica que no haya contradicciones ni referencias rotas entre ellos:

- `app/services/onboarding.py` — pasos 2b (link-caf) y 5b (token + email)
- `app/core/clients/scraping_client.py` — método `link_caf`
- `app/core/clients/messages_client.py` — métodos `send_email` y `get_usage`
- `app/core/clients/medidor_client.py` — métodos `credit`, `suspend_wallet`, `get_usage_summary`
- `app/services/billing.py` — lectura de consumo IA + mensajes en cierre mensual
- `app/core/config.py` — variables `SCRAPING_BASE_URL`, `SCRAPING_ADMIN_KEY`, `HUB_WEBHOOK_SECRET`, `MAX_RECARGA_CENTS`

Verifica:
1. Que `onboarding.py` llama a `scraping_client.link_caf` con los parámetros correctos (tipos coinciden con `LinkCafIn` del router de Scraping).
2. Que `onboarding.py` envía exactamente `{"token_url": ..., "nombre": ..., "expiracion_horas": ...}` al Centro de Mensajes.
3. Que `billing.py` usa `medidor_client.get_usage_summary` y `messages_client.get_usage` con las firmas correctas.
4. Que `config.py` tiene todas las variables nuevas con los tipos correctos (`SecretStr` donde aplica).

Reporta cualquier inconsistencia encontrada y corrígela.

---

## PASO 2 — py_compile global

Corre `py_compile` sobre todo el directorio `app/` del CAF y sobre los archivos de Scraping modificados:

```
python -m py_compile <ruta_archivo>
```

Archivos CAF:
- Todos los `.py` bajo `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\`

Archivos Scraping:
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\app\models\company.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\app\routers\companies.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\alembic\versions\0005_caf_client_id_bigint.py`

Reporta el resultado. Si algún archivo falla, corrígelo.

---

## PASO 3 — Actualizar CLAUDE.md §12

Actualiza la sección 12 del archivo:
`C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\CLAUDE.md`

Reemplaza el contenido de la sección 12 con el estado real de esta sesión. El nuevo contenido debe ser:

```
## 12. ESTADO ACTUAL Y PENDIENTES (actualizar al cerrar cada sesion)

**Sesion al: 2026-06-07. Foco: GRUPO 3 completo + pre-produccion.**

Modelo del piloto: cliente elige plan -> paga (Conekta sandbox) -> CAF acredita
saldo en wallet del Medidor -> consumo IA descuenta -> al agotarse, bloqueo.
El Medidor core YA implementa prepago (authorize/finish/credit/balance). Identidad:
CAF clients.id <-> Scraping Company.caf_client_id <-> Company.id <-> wallet
external_user_id (tenant 'inovaweb').

### Completado esta sesion (Grupo 3)

| Tarea | Estado | Archivos clave |
|---|---|---|
| C1 Fix medidor_client rutas | ✅ pytest 3/3 VPS | medidor_client.py |
| A — Onboarding wallet + Scraping + activación | ✅ py_compile OK | onboarding.py, scraping_client.py, 005_activation_tokens.sql |
| B — Hardening H1-H5 | ✅ py_compile OK | onboarding.py, config.py, prepago.py, 006_idempotencia.sql |
| C — Frontend Jinja2 + HTMX | ✅ py_compile OK | templates/, admin_router.py, portal_router.py |
| D — Billing consumo IA + emails | ✅ py_compile OK | billing.py, messages_client.py |
| Pre-prod prep | ✅ | commits-listos.md, deploy-vps.sh, seed-mensajes.md |

### Pendientes para el usuario (VPS)

| # | Pendiente | Estado |
|---|---|---|
| 18 | Key ADMIN del Medidor → MEDIDOR_API_KEY en .env VPS | pendiente usuario |
| 1 | DNS/TLS admin.inovaweb.com.mx + app.inovaweb.com.mx | pendiente usuario |
| DEPLOY | git commit+push ambos repos → deploy VPS → migraciones 005+006 | pendiente usuario |
| SEED | Sembrar plantilla caf-activacion-correo en Centro de Mensajes | pendiente usuario |
| QA | pytest en Docker/VPS (solo py_compile local verificado) | pendiente usuario |

### Pendientes técnicos menores

| # | Detalle |
|---|---|
| D3 | Confirmar que Centro de Mensajes registra mensajes con el mismo external_user_id |
| D4 | Verificar ruta real de Scraping en VPS (/root/ vs /opt/) y slug tenant |
| F | Facturación CFDI 4.0 vía Ecofile — tarea final, requiere contrato de API |

GIT: VPS usa SSH (llave ed25519 en GitHub). Windows usa HTTPS con credential cache.
pytest diferido a Docker/VPS (venv Linux no usable en Windows / OneDrive).
```

---

## PASO 4 — Generar comando de commit final

Escribe al final del archivo:
`C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\.vpm\tasks\commits-listos.md`

Una sección nueva titulada `## Commit adicional — CLAUDE.md actualizado` con el comando:

```bash
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera"
git add CLAUDE.md
git commit -m "docs(claude): actualizar estado sesion 2026-06-07 — grupo3 completo + pre-prod"
git push origin main
```

---

## Reporte final

Al terminar reporta:
- Resultado del PASO 1 (coherencia — inconsistencias encontradas y corregidas)
- Resultado del PASO 2 (py_compile — OK o errores)
- Confirmación de que CLAUDE.md §12 fue actualizado
- Confirmación de que commits-listos.md tiene la sección nueva
