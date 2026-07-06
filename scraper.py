"""
Property Scout v3 — Motor de rastreo
Círculo de portales que publican para La Plata y alrededores:

  MercadoLibre   → API oficial (links reales garantizados)
  Zonaprop       → Playwright (JS dinámico)
  Argenprop      → Playwright (JS dinámico)
  RE/MAX         → Playwright (JS dinámico)
  Century 21     → Playwright (JS dinámico)
  InmoBúsqueda   → requests + BS4 (HTML estático)

Estrategia híbrida por portal: primero selectores específicos, si fallan cae
a un barrido genérico de links de detalle (sobrevive mejor a rediseños).
Las propiedades nuevas se "enriquecen" visitando su página de detalle para
capturar la descripción completa — es lo que le da material a la IA.

Punto de mantenimiento: los diccionarios PORTAL_URLS y DETAIL_PATTERNS.
"""

import asyncio
import hashlib
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

MAX_ENRICH_PER_PORTAL = 25   # tope de páginas de detalle por portal por scan
PAGE_TIMEOUT_MS = 30000

# Patrones de URL de detalle por portal (fallback genérico + validación)
DETAIL_PATTERNS = {
    "zonaprop":  re.compile(r"zonaprop\.com\.ar/propiedades/.+\.html"),
    "argenprop": re.compile(r"argenprop\.com/.+--\d{6,}"),
    "remax":     re.compile(r"remax\.com\.ar/listings/[^?#]+"),
    "century21": re.compile(r"century21\.com\.ar/propiedad/[^?#]+"),
}

# Slugs de zona → variantes por portal
ZONA_PORTAL = {
    "zonaprop":     lambda z: z,
    "argenprop":    lambda z: z,
    "inmobusqueda": lambda z: z,
}

TIPO_PLURAL = {
    "casa": "casas", "departamento": "departamentos", "ph": "ph",
    "lote": "terrenos", "local": "locales-comerciales", "quinta": "quintas",
    "otro": "propiedades",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_id(url: str, portal: str) -> str:
    return hashlib.md5(f"{portal}-{url}".encode()).hexdigest()[:12]


def parse_precio(texto: str):
    """'USD 85.000' / 'U$S 85.000' / '$ 120.000.000' → (monto, moneda)."""
    if not texto:
        return None, None
    t = texto.upper().replace("\xa0", " ")
    moneda = None
    if "USD" in t or "U$S" in t or "U$D" in t or "DÓLAR" in t or "DOLAR" in t:
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


def _prop(portal, url, titulo="", precio_txt="", zona_texto=""):
    precio, moneda = parse_precio(precio_txt)
    return {
        "id": make_id(url, portal),
        "portal": portal,
        "url": url,
        "titulo": (titulo or "").strip()[:200],
        "precio": precio,
        "moneda": moneda,
        "zona_texto": (zona_texto or "").strip()[:200],
        "descripcion": "",
        "fecha_detectada": time.strftime("%Y-%m-%d %H:%M"),
    }


# ─── Playwright helpers ───────────────────────────────────────────────────────

async def _new_page(context):
    page = await context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    return page


async def _goto(page, url):
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)  # dejar renderizar el JS


async def _generic_sweep(page, portal: str, base_url: str) -> list:
    """Fallback: junta todos los links de detalle de la página, con el texto
    del contenedor más cercano como título/precio aproximado."""
    props = []
    anchors = await page.eval_on_selector_all(
        "a[href]",
        """els => els.map(a => ({
              href: a.href,
              text: (a.closest('div,article,li') || a).innerText.slice(0, 400)
           }))""",
    )
    seen = set()
    pattern = DETAIL_PATTERNS[portal]
    for a in anchors:
        href = a["href"].split("?")[0]
        if pattern.search(href) and href not in seen:
            seen.add(href)
            texto = a["text"] or ""
            precio_m = re.search(r"(USD|U\$S|\$)\s?[\d.,]+", texto)
            props.append(_prop(
                portal, href,
                titulo=texto.split("\n")[0] if texto else href,
                precio_txt=precio_m.group(0) if precio_m else "",
                zona_texto="",
            ))
    return props


# ─── Portales con Playwright ──────────────────────────────────────────────────

