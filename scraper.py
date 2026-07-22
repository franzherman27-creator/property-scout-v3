"""
Property Scout v3.3 — Motor de rastreo (8 fuentes · ML API oficial · fixes de calibración)

  MercadoLibre   → HTML SSR de inmuebles.mercadolibre.com.ar (verificado)
  Century 21     → Playwright, URL real /v/resultados/... (verificado)
  Argenprop      → Playwright (funcionando: ~20/scan)
  RE/MAX         → Playwright + retry (era inestable por scans concurrentes)
  Zonaprop       → Playwright + camuflaje; tiene anti-bot duro, puede resistir.
                   Si bloquea, se loguea claramente. El inventario aparece
                   igual vía ML/Argenprop porque las inmobiliarias cross-postean.
  InmoBúsqueda   → requests + BS4

Cambios v3.1: bloqueos detectados y logueados, muestra de links cuando un
portal trae 0 (debug fácil), parse de "US$", scans serializados desde app.py.
"""

import asyncio
import hashlib
import logging
import os
import re
import time

import requests
from bs4 import BeautifulSoup

import fx
import zonas

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Registro central de portales — fuente única de verdad para el frontend.
# "nota" aparece como tooltip en el selector; None = sin nota.
PORTAL_INFO: list[dict] = [
    {"id": "zonaprop",     "nombre": "Zonaprop",     "nota": "Requiere IP residencial para resultados completos"},
    {"id": "argenprop",    "nombre": "Argenprop",     "nota": None},
    {"id": "remax",        "nombre": "RE/MAX",        "nota": None},
    {"id": "century21",    "nombre": "Century 21",    "nota": None},
    {"id": "mercadolibre", "nombre": "MercadoLibre",  "nota": None},
    {"id": "comunidad",    "nombre": "Comunidad",     "nota": None},
    {"id": "eldia",        "nombre": "El Día",        "nota": None},
    {"id": "inmobusqueda", "nombre": "InmoBúsqueda",  "nota": "Datos de ingesta local si se configuró zonaprop_local"},
]
PORTALES: list[str] = [p["id"] for p in PORTAL_INFO]

MAX_ENRICH_PER_PORTAL = 20
MAX_CARDS_PER_PORTAL = 60
PAGE_TIMEOUT_MS = 30000

DETAIL_PATTERNS = {
    "zonaprop":  re.compile(r"zonaprop\.com\.ar/propiedades/.+\.html"),
    "argenprop": re.compile(r"argenprop\.com/.+--\d{6,}"),
    "remax":     re.compile(r"remax\.com\.ar/listings/[^?#]+-[^?#]+"),
    "century21": re.compile(r"century21\.com\.ar/[^\s\"?#]*propiedad[^\s\"?#]*"),
    "mercadolibre": re.compile(r"mercadolibre\.com\.ar.*MLA-?\d{6,}"),
    "comunidad": re.compile(r"comunidadinmobiliaria\.com\.ar/propiedad/[^?#]+"),
}

