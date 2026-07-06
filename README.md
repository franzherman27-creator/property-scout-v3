# Property Scout v3 — Agente de búsqueda con IA

Evolución del agente de mayo. La diferencia: ya no filtra por keywords —
**entiende el pedido y evalúa cada publicación como lo haría un martillero**.

## Cómo funciona

1. Escribís el pedido en lenguaje natural en el dashboard, tal cual te lo pasó
   el cliente: *"casa apto Banco Nación, zona norte, hasta 150.000 USD, 2 dorm
   y parque"*.
2. Claude (Sonnet) lo interpreta una sola vez: tipo, zonas, precio, y separa
   **requisitos duros** (sin esto no sirve) de **deseables** (suman pero no
   excluyen).
3. Cada hora el agente barre el círculo de portales que publican para La Plata:
   MercadoLibre (API oficial), Zonaprop, Argenprop, RE/MAX, Century 21
   (Playwright) e InmoBúsqueda (estático).
4. Un pre-filtro barato descarta lo obvio (precio fuera de rango). A cada
   publicación **nueva** que sobrevive, el agente le lee la descripción completa
   y Claude (Haiku) la evalúa: score 0–100, veredicto, razones concretas y qué
   falta confirmar por teléfono.
5. Los matches aparecen en el dashboard y, si configurás Telegram, te llegan
   al celular.

Reglas de evaluación clave:
- Requisito duro **contradicho** por la publicación → descartada.
- Requisito duro **no mencionado** (ej. "apto crédito", que casi nunca se
  publica) → no descarta: va a "Para revisar" con el dato marcado a confirmar.
- Solo se evalúan publicaciones nuevas → el costo de API se mantiene en
  centavos por día en uso normal.

## Deploy en Railway

**Si el servicio de mayo sigue vivo:** reemplazá el contenido del repo de
GitHub por estos archivos, commit y push. Railway detecta el `Dockerfile` y
redeploya solo.

**Si arrancás de cero:**
1. Repo nuevo en GitHub (privado) con todos estos archivos.
2. En railway.app → New Project → Deploy from GitHub repo.
3. Railway detecta el `Dockerfile` (imagen oficial de Playwright — no hace
   falta instalar navegadores a mano).

**Variables de entorno (Settings → Variables):**

| Variable | Obligatoria | Para qué |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sí | La capa de IA. Sin esto el agente no interpreta ni evalúa. |
| `TELEGRAM_BOT_TOKEN` | No | Avisos de matches nuevos al celular. |
| `TELEGRAM_CHAT_ID` | No | Tu chat de Telegram. |

**Persistencia:** el filesystem de Railway se borra en cada redeploy. Para que
las búsquedas y el historial sobrevivan: en el servicio → Settings → Volumes →
montar un volumen en `/app/data`.

**Telegram (5 minutos):** hablarle a @BotFather → `/newbot` → copiar el token.
Después mandarle un mensaje al bot y abrir
`https://api.telegram.org/bot<TOKEN>/getUpdates` para leer tu `chat_id`.

## Probar antes de deployar

```bash
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY=sk-ant-...

# One-shot desde la terminal (parsea, barre y evalúa una vez):
python cli.py "casa a reciclar en tolosa o la plata hasta 100000 usd"

# O el servidor completo:
python app.py   # → http://localhost:8080
```

## Mantenimiento

Los portales rediseñan sus páginas cada tanto. El agente tiene dos capas de
defensa: selectores específicos y, si fallan, un barrido genérico de links de
detalle. Si un portal deja de traer resultados:

- Punto de ajuste: `DETAIL_PATTERNS` y las funciones `scrape_*` en `scraper.py`.
- `python cli.py "..."` muestra en el log cuántas publicaciones trae cada portal
  — ahí se ve al toque cuál se cayó.

## Estructura

```
property-scout-v3/
├── app.py           ← servidor Flask + scheduler + Telegram
├── ai_matcher.py    ← capa de IA: interpretar pedido + evaluar propiedades
├── scraper.py       ← círculo de portales (Playwright + API ML + estáticos)
├── cli.py           ← prueba one-shot desde terminal
├── static/index.html← dashboard (sistema visual VR)
├── requirements.txt
├── Dockerfile       ← imagen Playwright lista para Railway
└── Procfile         ← fallback si no se usa Docker
```
