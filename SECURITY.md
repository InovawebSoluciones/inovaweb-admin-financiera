# Modelo de seguridad - inovaweb-admin-financiera

Centro de Administracion Financiera de la plataforma Inovaweb. Criticidad
CRITICA - es el unico punto de entrada con auth humana (login/password) hacia
los 4 cores Nivel 1, gestiona certificados CSD para timbrado fiscal, custodia
secretos del PAC, expone portal a clientes externos con visibilidad sobre su
informacion contable. Comprometerlo = poder timbrar facturas falsas con el RFC
de Inovaweb, falsificar saldos, suplantar a clientes y emitir cargos contra
sus medios de pago.

---

## 1. Activos y superficies

| Activo | Sensibilidad | Donde vive |
|---|---|---|
| Certificado de sello digital (CSD) `.cer` + `.key` SAT | Critico fiscal | Secrets/volume cifrado, `KEY_PASSWORD` en `.env` |
| API keys del CAF hacia los 4 cores Nivel 1 (medidor/hub/messages/finanzas) | Critico | `.env` (`*_API_KEY`) |
| `PAC_API_KEY` / `PAC_API_SECRET` (Facturama / Soluc. Factible) | Critico fiscal | `.env` |
| `JWT_SECRET` (firma de tokens de sesion) | Critico | `.env` |
| `AES_KEY` (cifra credenciales del PAC y datos sensibles del cliente en BD) | Critico | `.env` |
| Tabla `users` con hashes Argon2id de passwords + email + rol | Critico | Postgres, append-only en columnas criticas |
| Tabla `audit_log` (quien hizo que, cuando, valor anterior/nuevo) | Critico | Postgres, append-only enforced |
| Tabla `invoices` (CFDIs emitidos, UUID SAT, totales) | Critico fiscal | Postgres, append-only enforced |
| Tabla `clients` (datos comerciales, RFC, contacto, plan vigente) | Alto | Postgres |
| Tabla `payments` / `adjustments` (movimientos contables locales) | Critico | Postgres, append-only |
| Session cookies (httpOnly, SameSite=Strict) en el navegador del usuario | Critico | Browser del operador / cliente |

Endpoints expuestos a internet via Caddy (dos dominios distintos):

**`admin.inovaweb.com.mx` (operador interno):**
- `GET /login`, `POST /login` - publicos
- `GET /admin/*` - JWT obligatorio con rol admin/finanzas/lectura
- `POST /admin/clients` - alta atomica cross-core
- `GET /api/v2/*` - JSON autenticado con JWT o API key admin del CAF
- `POST /webhooks/{pac|hub-payment-paid}` - publicos, firmados por el emisor

**`app.inovaweb.com.mx` (portal cliente):**
- `GET /login`, `POST /login` - publicos
- `GET /portal/*` - JWT obligatorio con rol client; filtro por `client_id` del usuario
- `POST /portal/recharge` - JWT + rate limit estricto

**Comunes ambos dominios:**
- `GET /health`, `GET /health/db` - publicos sin secretos
- `GET /docs` `/openapi.json` - solo dev; ocultos en prod

---

## 2. Controles aplicados

### 2.1 Integridad de operaciones financieras
- `invoices`, `payments`, `adjustments`, `audit_log`: triggers en BD bloquean
  DELETE y UPDATE de columnas criticas.
- Solo INSERT permitido. Correcciones = nuevas entradas (notas de credito,
  ajustes con motivo obligatorio).
- `users.password_hash` puede actualizarse (cambio de password), pero el
  cambio queda registrado en `audit_log` con timestamp y actor.
- Reversion contable = nueva entrada con direction opuesto + `parent_entry_id`
  apuntando a la original.

### 2.2 Autenticacion con usuario y contrasena
- **Argon2id** para hash de passwords. Parametros: memory=64MB, iterations=3,
  parallelism=4. NUNCA SHA-256 simple ni bcrypt.
