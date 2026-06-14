# Módulos bajo control del CAF (los 4 cores Nivel 1)

El **CAF** (Centro de Administración, Nivel 2 — repo `inovaweb-admin-financiera`) **controla y administra** los 4 cores de infraestructura (Nivel 1). El CAF es el plano de orquestación, cobro y contabilidad; los cores son APIs especializadas. Este directorio documenta cada módulo **desde la óptica del CAF**: qué es, qué endpoints le consume el CAF, cómo se opera y sus gotchas.

> Fuente de verdad de cada módulo: su propio repo en el VPS (lo que corre). Estos docs se generan/actualizan con el skill `inovaweb-documentacion` (protocolo "traslada").

## Los 4 módulos

| Módulo | Doc | Rol | Repo VPS | Contenedor | Puerto host | GitHub |
|---|---|---|---|---|---|---|
| **Medidor** | [medidor.md](medidor.md) | Mide y tarifica consumo de IA; wallets + holds | `/opt/medidor_ia` | `medidor-api` (+`medidor-jobs`) | `127.0.0.1:8007` | `InovawebSoluciones/medidor_ia` |
| **Finanzas-Core** | [finanzas.md](finanzas.md) | Ledger inmutable de ingresos | `/opt/inovaweb-finanzas-core` | `finanzas_core` | `127.0.0.1:8004` | `InovawebSoluciones/inovaweb-finanzas-core` |
| **Centro de Mensajes** | [centro-mensajes.md](centro-mensajes.md) | Email/WhatsApp/push + reportes de consumo | `/opt/inovaweb-centro-mensajes` | `centro_mensajes` | `127.0.0.1:8005` | `InovawebSoluciones/inovaweb-centro-mensajes` |
| **Hub de Pasarelas** | [hub-pasarelas.md](hub-pasarelas.md) | Central de pagos (Conekta/OXXO/SPEI/tarjeta) | `/opt/inovaweb-hub-pasarelas` | `hub_pasarelas` | `127.0.0.1:8003` | `InovawebSoluciones/inovaweb-hub-pasarelas` |

Todos publican solo en **loopback**; el acceso público (`medidor/finanzas/mensajes/hub.inovaweb.com.mx`) lo termina el reverse proxy del stack (Caddy/Nginx en `n8n_default`). El CAF los alcanza por sus `*_BASE_URL` (dominios públicos) con su API key respectiva.

## Cómo el CAF administra cada módulo (resumen de contratos)

| Módulo | Cliente en el CAF | Lo que el CAF hace | Auth |
|---|---|---|---|
| Medidor | `app/core/clients/medidor_client.py` | Crea wallet, **acredita** (credit) por recarga, lee balance/usage, suspende. **No debita** ni hace authorize/finish. | `X-Api-Key` ADMIN |
| Finanzas | `app/core/clients/finanzas_client.py` | Asienta consumos/ingresos (`post_entry`), lee balance/totals/entries. **No crea cuentas ni llaves.** | `X-API-Key` (`fz_caf_…`) |
| Centro Mensajes | `app/core/clients/messages_client.py` | Envía email por plantilla `caf-*`, lee uso por canal/cliente. WhatsApp = 501. | `X-API-Key` |
| Hub | `app/core/clients/hub_client.py` | Inicia cobros (`POST /hub/v1/charge`); recibe el webhook `payment.paid` y acredita. | `X-API-Key` `payments:write` |

## Reglas transversales (válidas para los 4 + el CAF)
- **Dinero en centavos enteros BIGINT.** Nunca floats. Verificado en los 4 cores y el CAF.
- **Append-only en lo financiero** (ledger/transactions/payments/messages) por triggers de Postgres: correcciones = entrada nueva (reversa/compensación), nunca UPDATE/DELETE.
- **Identidad cross-core:** el CAF mapea su `clients.id` al `external_user_id = "client-<id>"` en cada core; el `tenant_id` lo resuelve la API key del CAF (nunca el body).
- **Idempotencia obligatoria** en escrituras de dinero: Medidor `request_id`, Finanzas `source_ref`, Hub `hub_transaction_id`, Centro `meta.source_ref`.
- **Regla de pagos:** TODO pago pasa por el **Hub**, nunca por una pasarela directa.
- **Regla de cobro:** el **Medidor mide** (consumo IA), el **CAF tarifica y cobra** (saldo prepago nativo `prepaid_ledger`; ver ADR-015/016). Finanzas solo asienta lo que el CAF le manda.
- **Migraciones SQL** de cada core se aplican **solo en el primer arranque** del volumen Postgres; las posteriores se corren a mano (`psql`).

## Pendientes conocidos a nivel plataforma (de los 4 docs)
- **Hub:** ⚠️ **drift VPS↔GitHub** — el commit D2 (`7281722`, notificación al CAF) corre en prod pero **no está pusheado**, y el VPS está 2 commits atrás de remoto. GitHub no refleja producción. Reconciliar (patrón del CAF: merge favoreciendo el VPS + push).
- **Centro de Mensajes:** plantillas `caf-*` **no sembradas** (→404) y `tenant_channel_credentials` **vacío** (sin proveedor de email → `send_email` real falla `no_credentials`). WhatsApp/SMS = 501. Pendiente Resend/M365.
- **Medidor:** `/opt/medidor_ia` **no es repo git** en el VPS (sincronía con GitHub manual); `medidor-jobs` aparece `unhealthy` (falso positivo); API key `mk_prod_` filtrada en un workflow n8n (rotar).
- **Finanzas / Centro / Hub:** acceso público depende del reverse proxy del stack n8n; si esa red cae, los dominios no resuelven aunque los contenedores estén `healthy`.
