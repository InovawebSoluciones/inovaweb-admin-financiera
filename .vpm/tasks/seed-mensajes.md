# Sembrar plantilla `caf-activacion-correo` en el Centro de Mensajes

> Generado por Claude Code (2026-06-06; actualizado 2026-06-07). Para que el
> correo de activación del onboarding (#16, paso 5b) funcione, esta plantilla
> debe existir en el Centro de Mensajes ANTES del primer alta. Sin ella, el
> envío con `origin_kind=template` devuelve 404 y el titular nunca recibe su
> enlace.

**Repo local del Centro de Mensajes:**
`C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-centro-mensajes\`

**En prod:** `mensajes.inovaweb.com.mx` (VPS 89.116.25.222, contenedor en la red
`n8n_default`, puerto host 8005). Repo en el VPS: `/opt/inovaweb-centro-mensajes`
(confirmar ruta real).

---

## ⚠️ Nota de sintaxis (IMPORTANTE)

**Llaves SIMPLES, no dobles.** El motor de render del Centro usa `{variable}`
(regex en `app/core/template_render.py:28`), NO `{{variable}}`. Con llaves dobles
renderizaría `{valor}` (con llaves literales sobrando). Toda la plantilla de
abajo usa `{nombre}`, `{token_url}`, `{expiracion_horas}` con UNA sola llave.

**Las 3 variables ya están cableadas en el CAF.** Verifiqué/corregí
`inovaweb-admin-financiera/app/services/onboarding.py:335`: el envío ahora pasa
`{token_url, nombre, expiracion_horas}`. El Centro valida que toda variable
declarada en `variables_schema` venga en el envío (`validate_variables`), así que
el schema de abajo declara exactamente esas tres y coincide con lo que el CAF
manda. (`expiracion_horas` se envía como `"24"`, que es la expiración real del
token, default de la tabla `activation_tokens`.)

---

## Método A — API REST (RECOMENDADO)

Usa la `MESSAGES_API_KEY` del CAF (es admin master, scope `*`, incluye
`admin:templates`). Endpoint: `POST /admin/v1/templates`. Auth: header
`X-API-Key`.

```bash
curl -sS -X POST https://mensajes.inovaweb.com.mx/admin/v1/templates \
  -H "X-API-Key: <MESSAGES_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "caf-activacion-correo",
    "channel": "email",
    "name": "CAF - Activacion de cuenta (correo)",
    "subject_template": "Activa tu cuenta en Inovaweb",
    "body_html_template": "<!doctype html><html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#1f2937;line-height:1.5\"><div style=\"max-width:520px;margin:0 auto;padding:24px\"><h2 style=\"color:#111827\">Hola {nombre}, bienvenido a Inovaweb</h2><p>Tu cuenta ya fue creada. Para activarla y definir tu contrasena, haz clic en el siguiente boton:</p><p style=\"text-align:center;margin:28px 0\"><a href=\"{token_url}\" style=\"background:#2563eb;color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;display:inline-block\">Activar mi cuenta</a></p><p style=\"font-size:13px;color:#6b7280\">Si el boton no funciona, copia y pega esta direccion en tu navegador:<br><a href=\"{token_url}\">{token_url}</a></p><p style=\"font-size:13px;color:#6b7280\">Este enlace expira en {expiracion_horas} horas. Si no solicitaste esta cuenta, ignora este correo.</p><hr style=\"border:none;border-top:1px solid #e5e7eb;margin:24px 0\"><p style=\"font-size:12px;color:#9ca3af\">Inovaweb - Plataforma de servicios</p></div></body></html>",
    "body_text_template": "Hola {nombre}, bienvenido a Inovaweb.\n\nTu cuenta ya fue creada. Para activarla y definir tu contrasena, abre este enlace:\n\n{token_url}\n\nEl enlace expira en {expiracion_horas} horas. Si no solicitaste esta cuenta, ignora este correo.\n\n-- Inovaweb",
    "variables_schema": { "token_url": "string", "nombre": "string", "expiracion_horas": "string" },
    "metadata": { "origen": "CAF", "proposito": "activacion-onboarding-#16" }
  }'
```

Respuesta esperada: `201 Created` con `{id, slug, version: 1, ...}`.
Si ya existe: `409` ("Use PATCH para nueva version") — significa que ya está sembrada.

**Verificar:**
```bash
curl -sS https://mensajes.inovaweb.com.mx/admin/v1/templates \
  -H "X-API-Key: <MESSAGES_API_KEY>" | grep caf-activacion-correo
```

---

## Método B — SQL directo (fallback)

Solo si la API no está disponible. Inserta en la tabla `templates` (la tabla es
append-only: INSERT permitido, UPDATE/DELETE bloqueados por trigger). El
`tenant_id` se resuelve por el slug del tenant del CAF — **confirma cuál es** con
`SELECT id, slug, name FROM tenants;` (probablemente `inovaweb`).

```bash
# Entrar al contenedor de Postgres del Centro de Mensajes en el VPS:
docker exec -i <contenedor_postgres_mensajes> psql -U messages -d centro_mensajes <<'SQL'
INSERT INTO templates (
    tenant_id, slug, version, channel, name,
    subject_template, body_html_template, body_text_template, message_template,
    variables_schema, metadata, is_active, created_by_api_key_id
)
SELECT
    t.id, 'caf-activacion-correo', 1, 'email', 'CAF - Activacion de cuenta (correo)',
    'Activa tu cuenta en Inovaweb',
    '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#1f2937;line-height:1.5"><div style="max-width:520px;margin:0 auto;padding:24px"><h2>Hola {nombre}, bienvenido a Inovaweb</h2><p>Tu cuenta ya fue creada. Para activarla y definir tu contrasena, haz clic en el boton:</p><p style="text-align:center;margin:28px 0"><a href="{token_url}" style="background:#2563eb;color:#fff;text-decoration:none;padding:12px 24px;border-radius:8px;display:inline-block">Activar mi cuenta</a></p><p style="font-size:13px;color:#6b7280">Si el boton no funciona, copia esta direccion:<br><a href="{token_url}">{token_url}</a></p><p style="font-size:13px;color:#6b7280">Este enlace expira en {expiracion_horas} horas. Si no solicitaste esta cuenta, ignora este correo.</p></div></body></html>',
    E'Hola {nombre}, bienvenido a Inovaweb.\n\nPara activar tu cuenta, abre este enlace:\n\n{token_url}\n\nExpira en {expiracion_horas} horas.\n\n-- Inovaweb',
    NULL,
    '{"token_url":"string","nombre":"string","expiracion_horas":"string"}'::jsonb,
    '{"origen":"CAF","proposito":"activacion-onboarding-#16"}'::jsonb,
    true, NULL
FROM tenants t
WHERE t.slug = 'inovaweb'
ON CONFLICT (tenant_id, slug, version) DO NOTHING;
SQL
```

(Ajusta `<contenedor_postgres_mensajes>`, el user/db de Postgres y el slug del
tenant a los valores reales del VPS.)

---

## Variables que envía el CAF (referencia)

| variable           | tipo   | valor que manda el CAF                                   |
|--------------------|--------|---------------------------------------------------------|
| `token_url`        | string | `https://app.inovaweb.com.mx/activate?token=<token>`    |
| `nombre`           | string | nombre del titular (`titular_full_name`)                |
| `expiracion_horas` | string | `"24"` (expiración real del token)                      |

Fuente: `inovaweb-admin-financiera/app/services/onboarding.py:330-339`.