- Password minimo 12 chars, validador zxcvbn score >=3.
- Password reset por email con token de un solo uso, expira en 30 min,
  invalida sesiones activas tras uso.
- **2FA obligatorio para rol super-admin** (TOTP via Google Authenticator).
- Bloqueo automatico tras 5 intentos fallidos en 15 minutos (rate limit por
  email + IP).
- Mensaje de error unificado "credenciales invalidas" - previene enumeracion
  de emails registrados.

### 2.3 Sesiones JWT
- Access token corto: 15 minutos. Refresh token largo: 30 dias con rotacion
  (cada refresh invalida el anterior).
- Cookies: `HttpOnly=true`, `Secure=true`, `SameSite=Strict`, `Path=/`.
- JWT firmado con HS256 + `JWT_SECRET` rotable.
- Claims minimos: `sub` (user_id), `role`, `client_id` (si rol=client),
  `exp`, `iat`, `jti` (para revocacion).
- Tabla `revoked_tokens` con `jti` + `revoked_at` para logout efectivo.
- Validacion de `client_id` en cada request del portal: el usuario solo ve
  recursos donde `recurso.client_id = token.client_id`.

### 2.4 Cifrado de secretos
- `AES_KEY` se usa para cifrar en BD: credenciales del PAC, passwords de CSD,
  datos sensibles opcionales (telefono, RFC duplicado).
- Llave de 32 bytes base64, validada al arranque (fail-fast si invalida).
- Nonce de 12 bytes por cifrado, generado con `os.urandom` (CSPRNG).
- Mensajes de error genericos al fallar descifrado (no diferenciar tampering
  de key incorrecta).

### 2.5 Multi-rol estricto y autorizacion por endpoint
- Roles: `super-admin`, `finanzas`, `lectura`, `client-titular`, `client-user`.
- Cada endpoint declara su rol minimo via decorador `@require_role(...)`.
- Cross-tenant guard en portal cliente: cada query SQL filtra por
  `clients.id = :client_id` resuelto del JWT.
- 404 sin diferenciar "no existe" de "no es tuyo" (IDOR guard).
- Endpoints admin verifican rol antes de cualquier I/O.

### 2.6 Onboarding atomico cross-core (patron Saga)
- `POST /admin/clients` ejecuta secuencia:
  1. INSERT cliente local
  2. POST a finanzas-core (crear tenant + emitir API key)
  3. POST a medidor (crear wallet + emitir API key)
  4. POST a hub-pasarelas (crear config + emitir API key)
  5. POST a centro-mensajes (crear tenant + emitir API key)
  6. Si todos ok -> commit local + entrega de credenciales
- Si cualquiera falla, se ejecutan las compensaciones inversas y se registra
  el incidente en audit_log con estado `onboarding_rollback`.
- Idempotencia: `request_id` UNIQUE por intento; reintento del operador con
  mismo request_id no duplica.

### 2.7 Facturacion electronica CFDI 4.0
- Certificados CSD almacenados en volume con permisos `600`, dueño root.
- `KEY_PASSWORD` en `.env`, nunca en BD ni logs.
- Sellado del XML hecho en proceso, sin enviar `.key` al PAC.
- Validacion de respuesta del PAC: UUID timbrado, firma del SAT, fecha de
  certificacion.
- Cola interna `invoice_queue` con reintento exponencial si PAC esta caido,
  hasta MAX_ATTEMPTS=8, despues escala a `manual` para revision humana.
- Notas de credito generan nuevo CFDI tipo `E` (Egreso), nunca modifican el
  CFDI original.

### 2.8 Validacion de webhooks entrantes
- `POST /webhooks/pac`: firma HMAC SHA-256 con secreto compartido del PAC.
- `POST /webhooks/hub-payment-paid`: firma HMAC SHA-256 con secreto compartido
  del hub-pasarelas (mismo patron que webhooks del centro-mensajes).
- Rechazar antes de procesar si firma invalida o timestamp fuera de ventana
  (+/- 5 min).