async def scrape_zonaprop(context, search) -> list:
    parsed = search["parsed"]
    tipo = TIPO_PLURAL.get(parsed.get("tipo", "casa"), "propiedades")
    op = parsed.get("operacion", "venta")
    props, page = [], await _new_page(context)
    try:
        for zona in parsed.get("zonas", ["la-plata"])[:5]:
            url = f"https://www.zonaprop.com.ar/{tipo}-{op}-{zona}.html"
            try:
                await _goto(page, url)
                cards = await page.query_selector_all("[data-qa='posting PROPERTY']")
                if cards:
                    for c in cards:
                        link = await c.query_selector("a[href*='/propiedades/']")
                        if not link:
                            continue
                        href = await link.get_attribute("href") or ""
                        if href.startswith("/"):
                            href = "https://www.zonaprop.com.ar" + href
                        titulo_el = await c.query_selector("h2, h3, [data-qa*='TITLE']")
                        precio_el = await c.query_selector("[data-qa*='PRICE'], .price")
                        zona_el = await c.query_selector("[data-qa*='LOCATION'], .location")
                        props.append(_prop(
                            "zonaprop", href,
                            titulo=(await titulo_el.inner_text()) if titulo_el else "",
                            precio_txt=(await precio_el.inner_text()) if precio_el else "",
                            zona_texto=(await zona_el.inner_text()) if zona_el else zona,
                        ))
                else:
                    props += await _generic_sweep(page, "zonaprop", url)
            except Exception as e:
                log.warning("zonaprop %s: %s", zona, e)
    finally:
        await page.close()
    return props


async def scrape_argenprop(context, search) -> list:
    parsed = search["parsed"]
    tipo = TIPO_PLURAL.get(parsed.get("tipo", "casa"), "propiedades")
    op = parsed.get("operacion", "venta")
    props, page = [], await _new_page(context)
    try:
        for zona in parsed.get("zonas", ["la-plata"])[:5]:
            url = f"https://www.argenprop.com/{tipo}/{op}/{zona}"
            try:
                await _goto(page, url)
                cards = await page.query_selector_all(".listing__item, [class*='card-propiedad']")
                if cards:
                    for c in cards:
                        link = await c.query_selector("a[href]")
                        if not link:
                            continue
                        href = await link.get_attribute("href") or ""
                        if href.startswith("/"):
                            href = "https://www.argenprop.com" + href
                        texto = (await c.inner_text())[:400]
                        precio_m = re.search(r"(USD|U\$S|\$)\s?[\d.,]+", texto)
                        props.append(_prop(
                            "argenprop", href,
                            titulo=texto.split("\n")[0],
                            precio_txt=precio_m.group(0) if precio_m else "",
                            zona_texto=zona,
                        ))
                else:
                    props += await _generic_sweep(page, "argenprop", url)
            except Exception as e:
                log.warning("argenprop %s: %s", zona, e)
    finally:
        await page.close()
    return props


async def scrape_remax(context, search) -> list:
    parsed = search["parsed"]
    op = "buy" if parsed.get("operacion", "venta") == "venta" else "rent"
    props, page = [], await _new_page(context)
    try:
        url = (f"https://www.remax.com.ar/listings/{op}"
               f"?page=0&pageSize=48&sort=-createdAt&in:operationId=1"
               f"&locations=la-plata")
        await _goto(page, url)
        props += await _generic_sweep(page, "remax", url)
    except Exception as e:
        log.warning("remax: %s", e)
    finally:
        await page.close()
    return props


async def scrape_century21(context, search) -> list:
    parsed = search["parsed"]
    op = parsed.get("operacion", "venta")
    props, page = [], await _new_page(context)
    try:
        url = f"https://century21.com.ar/propiedades?operacion={op}&localidad=la-plata"
        await _goto(page, url)
        props += await _generic_sweep(page, "century21", url)
    except Exception as e:
        log.warning("century21: %s", e)
    finally:
        await page.close()
    return props


# ─── Portales sin Playwright ──────────────────────────────────────────────────

def scrape_mercadolibre(search) -> list:
    """API oficial — filtro fino lo hace el pre-filtro + la IA."""
    parsed = search["parsed"]
    props = []
    op = "venta" if parsed.get("operacion", "venta") == "venta" else "alquiler"
    for zona in parsed.get("zonas", ["la-plata"])[:5]:
        q = f"{parsed.get('tipo', 'casa')} {op} {zona.replace('-', ' ')}"
        try:
            r = requests.get(
                "https://api.mercadolibre.com/sites/MLA/search",
                params={"category": "MLA1459", "q": q, "limit": 50},
                headers={"User-Agent": UA}, timeout=20,
            )
            for item in r.json().get("results", []):
                props.append({
                    "id": make_id(item.get("permalink", ""), "mercadolibre"),
                    "portal": "mercadolibre",
                    "url": item.get("permalink", ""),
                    "titulo": item.get("title", ""),
                    "precio": int(item.get("price") or 0) or None,
                    "moneda": "usd" if item.get("currency_id") == "USD" else "ars",
                    "zona_texto": (item.get("location", {}) or {}).get("address_line", zona),
                    "descripcion": "",
                    "fecha_detectada": time.strftime("%Y-%m-%d %H:%M"),
                })
        except Exception as e:
            log.warning("mercadolibre %s: %s", zona, e)
    return props


