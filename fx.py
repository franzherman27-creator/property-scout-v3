"""
fx.py — Tipo de cambio ARS/USD para Property Scout v3

Obtiene el dólar blue de dolarapi.com con caché en memoria (TTL 1 hora)
y respaldo en disco (data/fx_cache.json).

Garantías:
  - Si la API falla, usa la última cotización cacheada (aunque sea vieja).
  - Si no hay ningún dato, retorna None y el llamador decide qué hacer
    (el prefilter deja pasar ante la duda; nunca tira el run).
  - Advierte en el log cuando el precio parece absurdo para el tipo de inmueble.
"""

import json
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

_DOLARAPI_URL = "https://dolarapi.com/v1/dolares/blue"
_CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "fx_cache.json")
_CACHE_TTL_S = 3600  # 1 hora en segundos

# Límites sanity para detectar parsing roto en scrapers.
# Propiedades reales en Gran La Plata están en estos rangos.
_SANITY = {
    "usd": (1_000,      20_000_000),   # $1k – $20M USD
    "ars": (100_000, 50_000_000_000),  # $100k – $50B ARS
}

_mem: dict = {}  # {"tasa": float, "ts": float}


# ─── Caché ────────────────────────────────────────────────────────────────────

def _disk_load() -> dict:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _disk_save(tasa: float):
    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    try:
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tasa": tasa, "ts": time.time()}, f)
        os.replace(tmp, _CACHE_FILE)
    except Exception as e:
        log.warning("fx: no se pudo guardar caché en disco: %s", e)


# ─── API pública ──────────────────────────────────────────────────────────────

def get_tasa_blue() -> float | None:
    """Devuelve el precio de venta del dólar blue (ARS por 1 USD).

    Orden de preferencia:
      1) Caché en memoria si tiene menos de 1 hora.
      2) dolarapi.com (actualiza caché en memoria y disco).
      3) Caché en disco (cualquier edad — mejor que nada).
      4) None → el llamador debe operar sin conversión.
    """
    global _mem

    if _mem and time.time() - _mem.get("ts", 0) < _CACHE_TTL_S:
        return _mem["tasa"]

    try:
        r = requests.get(_DOLARAPI_URL, timeout=8,
                         headers={"User-Agent": "property-scout-v3/1.0"})
        if r.status_code == 200:
            data = r.json()
            tasa = float(data.get("venta") or data.get("compra") or 0)
            if tasa > 0:
                _mem = {"tasa": tasa, "ts": time.time()}
                _disk_save(tasa)
                log.info("fx: dólar blue = %.2f ARS/USD (dolarapi.com)", tasa)
                return tasa
            log.warning("fx: dolarapi.com devolvió venta=0")
        else:
            log.warning("fx: dolarapi.com → HTTP %s", r.status_code)
    except Exception as e:
        log.warning("fx: dolarapi.com no disponible (%s)", e)

    # Fallback: caché en disco
    cached = _disk_load()
    if cached.get("tasa"):
        age_h = (time.time() - cached.get("ts", 0)) / 3600
        log.warning("fx: usando tasa cacheada de hace %.1fh (%.2f ARS/USD)", age_h, cached["tasa"])
        _mem = cached
        return cached["tasa"]

    log.error("fx: sin cotización disponible — comparación ARS↔USD desactivada")
    return None


def convert(monto: int | float, de: str, a: str) -> float | None:
    """Convierte monto entre 'usd' y 'ars'.

    Returns None si las monedas son iguales a monto (sin conversión),
    o None si no hay tasa disponible.
    """
    if de == a:
        return float(monto)
    tasa = get_tasa_blue()
    if tasa is None:
        return None
    if de == "usd" and a == "ars":
        return monto * tasa
    if de == "ars" and a == "usd":
        return monto / tasa
    log.warning("fx.convert: monedas desconocidas de=%s a=%s", de, a)
    return None


def warn_if_absurd(precio: int, moneda: str, url: str = "") -> bool:
    """Loguea WARNING si el precio parece absurdo para un inmueble en La Plata.
    Retorna True si el precio es sospechoso (para que el llamador pueda actuar).
    """
    rango = _SANITY.get(moneda)
    if rango is None or precio is None:
        return False
    lo, hi = rango
    if not (lo <= precio <= hi):
        log.warning(
            "precio absurdo detectado: %s %s — posible parsing roto. URL: %s",
            moneda.upper(), precio, url[:100],
        )
        return True
    return False
