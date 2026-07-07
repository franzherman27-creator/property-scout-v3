"""
AI Matcher — Property Scout v3
Capa de inteligencia del agente.

- parse_pedido()   → convierte un pedido en lenguaje natural a criterios estructurados
- score_property() → evalúa una propiedad contra el pedido como lo haría un martillero
- score_batch()    → evalúa en paralelo (solo propiedades nuevas)

Requiere la variable de entorno ANTHROPIC_API_KEY.
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Sonnet entiende el pedido (1 llamada por búsqueda creada).
# Haiku evalúa cada propiedad (rápido y muy barato, corre en volumen).
MODEL_PARSER = "claude-sonnet-4-6"
MODEL_SCORER = "claude-haiku-4-5-20251001"

MAX_WORKERS = 4
MAX_RETRIES = 3


# ─── Llamada base a la API ────────────────────────────────────────────────────

def _call_claude(model: str, system: str, user_content: str, max_tokens: int = 1200) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("Falta ANTHROPIC_API_KEY en las variables de entorno.")

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "temperature": 0,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=60)
            if r.status_code == 200:
                data = r.json()
                return "".join(
                    b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
                )
            if r.status_code in (429, 500, 502, 503, 529):
                wait = 2 ** attempt
                log.warning("API %s — reintento en %ss (intento %s)", r.status_code, wait, attempt)
                time.sleep(wait)
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                continue
            raise RuntimeError(f"API error {r.status_code}: {r.text[:300]}")
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(2 ** attempt)

    raise RuntimeError(f"API sin respuesta tras {MAX_RETRIES} intentos: {last_err}")


def _extract_json(text: str) -> dict:
    """Extrae el primer objeto JSON del texto, tolerando fences de markdown."""
    clean = re.sub(r"```(?:json)?", "", text).strip()
    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Respuesta sin JSON: {text[:200]}")
    return json.loads(clean[start : end + 1])


# ─── 1) Parsear el pedido ─────────────────────────────────────────────────────

PARSE_SYSTEM = """Sos el analista de una inmobiliaria de La Plata, Argentina. \
Convertís pedidos de búsqueda de propiedades escritos en lenguaje natural \
(tal cual los escribe un cliente por WhatsApp) a JSON estructurado.

Respondé SOLO el JSON, sin markdown ni texto adicional.

Esquema:
{
  "titulo": "resumen corto del pedido (máx 8 palabras)",
  "operacion": "venta" | "alquiler",
  "tipo": "casa" | "departamento" | "ph" | "lote" | "local" | "quinta" | "otro",
  "zonas": ["slugs de zonas"],
  "precio_max": number | null,
  "precio_min": number | null,
  "moneda": "usd" | "ars",
  "dormitorios_min": number | null,
  "requisitos_duros": ["condiciones sin las cuales NO sirve"],
  "deseables": ["condiciones que suman pero no excluyen"],
  "notas": "contexto adicional relevante en una frase"
}

Conocimiento local (Gran La Plata):
- "zona norte" = tolosa, ringuelet, gonnet, city-bell, villa-elisa, arturo-segui
- Slugs válidos: la-plata, tolosa, ringuelet, gonnet, city-bell, villa-elisa,
  arturo-segui, los-hornos, san-carlos, altos-de-san-lorenzo, villa-elvira,
  berisso, ensenada. "Casco urbano" o "el centro" = la-plata.
- Si no aclara operación, asumí venta.
- Si no aclara moneda: venta → usd; alquiler → ars.
- "Apto crédito", "apto banco", "escritura", "cochera": son requisitos duros
  si el cliente los menciona como condición.