def scrape_inmobusqueda(search) -> list:
    parsed = search["parsed"]
    tipo = parsed.get("tipo", "casa")
    op = parsed.get("operacion", "venta")
    props = []
    for zona in parsed.get("zonas", ["la-plata"])[:5]:
        url = f"https://www.inmobusqueda.com.ar/{tipo}-{op}-{zona}.html"
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if re.search(r"-\d{5,}\.html$", href):
                    if href.startswith("/"):
                        href = "https://www.inmobusqueda.com.ar" + href
                    cont = a.find_parent(["div", "li", "article"])
                    texto = cont.get_text(" ", strip=True)[:400] if cont else a.get_text(strip=True)
                    precio_m = re.search(r"(USD|U\$S|\$)\s?[\d.,]+", texto)
                    props.append(_prop(
                        "inmobusqueda", href,
                        titulo=a.get_text(strip=True) or texto[:80],
                        precio_txt=precio_m.group(0) if precio_m else "",
                        zona_texto=zona,
                    ))
        except Exception as e:
            log.warning("inmobusqueda %s: %s", zona, e)
    # dedup interno
    vistos, unicos = set(), []
    for p in props:
        if p["url"] not in vistos:
            vistos.add(p["url"])
            unicos.append(p)
    return unicos


# ─── Enriquecimiento: descripción completa de la página de detalle ────────────

async def enrich_playwright(context, props: list):
    page = await _new_page(context)
    try:
        for prop in props[:MAX_ENRICH_PER_PORTAL]:
            try:
                await _goto(page, prop["url"])
                texto = await page.evaluate(
                    """() => {
                        const sel = document.querySelector(
                          '#longDescription, [data-qa*="DESCRIPTION"], .description,'
                          + ' #descripcion, [class*="description"], article, main');
                        const src = sel || document.body;
                        return src.innerText;
                    }"""
                )
                prop["descripcion"] = re.sub(r"\n{3,}", "\n\n", texto or "").strip()[:3500]
                if not prop["titulo"]:
                    prop["titulo"] = (await page.title() or "")[:200]
            except Exception as e:
                log.warning("enrich %s: %s", prop["url"], e)
    finally:
        await page.close()


def enrich_requests(props: list):
    for prop in props[:MAX_ENRICH_PER_PORTAL]:
        try:
            r = requests.get(prop["url"], headers={"User-Agent": UA}, timeout=20)
            soup = BeautifulSoup(r.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            cont = soup.select_one(".description, #descripcion, [class*='descripcion'], article, main") or soup.body
            texto = cont.get_text("\n", strip=True) if cont else ""
            prop["descripcion"] = texto[:3500]
        except Exception as e:
            log.warning("enrich %s: %s", prop["url"], e)


# ─── Orquestador ──────────────────────────────────────────────────────────────

async def _scan_async(search: dict, known_ids: set) -> list:
    from playwright.async_api import async_playwright

    todas = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(user_agent=UA, locale="es-AR")
        try:
            for fn in (scrape_zonaprop, scrape_argenprop, scrape_remax, scrape_century21):
                try:
                    encontradas = await fn(context, search)
                    log.info("%s → %s publicaciones", fn.__name__, len(encontradas))
                    todas += encontradas
                except Exception as e:
                    log.error("%s falló: %s", fn.__name__, e)

            # Solo enriquecemos lo NUEVO (ahorra tiempo y tokens)
            nuevas_pw = [p for p in todas if p["id"] not in known_ids]
            if nuevas_pw:
                await enrich_playwright(context, nuevas_pw)
        finally:
            await context.close()
            await browser.close()

    # Portales sin browser
    for fn in (scrape_mercadolibre, scrape_inmobusqueda):
        try:
            encontradas = fn(search)
            log.info("%s → %s publicaciones", fn.__name__, len(encontradas))
            nuevas = [p for p in encontradas if p["id"] not in known_ids]
            if fn is scrape_inmobusqueda:
                enrich_requests(nuevas)
            todas += encontradas
        except Exception as e:
            log.error("%s falló: %s", fn.__name__, e)

    # dedup global por id
    vistos, unicas = set(), []
    for p in todas:
        if p["id"] not in vistos:
            vistos.add(p["id"])
            unicas.append(p)
    return unicas


def run_scan_for_search(search: dict, known_ids: set = None) -> list:
    """Punto de entrada sincrónico (corre en un thread con su propio loop)."""
    return asyncio.run(_scan_async(search, known_ids or set()))
