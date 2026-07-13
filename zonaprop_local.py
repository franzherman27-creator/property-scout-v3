#!/usr/bin/env python3
"""
zonaprop_local.py — Scraper local de Zonaprop para Property Scout v3
Corre con browser VISIBLE (no-headless) desde IP residencial para evadir DataDome.

Uso:
    python zonaprop_local.py

Variables de entorno requeridas:
    INGEST_TOKEN          token secreto (mismo que INGEST_TOKEN en Railway)
    PROPERTY_SCOUT_URL    URL del servidor (ej: https://tu-app.railway.app)

Variables opcionales:
    ZONAS                 zonas separadas por coma (default: la-plata)
    MAX_ENRICH            fichas a visitar para descripción (default: 15)
"""

import asyncio
import hashlib
import logging
import os
import random
import re
import sys
import time
from datetime import datetime

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("zonaprop_local.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── Configuración ────────────────────────────────────────────────────────────

SERVER_URL = os.environ.get("PROPERTY_SCOUT_URL", "http://localhost:8080").rstrip("/")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
ZONAS = [z.strip() for z in os.environ.get("ZONAS", "la-plata").split(",") if z.strip()]
MAX_ENRICH = int(os.environ.get("MAX_ENRICH", "15"))

OPERACIONES = ["venta", "alquiler"]
TIPOS_URL = ["casas", "departamentos"]
MAX_PROPS_PER_URL = 100
PAGE_WAIT_RANGE = (5000, 10000)   # ms de espera post-carga (aleatorio)
INTER_URL_WAIT = (4000, 9000)     # ms entre cada URL

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

STEALTH_JS = """
// Ocultar indicadores de WebDriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
delete window.__selenium_unwrapped;
delete window.__webdriverFunc;

// Lenguajes realistas (Argentina)
Object.defineProperty(navigator, 'languages', {
  get: () => ['es-AR', 'es', 'en-US', 'en']
});

// Plugins: Chrome real tiene varios (DataDome los verifica)
Object.defineProperty(navigator, 'plugins', {
  get: () => {
    const arr = [
      {name:'Chrome PDF Plugin'}, {name:'Chrome PDF Viewer'},
      {name:'Native Client'}, {name:'Widevine Content Decryption Module'}
    ];
    arr.item = i => arr[i];
    arr.namedItem = n => arr.find(p => p.name === n) || null;
    arr.refresh = () => {};
    return arr;
  }
});

// Chrome runtime completo
window.chrome = {
  runtime: {id: undefined},
  loadTimes: () => ({}),
  csi: () => ({})
};

// Permissions API — DataDome la consulta activamente
const _origQuery = window.navigator.permissions.query.bind(navigator.permissions);
window.navigator.permissions.query = params =>
  params.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : _origQuery(params);

// Hardware concurrency real
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
"""

DETAIL_PATTERN = re.compile(r"zonaprop\.com\.ar/propiedades/.+\.html")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_id(url: str, portal: str = "zonaprop") -> str:
    return hashlib.md5(f"{portal}-{url}".encode()).hexdigest()[:12]


def parse_precio(texto: str):
    if not texto:
        return None, None
    t = texto.upper().replace("\xa0", " ")
    moneda = None
    if any(k in t for k in ("USD", "U$S", "U$D", "US$", "DÓLAR", "DOLAR")):
        moneda = "usd"
    elif "$" in t or "PESO" in t or "ARS" in t:
        moneda = "ars"
    nums = re.findall(r"[\d][\d.,]*", t)
    if not nums:
        return None, moneda
    monto = nums[0].replace(".", "").replace(",", "")
    try:
        return int(monto), moneda
    except ValueError:
        return None, moneda


async def human_scroll(page, steps: int = 7):
    """Scroll lento y variable para cargar contenido lazy y parecer humano."""
    for _ in range(steps):
        delta = random.randint(250, 650)
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(random.uniform(0.3, 1.1))


async def human_move(page):
    """Movimiento de mouse aleatorio."""
    for _ in range(random.randint(2, 5)):
        x = random.randint(100, 1300)
        y = random.randint(100, 800)
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.1, 0.4))


# ─── Scraping ─────────────────────────────────────────────────────────────────

async def check_blocked(page, url: str) -> bool:
    """Detecta page de challenge DataDome y lo reporta claramente."""
    try:
        title = (await page.title() or "").lower()
        body_len = await page.evaluate("() => document.body.innerText.length")
        markers = ("denied", "captcha", "robot", "blocked", "verify",
                   "attention required", "access denied", "datadome",
                   "just a moment", "challenge")
        if any(m in title for m in markers) or body_len < 800:
            log.warning("⛔ BLOQUEADO — title='%s' body=%s chars", title[:80], body_len)
            log.info("   El browser quedará abierto 40s — si hay CAPTCHA resolvelo a mano.")
            await page.wait_for_timeout(40000)
            # Verificar si se resolvió
            body_len2 = await page.evaluate("() => document.body.innerText.length")
            if body_len2 > 800:
                log.info("   Bloqueo superado manualmente, continuando.")
                return False
            return True
    except Exception:
        pass
    return False


