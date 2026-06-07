# Reporte de Auditoría — inovaweb-admin-financiera (CAF, Nivel 2)

**Fecha:** 2026-06-06 · **Capa:** Nivel 2 (servicio orquestador, UI + auth humano) · **Rol:** Centro de Administración Financiera

> Este es el reporte **individual** del CAF (cambios aplicados a sí mismo). El
> **resumen general de toda la plataforma** está en
> `00-RESUMEN-GLOBAL.md` (misma carpeta), por ser el CAF la capa que administra todo.

## Preámbulo

Se realizó una **auditoría exhaustiva de la plataforma Inovaweb** (6 proyectos),
cubriendo revisión de código, base de datos, seguridad OWASP y **consistencia de
contratos de integración entre proyectos**. El CAF, como orquestador de los 4 cores,
recibió el análisis más profundo. Aquí se transfieren los hallazgos propios, el fix
aplicado y los pendientes.

## 1. Veredicto del módulo

| Dimensión | Resultado |
|---|---|
| Dinero en centavos BIGINT (sin floats) | ✅ PASS (`database/001`: todos los `*_cents` BIGINT; parseo defensivo FIX-7) |
| Append-only por triggers SQL | ✅ PASS (`database/002`: audit_log, payments, adjustments; invoices con campos financieros bloqueados) |
| Auth | ✅ PASS (Argon2id; JWT httpOnly + SameSite=Strict + Secure; lockout 5 intentos) |
| Webhook Hub | ✅ PASS (HMAC + timestamp anti-replay + correlación purpose/amount + idempotencia BD) |
| OWASP | ✅ PASS con observaciones |
| Contrato Medidor (C1) | 🔴→✅ **CRÍTICO, CORREGIDO** (pendiente QA) |

## 2. Afectaciones de este módulo

### 2.1 🔴→✅ C1 (CRÍTICO) — ruta de acreditación corregida
**Hallazgo:** `app/core/clients/medidor_client.py` acreditaba en
`POST /admin/v1/wallets/{id}/credit` y compensaba la Saga con
`DELETE /admin/v1/wallets/{id}` — **rutas inexistentes** en el Medidor (que expone
credit en `/v1/wallets/{id}/credit` y **no** tiene DELETE de wallet). Toda
recarga/onboarding habría dado **404**. Latente porque el flujo prepago end-to-end
nunca se ejecutó (tasks #16/#18 pendientes). **Causa raíz:** la documentación del
propio Medidor publicaba la ruta `/admin/v1` errónea (ya corregida en el Medidor).

**Fix aplicado en esta auditoría (código tocado):**
- `medidor_client.py` — `credit` → `POST /v1/wallets/{wallet_id}/credit`.
- `medidor_client.py` — `delete_wallet` → **`suspend_wallet`** vía
  `POST /admin/v1/wallets/{id}/suspend` (endpoint real; el Medidor no tiene DELETE).
- `app/services/onboarding.py:231` — la compensación ahora llama `suspend_wallet`.
- `tests/test_onboarding.py` — aserciones actualizadas a `suspend_wallet`.
- Docstrings + `docs/ARQUITECTURA-GLOBAL.md` alineados.

**Estado QA:** `py_compile` OK; **pytest NO se pudo correr localmente** (el `.venv`
está como "solo nube" en OneDrive). **Debe correrse en Docker/VPS antes del commit.**

### 2.2 Observaciones OWASP (no bloqueantes)
- **CSRF:** cubierto por `SameSite=Strict`, pero **sin token CSRF explícito** para
  POST mutativos (mencionado en SECURITY.md, no implementado).
- **`revoked_tokens` no implementada:** el logout solo borra cookies; un JWT robado
  sigue válido hasta expirar (≤15 min).
- **`HUB_WEBHOOK_SECRET`** cae a `HUB_API_KEY` en dev/staging (`config.py:97-105`);
  en prod es obligatorio. Usar secreto dedicado en dev.
- **`/api/v2`** no valida en el schema que el monto sea entero (rechazo ocurre más
  abajo en `prepago.py:159-161`).

### 2.3 Dependencias de otros módulos que afectan al CAF
- **Centro de Mensajes:** plantillas `caf-pago-confirmado`/`caf-activacion-*` **no
  sembradas** → `send_email` daría 404. WhatsApp **501** (el CAF lo asume,
  `messages_client.py:104`).
- **Scraping:** la identidad CAF↔Scraping no está cableada (`medidor_wallet_id` sin
  ruta que lo pueble; `link-caf` inexistente).
- **Onboarding** del CAF **sin idempotencia por `request_id`** (`onboarding.py:79`
  acepta el parámetro pero no lo usa).

## 3. Qué se entregó/cambió en este módulo

- ✅ **Código (fix C1):** `medidor_client.py`, `onboarding.py`, `tests/test_onboarding.py`.
- ✅ **Docs nuevos:** `docs/ARQUITECTURA-GLOBAL.md`, `docs/OWASP.md`,
  `docs/GUIA-DESARROLLADOR.md`, `docs/GUIA-USUARIO-OPERADOR.md`,
  `docs/GUIA-USUARIO-CLIENTE.md`, `docs/RESUMEN-EJECUTIVO.md`.
- ✅ **Actualizado:** `CHANGELOG.md` (entrada 2026-06-06 con el hallazgo) y
  `CLAUDE.md §12` (estado + blocker C1).

## 4. Pendientes para el equipo CAF

1. **Correr pytest en Docker/VPS** y re-verificar QA del flujo prepago (TASK-15b)
   **antes de commitear** (se tocó código).
2. Implementar **`revoked_tokens`** (logout efectivo) y **token CSRF** para POST mutativos.
3. Validar **monto entero** en los schemas de `/api/v2`.
4. Añadir **idempotencia por `request_id`** al onboarding.
5. Coordinar con **Centro de Mensajes** (sembrar plantillas `caf-*`, habilitar WhatsApp)
   y con **Scraping** (ruta para poblar `medidor_wallet_id`).
6. Sustituir los `[TODO: verificar config Nginx real en VPS]` con la config real.

## 5. Commit (⚠️ condicionado a QA)

```bash
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera" && git add . && git commit -m "fix(medidor-client): ruta credit /v1 + compensacion suspend; docs: auditoria global completa 2026-06-06" && git push origin main
```
> **No ejecutar hasta que `pytest` pase en Docker/VPS** (se modificó código en el fix C1).