- Idempotencia: `external_event_id` UNIQUE.

### 2.9 Recargas asistidas (portal cliente)
- `POST /portal/recharge` requiere JWT client + rate limit por client_id.
- Monto minimo y maximo configurables por plan del cliente.
- Inicia flujo en hub-pasarelas con `request_id` determinista.
- El cliente NUNCA toca directamente las credenciales del hub; el CAF
  intermediar mediante su key admin.
- Confirmacion llega via webhook `/webhooks/hub-payment-paid`, que acredita
  saldo en medidor y registra en `payments` + `audit_log`.

### 2.10 Validacion de input
- Pydantic con `Field` validators en todos los endpoints:
  - RFC validado con regex SAT (12/13 chars, formato fisica/moral).
  - Email validado con `email-validator`.
  - Telefonos en E.164.
  - Montos en centavos enteros, rango `[1, 100_000_000]` (max 1 millon MXN).
  - Codigos promocionales: `[a-zA-Z0-9-]{4,32}`.
  - Body max 256 KB en endpoints API; 2 MB en upload de logos / CSD.

### 2.11 Logging y observabilidad
- JSON-lines estructurado con `request_id`, `user_id`, `client_id`, `path`,
  `status`, `latency_ms`.
- Access log al cierre de cada request (excepto `/health`).
- **NUNCA en logs:** passwords, password hashes, JWT completo, `JWT_SECRET`,
  `AES_KEY`, API keys hacia los 4 cores, `PAC_API_SECRET`, `KEY_PASSWORD`,
  contenido del CSD, RFC completo del cliente (solo prefijo).
- Audit log SEPARADO del access log: tabla `audit_log` con campos
  `actor_user_id`, `actor_ip`, `action`, `entity_type`, `entity_id`,
  `prev_value`, `new_value`, `created_at`.

### 2.12 Hardening de transporte (Caddy del stack n8n)
- HSTS 2 anos con `includeSubDomains preload` (ambos dominios).
- X-Frame-Options DENY, X-Content-Type-Options nosniff,
  Referrer-Policy strict-origin-when-cross-origin.
- CSP estricta para HTML: `default-src 'self'; script-src 'self' 'unsafe-inline';
  style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'`.
- HTMX no requiere `eval` ni `unsafe-eval` - CSP permanece sin `unsafe-eval`.
- Permissions-Policy bloquea FLoC/geo/mic/cam.
- Body max 2 MB en Caddy (acomoda upload de CSD).
- TLS 1.3 con Let's Encrypt auto-renovado.

### 2.13 Hardening de contenedor (audit fix aprobado en centro-mensajes)
- `read_only: true` + tmpfs para `/tmp`.
- `cap_drop: [ALL]`, `security_opt: [no-new-privileges]`.
- Usuario non-root UID 10001.
- `mem_limit: 512m`, `cpus: 1.0` para no derribar otros cores del VPS.

---

## 3. Amenazas y mitigaciones