async def scrape_url(page, url: str) -> list:
    log.info("→ %s", url)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=50000)
    except Exception as e:
        log.error("  Error cargando: %s", e)
        return []

    wait_ms = random.randint(*PAGE_WAIT_RANGE)
    log.info("  Esperando %dms...", wait_ms)
    await page.wait_for_timeout(wait_ms)

    if await check_blocked(page, url):
        return []

    await human_move(page)
    await human_scroll(page)
    await page.wait_for_timeout(random.randint(800, 1800))

    anchors = await page.eval_on_selector_all(
        "a[href]",
        """els => els.map(a => ({
             href: a.href,
             text: (a.closest('div,article,li,section,h2,h3') || a)
                     .innerText.slice(0, 500)
           }))""",
    )

    props, seen = [], set()
    for a in anchors:
        href = a["href"].split("?")[0].split("#")[0]
        if not DETAIL_PATTERN.search(href) or href in seen:
            continue
        seen.add(href)
        texto = (a.get("text") or "").strip()
        precio_m = re.search(r"(USD|US\$|U\$S|\$)\s?[\d.,]+", texto)
        precio, moneda = parse_precio(precio_m.group(0) if precio_m else "")
        lineas = [l.strip() for l in texto.split("\n") if 5 < len(l.strip()) < 120]
        zona_texto = lineas[1] if len(lineas) > 1 else ""
        props.append({
            "id": make_id(href),
            "portal": "zonaprop",
            "url": href,
            "titulo": (lineas[0] if lineas else href)[:200],
            "precio": precio,
            "moneda": moneda,
            "zona_texto": zona_texto[:200],
            "descripcion": "",
            "fecha_detectada": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        if len(props) >= MAX_PROPS_PER_URL:
            break

    log.info("  → %d propiedades", len(props))
    if not props and anchors:
        sample = [a["href"] for a in anchors if "/" in a["href"]][:5]
        log.warning("  0 links matchearon. Muestra de hrefs: %s", sample)
    return props


async def enrich_descriptions(page, props: list):
    """Visita cada ficha para obtener título y descripción completos."""
    to_enrich = [p for p in props if not p.get("descripcion")][:MAX_ENRICH]
    if not to_enrich:
        return
    log.info("Enriqueciendo %d fichas...", len(to_enrich))
    for prop in to_enrich:
        try:
            await page.goto(prop["url"], wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(random.randint(2000, 4500))

            if await check_blocked(page, prop["url"]):
                log.warning("  Enriquecimiento bloqueado, saltando el resto.")
                break

            texto = await page.evaluate(
                """() => {
                    const sel = document.querySelector(
                      '#longDescription, [data-qa*="DESCRIPTION"], .description,'
                      + ' #descripcion, [class*="description"], [class*="descripcion"],'
                      + ' article, main');
                    return (sel || document.body).innerText;
                }"""
            )
            prop["descripcion"] = re.sub(r"\n{3,}", "\n\n", texto or "").strip()[:3500]
            if not prop["titulo"] or prop["titulo"] == prop["url"]:
                prop["titulo"] = (await page.title() or "")[:200]
            log.info("  ✓ %s", prop["url"][-65:])
        except Exception as e:
            log.warning("  enrich %s: %s", prop["url"][-60:], e)
        await asyncio.sleep(random.uniform(1.5, 3.5))


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run():
    from playwright.async_api import async_playwright

    all_props: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
            ],
        )
        context = await browser.new_context(
            user_agent=UA,
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        # Calentar sesión visitando la home para obtener cookies naturales
        log.info("Calentando sesión en zonaprop.com.ar...")
        try:
            await page.goto("https://www.zonaprop.com.ar/",
                            wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(random.randint(4000, 7000))
            await human_move(page)
            await human_scroll(page, steps=3)
            await page.wait_for_timeout(random.randint(2000, 4000))
        except Exception as e:
            log.warning("Warmup home: %s", e)

        # Scrapear todas las combinaciones
        urls_total = len(OPERACIONES) * len(TIPOS_URL) * len(ZONAS)
        procesadas = 0
        for operacion in OPERACIONES:
            for tipo_url in TIPOS_URL:
                for zona in ZONAS:
                    url = f"https://www.zonaprop.com.ar/{tipo_url}-{operacion}-{zona}.html"
                    props = await scrape_url(page, url)
                    if props:
                        await enrich_descriptions(page, props)
                    all_props += props
                    procesadas += 1
                    if procesadas < urls_total:
                        wait = random.randint(*INTER_URL_WAIT)
                        log.info("Pausa entre URLs: %dms", wait)
                        await page.wait_for_timeout(wait)

        await page.close()
        await context.close()
        await browser.close()

    # Dedup local por ID
    seen: set[str] = set()
    unique: list[dict] = []
    for p in all_props:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)

    log.info("━━ Total propiedades únicas: %d", len(unique))

    if not unique:
        log.warning("Sin propiedades — nada que enviar al servidor.")
        sys.exit(1)

    # Enviar al servidor
    if not INGEST_TOKEN:
        log.error("INGEST_TOKEN no configurado. Exportá la variable de entorno.")
        sys.exit(1)

    log.info("Enviando a %s/api/properties/ingest...", SERVER_URL)
    try:
        r = requests.post(
            f"{SERVER_URL}/api/properties/ingest",
            json={"properties": unique},
            headers={
                "X-Ingest-Token": INGEST_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=120,
        )
        if r.ok:
            result = r.json()
            log.info("✓ Servidor: %s", result)
        else:
            log.error("✗ Error %s: %s", r.status_code, r.text[:400])
            sys.exit(1)
    except requests.exceptions.ConnectionError:
        log.error("✗ No se pudo conectar a %s — ¿el servidor está corriendo?", SERVER_URL)
        sys.exit(1)
    except Exception as e:
        log.error("✗ Error al enviar: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    log.info("━━ Zonaprop Local Scraper — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("   Servidor : %s", SERVER_URL)
    log.info("   Zonas    : %s", ZONAS)
    log.info("   Operación: %s", OPERACIONES)
    log.info("   Tipos    : %s", TIPOS_URL)
    asyncio.run(run())
