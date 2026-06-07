# DEPLOY PREP — Pasos finales antes de prod (2026-06-06)

**Repo CAF:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\`
**Repo Scraping:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\`

NO hagas commit, NO hagas push, NO levantes Docker.
Solo genera artefactos de texto listos para que Conrado los copie en el VPS.

---

## PASO 1 — Leer commits-listos.md y deploy-vps.sh

Lee ambos archivos y confirma que están completos:
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\.vpm\tasks\commits-listos.md`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\.vpm\tasks\deploy-vps.sh`

Si alguno falta o está incompleto, recréalo con el contenido correcto.

---

## PASO 2 — Generar plantilla del Centro de Mensajes

Crea el archivo:
`C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\.vpm\tasks\seed-mensajes.md`

Con instrucciones exactas para que Conrado siembre la plantilla `caf-activacion-correo` en el Centro de Mensajes. Incluir:
- Ruta del repo local del Centro de Mensajes (búscalo en el sistema de archivos bajo `C:\Users\conra\OneDrive - Inovaweb\webescolar\`)
- El comando o script exacto para insertar la plantilla (SQL o script Python según lo que use ese repo)
- Contenido HTML/texto de la plantilla con las variables `{{nombre}}`, `{{token_url}}`, `{{expiracion_horas}}`

---

## PASO 3 — Actualizar deploy-vps.sh para incluir Scraping

Lee el archivo `deploy-vps.sh` actual y agrégale al final:

```bash
# Deploy Scraping (fix D1 — alembic 0005)
echo "=== DEPLOY SCRAPING ==="
cd /opt/scraping-universidades
git pull
docker compose run --rm scraping alembic upgrade head || \
  python -m alembic upgrade head
echo "Scraping OK"
```

Si no sabes la ruta exacta del servicio Scraping en el VPS, busca en el repo de Scraping el `docker-compose.yml` para inferir el nombre del servicio y ajústalo.

---

## PASO 4 — Verificación final de archivos tocados

Corre `py_compile` sobre estos archivos y reporta el resultado:

CAF:
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\core\clients\medidor_client.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\core\clients\messages_client.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\core\clients\scraping_client.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\services\onboarding.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\services\billing.py`

Scraping:
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\app\models\company.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\app\routers\companies.py`

---

## Reporte final

Al terminar reporta:
- Estado de cada paso
- Contenido completo de `commits-listos.md` y `seed-mensajes.md`
- Resultado de `py_compile`
- Cualquier hallazgo nuevo