| Amenaza | Mitigacion |
|---|---|
| Suplantacion del operador interno (robo de sesion) | 2FA obligatorio super-admin, refresh token rotativo, IP fingerprint en sesion |
| Ataque de fuerza bruta a `/login` | Bloqueo automatico tras 5 intentos en 15 min por email + IP, rate limit Caddy global |
| SQL injection | sqlalchemy.text() con bind params; sin interpolacion de input |
| CSRF en portal cliente | SameSite=Strict en cookies + tokens CSRF de origen para POST mutativos |
| XSS en plantillas Jinja2 | autoescape activo + CSP estricta sin unsafe-eval |
| IDOR en portal (cliente A intentando ver factura del cliente B) | Filtro `WHERE client_id = :token.client_id` en TODA query del portal + 404 sin diferenciar |
| Onboarding parcial (falla en uno de los 4 cores) | Patron Saga con compensacion + audit_log de cada paso + alerta operativa |
| CFDI duplicado por replay del trigger | UNIQUE en `invoice.id_local` y validacion previa de existencia antes de timbrar |
| Robo de CSD del SAT | Volume con permisos 600 dueño root, no expuesto en docker inspect, key_password en .env distinto |
| Token JWT robado del navegador del cliente | HttpOnly cookies (no accesible via JS) + SameSite=Strict + short access token |
| Webhook PAC falsificado | Validacion HMAC obligatoria + ventana temporal +/- 5 min |
| Webhook hub-pasarelas falsificado | Misma validacion HMAC con secreto compartido |
| Fuga de credenciales por error en logs | Filtro de keys conocidas en formatter + lista negra de campos sensibles |
| Volcado de BD comprometido | Passwords con Argon2id (no descifrables); credenciales PAC cifradas con AES_KEY (separada del BD) |
| Cancelacion abusiva de facturas (operador interno deshonesto) | Cancelacion solo via NUEVO CFDI tipo E + audit_log + alerta a super-admin |
| Cliente intentando timbrar factura sin cerrarse el mes | Flag `closing_finalized` en BD; emision solo permitida si flag=true |
| Sobreescritura de configuracion del PAC | Tabla `pac_config` append-only; cambio = nueva fila + flag is_active |

---

## 4. Checklist para cambios futuros

### Agregar un nuevo rol
- [ ] Insertar slug del rol en `roles` table (catalogo cerrado).
- [ ] Definir permisos atomicos asociados en `role_permissions`.
- [ ] Decorador `@require_role(...)` en endpoints aplicables.
- [ ] Tests: auth fail (401), rol insuficiente (403), happy path.

### Agregar un nuevo PAC alternativo
- [ ] Implementar interface `PacClient` en `app/core/clients/pac/<nombre>.py`.
- [ ] Insertar fila en `pac_providers` table.
- [ ] Setear `PAC_PROVIDER` en `.env`.
- [ ] Tests: timbrado happy path + manejo de error transitorio + retry idempotente.

### Agregar un nuevo metodo de pago al portal
- [ ] Verificar que hub-pasarelas lo soporte primero.
- [ ] Mapear slug del hub a opcion visible en `/portal/recharge`.
- [ ] Tests: flujo end-to-end con webhook simulado.

### Nuevo endpoint admin
- [ ] JWT obligatorio + `@require_role` con rol minimo necesario.
- [ ] Audit log emitido para cualquier operacion de escritura.
- [ ] Pydantic con validadores estrictos.
- [ ] Tests: auth fail, rol fail, validation fail, happy path.

### Migracion SQL
- [ ] Sufijo `00X_descripcion.sql` con `IF NOT EXISTS` para idempotencia.
- [ ] No tocar columnas append-only existentes salvo ADD COLUMN.

---

## 5. Pendientes de seguridad

- [ ] Pentest externo antes de exponer portal cliente a internet productivo.
- [ ] Activar Redis para rate limiting real (`REDIS_URL` en `.env`).
- [ ] Implementar 2FA opcional para clientes externos (no solo super-admin).
- [ ] Allowlist de IPs por PAC en Caddy para `/webhooks/pac`.
- [ ] Allowlist de IPs por hub para `/webhooks/hub-payment-paid`.
- [ ] Rotacion programada de `JWT_SECRET` (procedimiento + version-aware tokens).
- [ ] Rotacion programada de API keys hacia los 4 cores (mensual).
- [ ] Audit log shipping externo (S3 con object lock) para tamper-evident.
- [ ] Backup cifrado de CSD en 2 ubicaciones offline.
- [ ] Procedimiento documentado de revocacion rapida de cliente comprometido.
- [ ] Verificacion de identidad fiscal (constancia SAT vigente) antes de
      timbrar primera factura por cliente.
- [ ] Detector de anomalias: cierre mensual con monto >5x el promedio del
      cliente dispara alerta y queda en estado `pending_review`.
- [ ] WAF a nivel Caddy para `/login` y `/portal/recharge` (signatures comunes
      OWASP Top-10).
