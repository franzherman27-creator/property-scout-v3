# Scraper local de Zonaprop

Corre desde tu PC con IP residencial para evadir el bloqueo DataDome que afecta a Railway.

---

## Requisitos

```
pip install playwright requests
playwright install chromium
```

> Si ya tenés el entorno del proyecto instalado, `playwright` ya está.

---

## Variables de entorno

Definirlas una vez en tu sesión o en el `.bat` de Task Scheduler (ver abajo):

| Variable | Ejemplo | Descripción |
|---|---|---|
| `INGEST_TOKEN` | `mi-token-secreto-123` | Mismo valor que `INGEST_TOKEN` en Railway |
| `PROPERTY_SCOUT_URL` | `https://tu-app.railway.app` | URL del servidor en Railway (sin `/` final) |
| `ZONAS` | `la-plata,tolosa,gonnet` | Zonas a scrapear (default: `la-plata`) |
| `MAX_ENRICH` | `15` | Fichas individuales a visitar para obtener descripción |

### Setear `INGEST_TOKEN` en Railway

En tu proyecto de Railway → **Variables** → agregar:

```
INGEST_TOKEN = mi-token-secreto-123
```

---

## Correr manualmente

```bat
set INGEST_TOKEN=mi-token-secreto-123
set PROPERTY_SCOUT_URL=https://tu-app.railway.app
python zonaprop_local.py
```

Se abre un browser **visible** de Chromium. No lo cerrés ni lo minimices durante el scraping.
Si DataDome presenta un CAPTCHA, resolverlo a mano — el script espera 40 segundos antes de continuar.

Los logs quedan también en `zonaprop_local.log`.

---

## Programar en Windows Task Scheduler

### 1. Crear el .bat

Guardá este archivo como `C:\property-scout\run_scraper.bat`:

```bat
@echo off
set INGEST_TOKEN=mi-token-secreto-123
set PROPERTY_SCOUT_URL=https://tu-app.railway.app
set ZONAS=la-plata

cd /d C:\Users\Solomun\property-scout-v3
python zonaprop_local.py >> C:\property-scout\scraper.log 2>&1
```

### 2. Crear la tarea

Abrí **Task Scheduler** (`taskschd.msc`) y:

1. **Create Task** (no "Basic Task")
2. **General** → Nombre: `ZonapropScraper` → marcar *Run whether user is logged on or not* → marcar *Run with highest privileges*
3. **Triggers** → New → *Daily* → hora que quieras (ej: 09:00) → repetir cada `6 hours` por `1 day` → OK
4. **Actions** → New → *Start a program* → `C:\property-scout\run_scraper.bat`
5. **Settings** → marcar *If the task is already running, do not start a new instance*
6. OK → ingresar tu contraseña de Windows

### Verificar que funciona

```bat
# Ejecutar la tarea a mano desde Task Scheduler con botón derecho → Run
# O desde cmd:
schtasks /run /tn "ZonapropScraper"
```

---

## Notas

- El browser queda visible intencionalmente — DataDome detecta browsers headless.
- Si el bloqueo persiste, aumentá los tiempos de espera editando `PAGE_WAIT_RANGE` en `zonaprop_local.py`.
- Las propiedades enviadas se evalúan automáticamente contra todas tus búsquedas activas y aparecen en el dashboard.
- Propiedades ya conocidas se ignoran (deduplicación por URL+portal).