TIPO_PLURAL = {
    "casa": "casas", "departamento": "departamentos", "ph": "ph",
    "lote": "terrenos", "local": "locales-comerciales", "quinta": "quintas",
    "otro": "inmuebles",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_id(url: str, portal: str) -> str:
    return hashlib.md5(f"{portal}-{url}".encode()).hexdigest()[:12]


def parse_precio(texto: str):
    """'USD 85.000' / 'U$S' / 'US$155.000' / '$ 120.000.000' → (monto, moneda).

    Casos borde:
    - Texto vacío / None           → (None, None)
    - "Consultar" / "Consultar precio" → (None, None)  [sin precio publicado]
    - Precio = 0 tras parseo       → (None, moneda)    [tratado como sin precio]
    - Sin dígitos                  → (None, moneda)
    """
    if not texto:
        return None, None
    t = texto.upper().replace("\xa0", " ").strip()
    # "Consultar" y variantes nunca tienen precio real
    if re.match(r"^CONSULTAR", t) or t in ("CONSULTAR PRECIO", "PRECIO A CONSULTAR", "A CONSULTAR"):
        return None, None
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
        valor = int(monto)
        # 0 no es un precio válido para un inmueble
        return (valor if valor > 0 else None), moneda
    except ValueError:
        return None, moneda


def _prop(portal, url, titulo="", precio_txt="", zona_texto="", imagen=None):
    precio, moneda = parse_precio(precio_txt)
    if precio is not None and moneda is not None:
        fx.warn_if_absurd(precio, moneda, url)
    titulo_clean = (titulo or "").strip()[:200]
    zona_clean = (zona_texto or "").strip()[:200]
    return {
        "id": make_id(url, portal),
        "portal": portal,
        "url": url,
        "titulo": titulo_clean,
        "precio": precio,
        "moneda": moneda,
        "zona_texto": zona_clean,
        "zona_canonica": zonas.normalizar(zona_clean, titulo_clean),
        "descripcion": "",
        "imagen": imagen or None,
        "fecha_detectada": time.strftime("%Y-%m-%d %H:%M"),
    }


def _dedup(props):
    vistos, unicos = set(), []
    for p in props:
        if p["id"] not in vistos:
            vistos.add(p["id"])
            unicos.append(p)
    return unicos[:MAX_CARDS_PER_PORTAL * 6]


# ─── Playwright helpers ───────────────────────────────────────────────────────

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['es-AR','es','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = {runtime: {}};
"""


async def _new_page(context):
    page = await context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    return page


async def _goto(page, url, wait_ms=4000):
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(wait_ms)


async def _check_blocked(page, portal) -> bool:
    """Detecta páginas de challenge anti-bot y lo deja claro en el log."""
    try:
        title = (await page.title() or "").lower()
        body_len = await page.evaluate("() => document.body.innerText.length")
        markers = ("denied", "captcha", "robot", "blocked", "verify", "attention")
        if any(m in title for m in markers) or body_len < 800:
            log.warning("%s: ⚠ posible bloqueo anti-bot (title='%s', body=%s chars)",
                        portal, title[:60], body_len)
            return True
    except Exception:
        pass
    return False


async def _sweep_links(page, portal: str) -> list:
    """Junta links de detalle + texto + imagen del contenedor. Si trae 0,
    loguea una muestra de hrefs para calibrar el patrón sin adivinar."""
    anchors = await page.eval_on_selector_all(
        "a[href]",
        """els => els.map(a => {
              const cont = a.closest('div,article,li,section') || a;
              const img = cont.querySelector('img[data-src], img[src]:not([src^="data:"])') || cont.querySelector('img');
              let imagen = '';
              if (img) {
                imagen = img.getAttribute('data-src')
                      || img.getAttribute('data-lazy-src')
                      || img.getAttribute('data-original')
                      || (img.src && !img.src.startsWith('data:') ? img.src : '')
                      || '';
              }
              return { href: a.href, text: cont.innerText.slice(0, 400), imagen };
           })""",
    )
    props, seen = [], set()
    pattern = DETAIL_PATTERNS[portal]
    for a in anchors:
        href = a["href"].split("?")[0].split("#")[0]
        if pattern.search(href) and href not in seen:
            seen.add(href)
            texto = a["text"] or ""
            precio_m = re.search(r"(USD|US\$|U\$S|\$)\s?[\d.,]+", texto)
            props.append(_prop(
                portal, href,
                titulo=texto.split("\n")[0][:150] if texto else href,
                precio_txt=precio_m.group(0) if precio_m else "",
                imagen=a.get("imagen") or None,
            ))
        if len(props) >= MAX_CARDS_PER_PORTAL:
            break
    if not props and anchors:
        sample = [a["href"] for a in anchors if "/" in a["href"]][:5]
        log.info("%s: 0 links matchearon el patrón. Muestra de hrefs: %s", portal, sample)
    return props


# ─── Portales con Playwright ──────────────────────────────────────────────────

async def scrape_zonaprop(context, search) -> list:
    parsed = search["parsed"]
    tipo = TIPO_PLURAL.get(parsed.get("tipo", "casa"), "inmuebles")
    op = parsed.get("operacion", "venta")
    props, page = [], await _new_page(context)
    try:
        for zona in parsed.get("zonas", ["la-plata"])[:4]:
            url = f"https://www.zonaprop.com.ar/{tipo}-{op}-{zona}.html"
            try:
                await _goto(page, url, wait_ms=6000)
                if await _check_blocked(page, "zonaprop"):
                    break  # bloqueado: no insistir en más zonas este scan
                props += await _sweep_links(page, "zonaprop")
            except Exception as e:
                log.warning("zonaprop %s: %s", zona, e)
    finally:
        await page.close()
    return _dedup(props)


async def scrape_argenprop(context, search) -> list:
    parsed = search["parsed"]
    tipo = TIPO_PLURAL.get(parsed.get("tipo", "casa"), "inmuebles")
    op = parsed.get("operacion", "venta")
    props, page = [], await _new_page(context)
    try:
        for zona in parsed.get("zonas", ["la-plata"])[:4]:
            url = f"https://www.argenprop.com/{tipo}/{op}/{zona}"
            try:
                await _goto(page, url)
                props += await _sweep_links(page, "argenprop")
            except Exception as e:
                log.warning("argenprop %s: %s", zona, e)
    finally:
        await page.close()
    return _dedup(props)


async def scrape_remax(context, search) -> list:
    parsed = search["parsed"]
    op = "buy" if parsed.get("operacion", "venta") == "venta" else "rent"
    props, page = [], await _new_page(context)
    try:
        url = (f"https://www.remax.com.ar/listings/{op}"
               f"?page=0&pageSize=48&sort=-createdAt&locations=la-plata")
        for intento in (1, 2):
            await _goto(page, url, wait_ms=6000)
            props = await _sweep_links(page, "remax")
            if len(props) >= 3:
                break
            log.info("remax: %s resultados en intento %s — reintento", len(props), intento)
            await page.wait_for_timeout(8000)
    except Exception as e:
        log.warning("remax: %s", e)
    finally:
        await page.close()
    return _dedup(props)


async def scrape_century21(context, search) -> list:
    """URL real verificada:
    /v/resultados/en-pais_argentina/en-estado_gba-sur/en-municipio_gba-sur-la-plata/tipo_X/operacion_Y
    El partido La Plata incluye todas las localidades (Tolosa, Gonnet, City Bell...).
    En C21 alquiler = 'renta'."""
    parsed = search["parsed"]
    tipo = parsed.get("tipo", "casa")
    if tipo not in ("casa", "departamento", "ph", "terreno", "local", "quinta"):
        tipo = "casa"
    if tipo == "lote":
        tipo = "terreno"
    op = "venta" if parsed.get("operacion", "venta") == "venta" else "renta"
    props, page = [], await _new_page(context)
    try:
        url = (f"https://century21.com.ar/v/resultados/en-pais_argentina"
               f"/en-estado_gba-sur/en-municipio_gba-sur-la-plata"
               f"/tipo_{tipo}/operacion_{op}")
        await _goto(page, url, wait_ms=7000)  # SPA: tarda en renderizar
        props = await _sweep_links(page, "century21")
    except Exception as e:
        log.warning("century21: %s", e)
    finally:
        await page.close()
    return _dedup(props)


# ─── Portales sin Playwright ──────────────────────────────────────────────────

# ─── MercadoLibre: API oficial con credenciales de aplicación ─────────────────

_ML_TOKEN = {"token": None, "expires": 0}


def _ml_token():
    """Token de aplicación (client_credentials). Se renueva solo antes de vencer.
    Requiere ML_APP_ID y ML_APP_SECRET en las variables de entorno."""
    app_id = os.environ.get("ML_APP_ID")
    secret = os.environ.get("ML_APP_SECRET")
    if not (app_id and secret):
        return None
    if _ML_TOKEN["token"] and time.time() < _ML_TOKEN["expires"] - 300:
        return _ML_TOKEN["token"]
    try:
        r = requests.post(
            "https://api.mercadolibre.com/oauth/token",
            data={"grant_type": "client_credentials",
                  "client_id": app_id, "client_secret": secret},
            headers={"Accept": "application/json"}, timeout=20,
        )
        if r.status_code == 200:
            d = r.json()
            _ML_TOKEN["token"] = d["access_token"]
            _ML_TOKEN["expires"] = time.time() + d.get("expires_in", 21600)
            log.info("mercadolibre: token de API obtenido OK")
            return _ML_TOKEN["token"]
        log.warning("mercadolibre: token rechazado (%s): %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("mercadolibre: error pidiendo token: %s", e)
    return None


def scrape_mercadolibre(search) -> list:
    """API oficial si hay credenciales (ML_APP_ID + ML_APP_SECRET);
    si no, intento por HTML (suele estar bloqueado desde IPs de datacenter)."""
    parsed = search["parsed"]
    op = parsed.get("operacion", "venta")
    tipo = parsed.get("tipo", "casa")
    props = []

    token = _ml_token()
    if token:
        try:
            r = requests.get(
                "https://api.mercadolibre.com/sites/MLA/search",
                params={"category": "MLA1459",
                        "q": f"{tipo} {op} la plata", "limit": 50},
                headers={"Authorization": f"Bearer {token}",
                         "User-Agent": UA}, timeout=25,
            )
            if r.status_code == 200:
                for item in r.json().get("results", []):
                    url = item.get("permalink", "")
                    if not url:
                        continue
                    precio_raw = int(item.get("price") or 0) or None
                    moneda_ml = "usd" if item.get("currency_id") == "USD" else "ars"
                    if precio_raw is not None:
                        fx.warn_if_absurd(precio_raw, moneda_ml, url)
                    titulo_ml = item.get("title", "")[:200]
                    zona_ml = (item.get("location", {}) or {}).get("address_line", "")[:200]
                    thumb = item.get("thumbnail") or None
                    props.append({
                        "id": make_id(url, "mercadolibre"),
                        "portal": "mercadolibre",
                        "url": url,
                        "titulo": titulo_ml,
                        "precio": precio_raw,
                        "moneda": moneda_ml,
                        "zona_texto": zona_ml,
                        "zona_canonica": zonas.normalizar(zona_ml, titulo_ml),
                        "descripcion": "",
                        "imagen": thumb,
                        "fecha_detectada": time.strftime("%Y-%m-%d %H:%M"),
                    })
                return _dedup(props)
            log.warning("mercadolibre API: %s — %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("mercadolibre API: %s", e)
    else:
        log.info("mercadolibre: sin credenciales — configurá ML_APP_ID y "
                 "ML_APP_SECRET en Railway para activar la API oficial")

    # Fallback HTML (mejor que nada; bloqueado con frecuencia desde datacenter)
    tipo_pl = TIPO_PLURAL.get(tipo, "inmuebles")
    url = f"https://inmuebles.mercadolibre.com.ar/{tipo_pl}/{op}/bsas-gba-sur/la-plata/"
    try:
        r = requests.get(url, headers={
            "User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9",
        }, timeout=25)
        soup = BeautifulSoup(r.text, "lxml")
        pattern = DETAIL_PATTERNS["mercadolibre"]
        seen = set()
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").split("?")[0].split("#")[0]
            if not pattern.search(href) or href in seen:
                continue
            seen.add(href)
            cont = a.find_parent(["li", "div", "article"]) or a
            texto = cont.get_text(" · ", strip=True)[:450]
            precio_m = re.search(r"(US\$|USD|U\$S|\$)\s?[\d.,]+", texto)
            img_tag = cont.find("img")
            imagen = None
            if img_tag:
                imagen = (img_tag.get("data-src") or img_tag.get("src") or None)
                if imagen and imagen.startswith("data:"):
                    imagen = None
            props.append(_prop(
                "mercadolibre", href,
                titulo=a.get_text(strip=True) or texto[:100],
                precio_txt=precio_m.group(0) if precio_m else "",
                imagen=imagen,
            ))
            if len(props) >= MAX_CARDS_PER_PORTAL:
                break
        if not props:
            log.info("mercadolibre HTML: 0 links (probable muro de login/robot)")
    except Exception as e:
        log.warning("mercadolibre HTML: %s", e)
    return _dedup(props)


def scrape_inmobusqueda(search) -> list:
    """URLs verificadas: {tipo}-venta-la-plata-casco-urbano.html cubre el casco,
    {tipo}-venta-partido-la-plata.html cubre Tolosa, Gonnet, City Bell, Villa
    Elisa y el resto de las localidades del partido."""
    parsed = search["parsed"]
    tipo = parsed.get("tipo", "casa")
    op = parsed.get("operacion", "venta")
    urls = [
        f"https://www.inmobusqueda.com.ar/{tipo}-{op}-la-plata-casco-urbano.html",
        f"https://www.inmobusqueda.com.ar/{tipo}-{op}-partido-la-plata.html",
    ]
    props = []
    for url in urls:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if "pagina-" in href:
                    continue
                if re.search(r"-\d{4,}\.html$", href):
                    if href.startswith("/"):
                        href = "https://www.inmobusqueda.com.ar" + href
                    cont = a.find_parent(["div", "li", "article"])
                    texto = cont.get_text(" ", strip=True)[:400] if cont else a.get_text(strip=True)
                    precio_m = re.search(r"(USD|US\$|U\$S|\$)\s?[\d.,]+", texto)
                    img_tag = cont.find("img") if cont else None
                    imagen = None
                    if img_tag:
                        imagen = (img_tag.get("data-src") or img_tag.get("src") or None)
                        if imagen and imagen.startswith("data:"):
                            imagen = None
                    props.append(_prop(
                        "inmobusqueda", href,
                        titulo=a.get_text(strip=True) or texto[:80],
                        precio_txt=precio_m.group(0) if precio_m else "",
                        imagen=imagen,
                    ))
        except Exception as e:
            log.warning("inmobusqueda %s: %s", url[:70], e)
    if not props:
        log.info("inmobusqueda: 0 links con patrón -NNNN.html — revisar estructura")
    return _dedup(props)


def scrape_comunidad(search) -> list:
    """La Comunidad Inmobiliaria (portal del círculo de martilleros de La Plata).
    URL verificada: /propiedades/ventas/la-plata/ — detalle en /propiedad/...
    Las fichas de detalle traen 'Apto Crédito: Sí/No' explícito."""
    parsed = search["parsed"]
    op = "ventas" if parsed.get("operacion", "venta") == "venta" else "alquileres"
    base = f"https://comunidadinmobiliaria.com.ar/propiedades/{op}/la-plata/"
    tp = "?tp=1" if parsed.get("tipo", "casa") == "casa" else ""
    props, pattern = [], DETAIL_PATTERNS["comunidad"]
    seen = set()
    # Página 1 + intento de paginación (si el sitio ignora el parámetro,
    # el dedup absorbe los repetidos sin problema)
    urls = [base + tp]
    sep = "&" if tp else "?"
    urls += [f"{base}{tp}{sep}page={n}" for n in (2, 3)]
    for url in urls:
        try:
            r = requests.get(url, headers={
                "User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9",
            }, timeout=20)
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href]"):
                href = (a.get("href") or "").split("?")[0].split("#")[0]
                if href.startswith("/propiedad/"):
                    href = "https://comunidadinmobiliaria.com.ar" + href
                if not pattern.search(href) or href in seen:
                    continue
                seen.add(href)
                cont = a.find_parent(["div", "li", "article"]) or a
                texto = cont.get_text(" · ", strip=True)[:400]
                precio_m = re.search(r"(USD|US\$|U\$S|\$)\s?[\d.,]+", texto)
                img_tag = cont.find("img")
                imagen = None
                if img_tag:
                    imagen = (img_tag.get("data-src") or img_tag.get("src") or None)
                    if imagen and imagen.startswith("data:"):
                        imagen = None
                props.append(_prop(
                    "comunidad", href,
                    titulo=a.get_text(strip=True) or texto[:100],
                    precio_txt=precio_m.group(0) if precio_m else "",
                    zona_texto="",
                    imagen=imagen,
                ))
                if len(props) >= MAX_CARDS_PER_PORTAL:
                    break
        except Exception as e:
            log.warning("comunidad %s: %s", url[:80], e)
        if len(props) >= MAX_CARDS_PER_PORTAL:
            break
    if not props:
        log.info("comunidad: 0 links matchearon el patrón /propiedad/")
    return _dedup(props)


# Categorías de clasificados de El Día por zona pedida
ELDIA_ZONA_CATS = {
    "tolosa": "clasificados-compra-venta-gonnet-ringuelet-tolosa",
    "ringuelet": "clasificados-compra-venta-gonnet-ringuelet-tolosa",
    "gonnet": "clasificados-compra-venta-gonnet-ringuelet-tolosa",
    "city-bell": "clasificados-compra-venta-city-bell",
    "villa-elisa": "clasificados-compra-venta-villa-elisa",
    "berisso": "clasificados-compra-venta-casas-departamentos-locales-salon-comercial-lotes-berisso-ensenada",
    "ensenada": "clasificados-compra-venta-casas-departamentos-locales-salon-comercial-lotes-berisso-ensenada",
}


def scrape_eldia(search) -> list:
    """Clasificados del diario El Día (clasificados.eldia.com).
    Avisos de texto estilo diario: el texto del aviso ES la descripción,
    así que no necesitan enriquecimiento aparte."""
    parsed = search["parsed"]
    tipo = parsed.get("tipo", "casa")
    cats = set()
    if tipo == "casa":
        cats.add("clasificados-compra-venta-casas-la-plata")
    elif tipo == "departamento":
        cats.add("clasificados-compra-venta-departamentos-La-Plata")
    elif tipo == "lote":
        cats.add("clasificados-compra-venta-lotes-terrenos-La-Plata")
    else:
        cats.add("clasificados-compra-venta-casas-la-plata")
    for zona in parsed.get("zonas", []):
        if zona in ELDIA_ZONA_CATS:
            cats.add(ELDIA_ZONA_CATS[zona])

    props = []
    for cat in cats:
        url = f"https://clasificados.eldia.com/{cat}"
        try:
            r = requests.get(url, headers={
                "User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9",
            }, timeout=20)
            soup = BeautifulSoup(r.text, "lxml")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()

            # 1) Avisos con link a la ficha en viviendas.eldia.com
            con_link = set()
            for a in soup.select("a[href*='viviendas.eldia.com']"):
                href = (a.get("href") or "").split("#")[0]
                if not href or href in con_link:
                    continue
                con_link.add(href)
                cont = a.find_parent(["div", "td", "li", "article"]) or a
                texto = cont.get_text(" ", strip=True)[:600]
                if len(texto) < 40:
                    continue
                precio_m = re.search(r"(USD|US\$|U\$S|\$)\s?[\d.,]+", texto)
                p = _prop("eldia", href, titulo=texto[:90],
                          precio_txt=precio_m.group(0) if precio_m else "")
                p["descripcion"] = texto
                props.append(p)

            # 2) Avisos de solo texto (sin link propio): bloques con pinta de
            # aviso — largo razonable y con precio o teléfono
            for el in soup.find_all(["p", "div", "td", "li"]):
                texto = el.get_text(" ", strip=True)
                if not (60 <= len(texto) <= 650):
                    continue
                tiene_precio = re.search(r"(USD|US\$|U\$S|\$)\s?[\d.,]{4,}", texto)
                tiene_tel = re.search(r"\b\d{3,4}[-\s]?\d{6,7}\b", texto)
                if not (tiene_precio or tiene_tel):
                    continue
                if el.find(["p", "div", "li"]):  # quedarse con el bloque hoja
                    continue
                aviso_id = hashlib.md5(texto.encode()).hexdigest()[:10]
                p = _prop("eldia", f"{url}#aviso-{aviso_id}", titulo=texto[:90],
                          precio_txt=tiene_precio.group(0) if tiene_precio else "")
                p["descripcion"] = texto
                props.append(p)
                if len(props) >= MAX_CARDS_PER_PORTAL:
                    break
        except Exception as e:
            log.warning("eldia %s: %s", cat, e)
    if not props:
        log.info("eldia: 0 avisos extraídos — revisar estructura de la página")
    return _dedup(props)


# ─── Enriquecimiento ──────────────────────────────────────────────────────────

async def enrich_playwright(context, props: list):
    page = await _new_page(context)
    try:
        for prop in props[:MAX_ENRICH_PER_PORTAL]:
            try:
                await _goto(page, prop["url"], wait_ms=3000)
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
                if not prop["titulo"]:
                    prop["titulo"] = (await page.title() or "")[:200]
                if not prop.get("imagen"):
                    og = await page.evaluate(
                        """() => {
                            const m = document.querySelector('meta[property="og:image"]');
                            return m ? m.getAttribute('content') : '';
                        }"""
                    )
                    if og and not og.startswith("data:"):
                        prop["imagen"] = og
            except Exception as e:
                log.warning("enrich %s: %s", prop["url"][:80], e)
    finally:
        await page.close()


def enrich_requests(props: list):
    for prop in props[:MAX_ENRICH_PER_PORTAL]:
        try:
            r = requests.get(prop["url"], headers={
                "User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9",
            }, timeout=20)
            soup = BeautifulSoup(r.text, "lxml")
            if not prop.get("imagen"):
                og = soup.find("meta", {"property": "og:image"})
                if og:
                    src = og.get("content") or ""
                    if src and not src.startswith("data:"):
                        prop["imagen"] = src
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            cont = soup.select_one(
                ".ui-pdp-description, .description, #descripcion,"
                " [class*='descripcion'], article, main") or soup.body
            texto = cont.get_text("\n", strip=True) if cont else ""
            prop["descripcion"] = texto[:3500]
        except Exception as e:
            log.warning("enrich %s: %s", prop["url"][:80], e)


# ─── Orquestador ──────────────────────────────────────────────────────────────

# Dicts nombre→función para filtrar fácilmente por portal seleccionado.
# Se definen aquí (después de las funciones) para que las referencias existan.
_SCRAPERS_STATIC: dict = {}   # poblado al final del módulo
_SCRAPERS_PLAYWRIGHT: dict = {}


async def _scan_async(search: dict, known_ids: set,
                      portales: set | None = None) -> list:
    """portales=None → todos los portales; portales=set → solo esos."""
    from playwright.async_api import async_playwright

    todas = []

    # 1) Portales sin browser primero: rápidos y dan señal inmediata
    for nombre, fn in _SCRAPERS_STATIC.items():
        if portales is not None and nombre not in portales:
            continue
        try:
            encontradas = fn(search)
            log.info("%s → %s publicaciones", nombre, len(encontradas))
            # El Día ya trae el texto completo del aviso; el resto se enriquece
            nuevas = [p for p in encontradas
                      if p["id"] not in known_ids and p["portal"] != "eldia"]
            try:
                enrich_requests(nuevas)
            except Exception as e:
                log.warning("enrich estático: %s", e)
            todas += encontradas
        except Exception as e:
            log.error("%s falló: %s", nombre, e)

    # 2) Portales con browser (solo si al menos uno fue seleccionado)
    playwright_activos = {
        nombre: fn for nombre, fn in _SCRAPERS_PLAYWRIGHT.items()
        if portales is None or nombre in portales
    }
    if not playwright_activos:
        return _dedup(todas)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=UA, locale="es-AR",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "es-AR,es;q=0.9,en;q=0.7"},
        )
        await context.add_init_script(STEALTH_JS)
        try:
            for nombre, fn in playwright_activos.items():
                try:
                    encontradas = await fn(context, search)
                    log.info("%s → %s publicaciones", nombre, len(encontradas))
                    todas += encontradas
                except Exception as e:
                    log.error("%s falló: %s", nombre, e)

            nuevas_pw = [p for p in todas if p["id"] not in known_ids
                         and p["portal"] in playwright_activos]
            if nuevas_pw:
                log.info("Enriqueciendo %s publicaciones nuevas (browser)…",
                         min(len(nuevas_pw), MAX_ENRICH_PER_PORTAL))
                try:
                    await enrich_playwright(context, nuevas_pw)
                except Exception as e:
                    log.warning("enrich browser: %s", e)
        finally:
            await context.close()
            await browser.close()

    return _dedup(todas)


def run_scan_for_search(search: dict, known_ids: set = None) -> list:
    portales = search.get("portales")
    portales_set = set(portales) if portales else None
    return asyncio.run(_scan_async(search, known_ids or set(), portales_set))


# Poblar los dicts de scrapers (después de que las funciones estén definidas)
_SCRAPERS_STATIC.update({
    "mercadolibre": scrape_mercadolibre,
    "comunidad":    scrape_comunidad,
    "eldia":        scrape_eldia,
    "inmobusqueda": scrape_inmobusqueda,
})
_SCRAPERS_PLAYWRIGHT.update({
    "argenprop": scrape_argenprop,
    "remax":     scrape_remax,
    "century21": scrape_century21,
    "zonaprop":  scrape_zonaprop,
})