- Distinguí bien: "con parque" suele ser deseable salvo que lo pida como
  condición explícita ("sí o sí con parque")."""


def parse_pedido(pedido_raw: str) -> dict:
    text = _call_claude(MODEL_PARSER, PARSE_SYSTEM, f"Pedido del cliente:\n{pedido_raw}")
    parsed = _extract_json(text)
    parsed.setdefault("zonas", ["la-plata"])
    parsed.setdefault("requisitos_duros", [])
    parsed.setdefault("deseables", [])
    parsed.setdefault("operacion", "venta")
    parsed.setdefault("moneda", "usd")
    return parsed


# ─── 2) Pre-filtro barato (sin IA) ────────────────────────────────────────────

def prefilter(search: dict, prop: dict) -> bool:
    """Descarta lo obviamente fuera de rango antes de gastar tokens.
    Ante la duda (datos faltantes), deja pasar: la IA decide."""
    parsed = search.get("parsed", {})
    precio_max = parsed.get("precio_max")
    moneda = parsed.get("moneda")

    p_precio, p_moneda = prop.get("precio"), prop.get("moneda")
    if precio_max and p_precio and p_moneda == moneda:
        if p_precio > precio_max * 1.08:  # 8% de tolerancia: a veces se negocia
            return False
    return True


# ─── 3) Evaluar cada propiedad ────────────────────────────────────────────────

SCORE_SYSTEM = """Sos un martillero senior de La Plata evaluando si una propiedad \
publicada matchea EXACTAMENTE el pedido de un cliente. Sos estricto: tu \
reputación depende de no mandar fruta, pero tampoco de descartar oportunidades \
reales por falta de un dato en la publicación.

Respondé SOLO JSON:
{
  "score": 0-100,
  "veredicto": "match" | "revisar" | "descartar",
  "razones": ["máx 4 puntos concretos de por qué sí / por qué no"],
  "faltantes": ["requisitos duros que la publicación no confirma ni niega"],
  "alertas": ["red flags: precio raro, datos inconsistentes, posible error"]
}

Reglas:
- Requisito duro explícitamente ausente o contradicho → "descartar", score ≤ 30.
- Requisito duro NO mencionado en la publicación → NO descartar: veredicto
  "revisar", score ≤ 75, y listalo en "faltantes" (ej: "apto crédito" casi
  nunca se publica, se confirma por teléfono).
- "match" solo si cumple todos los duros confirmados y la mayoría de los
  deseables → score ≥ 80.
- Los deseables suman score pero nunca descartan por sí solos.
- Zona: si la publicación es de una localidad claramente fuera de las zonas
  pedidas, descartá. Si es limítrofe o ambigua, "revisar".
- No inventes datos que la publicación no dice."""


def _prop_ficha(prop: dict) -> str:
    partes = [
        f"Portal: {prop.get('portal', '?')}",
        f"Título: {prop.get('titulo', '')}",
        f"Precio: {(prop.get('moneda') or '?').upper()} {prop.get('precio') or 'sin publicar'}",
        f"Zona / dirección: {prop.get('zona_texto', 'sin dato')}",
        f"URL: {prop.get('url', '')}",
    ]
    desc = (prop.get("descripcion") or "").strip()
    if desc:
        partes.append(f"Descripción:\n{desc[:2500]}")
    else:
        partes.append("Descripción: (la publicación no incluye texto descriptivo)")
    return "\n".join(partes)


def score_property(search: dict, prop: dict) -> dict:
    user = (
        f"PEDIDO DEL CLIENTE (textual):\n{search.get('pedido_raw', '')}\n\n"
        f"PEDIDO ESTRUCTURADO:\n{json.dumps(search.get('parsed', {}), ensure_ascii=False)}\n\n"
        f"PUBLICACIÓN A EVALUAR:\n{_prop_ficha(prop)}"
    )
    try:
        result = _extract_json(_call_claude(MODEL_SCORER, SCORE_SYSTEM, user, max_tokens=700))
        result.setdefault("score", 0)
        result.setdefault("veredicto", "revisar")
        result.setdefault("razones", [])
        result.setdefault("faltantes", [])
        result.setdefault("alertas", [])
        return result
    except Exception as e:
        log.error("Error evaluando %s: %s", prop.get("url"), e)
        return {
            "score": 0,
            "veredicto": "revisar",
            "razones": ["No se pudo evaluar automáticamente — revisar a mano"],
            "faltantes": [],
            "alertas": [f"error_evaluacion: {str(e)[:120]}"],
        }


def score_batch(search: dict, props: list) -> dict:
    """Evalúa una lista de propiedades en paralelo. Devuelve {prop_id: resultado}."""
    resultados = {}
    if not props:
        return resultados
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(score_property, search, p): p for p in props}
        for fut in as_completed(futures):
            prop = futures[fut]
            resultados[prop["id"]] = fut.result()
    return resultados
