"""
zonas.py — Canonicalización de zonas del Gran La Plata

Problema que resuelve
─────────────────────
Los portales escriben la misma localidad de formas muy distintas:
  "Manuel B. Gonnet", "M.B. Gonnet", "M B Gonnet" → slug "gonnet"

Además, portales que scrapeamos en bloque (Comunidad Inmobiliaria,
InmoBúsqueda) devuelven propiedades de TODO el partido de La Plata.
Sin este módulo el prefilter no puede rechazar "Sicardi" cuando el
usuario buscó "Gonnet".

Diseño de matching
──────────────────
- Matching por tokens (palabras completas). "gonnet" NO coincide dentro
  de otro token — no hay substring suelto.
- "La Plata" aparece en casi todo texto de dirección (ciudad del partido).
  Se le da prioridad mínima: cualquier zona más específica gana.
  Ejemplo: "Gonnet, La Plata" → slug "gonnet", no "la-plata".
- Texto sin zona reconocida → SIN_ZONA ("sin_zona") + WARNING en el log
  para ir alimentando el diccionario de aliases.
- en_busqueda() es conservador: SIN_ZONA/None siempre deja pasar (la IA
  decide); sólo rechaza cuando la zona es conocida y distinta a la pedida.
"""

import logging
import re
import unicodedata

log = logging.getLogger(__name__)

# ─── Diccionario canónico ─────────────────────────────────────────────────────
# Clave  : slug (idéntico al que genera ai_matcher.parse_pedido).
# Valor  : lista de aliases tal como aparecen en los portales.
#          La normalización quita tildes, puntuación y unifica mayúsculas;
#          los aliases se escriben aquí en texto libre para facilitar el mantenimiento.

ZONAS_CANONICAS: dict[str, list[str]] = {
    "la-plata": [
        "la plata",
        "la plata capital",
        "casco urbano",
        "el casco",
        "casco",
        "el centro",
        "la plata ciudad",
        "city of la plata",      # ML en inglés a veces
    ],
    "gonnet": [
        "gonnet",
        "m b gonnet",
        "m. b. gonnet",
        "m.b. gonnet",
        "mb gonnet",
        "manuel b gonnet",
        "manuel b. gonnet",
        "manuel belgrano gonnet",
        "manuel b gonnet partido la plata",
    ],
    "city-bell": [
        "city bell",
        "citybell",
        "city-bell",
    ],
    "villa-elisa": [
        "villa elisa",
    ],
    "ringuelet": [
        "ringuelet",
    ],
    "tolosa": [
        "tolosa",
    ],
    "arturo-segui": [
        "arturo segui",
        "arturo seguí",
        "arturo segui partido la plata",
    ],
    "los-hornos": [
        "los hornos",
    ],
    "altos-de-san-lorenzo": [
        "altos de san lorenzo",
        "altos san lorenzo",
    ],
    "villa-elvira": [
        "villa elvira",
    ],
    "san-carlos": [
        # "san carlos" suelto es ambiguo (hay muchos en Argentina);
        # requerimos contexto de La Plata para evitar falsos positivos.
        "san carlos partido la plata",
        "san carlos la plata",
        "san carlos, la plata",
    ],
    "melchor-romero": [
        "melchor romero",
        "m romero",
        "m. romero",
    ],
    "abasto": [
        "abasto",
    ],
    "olmos": [
        "olmos",
    ],
    "etcheverry": [
        "etcheverry",
    ],
    "hernandez": [
        "hernandez",
        "hernández",
    ],
    "gorina": [
        "gorina",
        "joaquin gorina",
        "joaquín gorina",
        "j gorina",
        "j. gorina",
    ],
    "sicardi": [
        "sicardi",
    ],
    "berisso": [
        "berisso",
    ],
    "ensenada": [
        "ensenada",
    ],
}

# Sentinel para "no se pudo determinar la zona".
SIN_ZONA = "sin_zona"


# ─── Normalización ────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Minúsculas, sin acentos/diacríticos, solo a-z0-9 y espacios simples."""
    nfd = unicodedata.normalize("NFD", s.lower())
    sin_tildes = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    solo_alfanum = re.sub(r"[^a-z0-9]+", " ", sin_tildes)
    return re.sub(r" +", " ", solo_alfanum).strip()


def _tokens_match(text_tokens: list[str], alias_tokens: tuple[str, ...]) -> bool:
    """True si alias_tokens aparece como secuencia de palabras completas en text_tokens."""
    n = len(alias_tokens)
    if n > len(text_tokens):
        return False
    alias_list = list(alias_tokens)
    for i in range(len(text_tokens) - n + 1):
        if text_tokens[i : i + n] == alias_list:
            return True
    return False


# Índice precalculado: (tokens_del_alias_normalizados, slug).
# Construido una vez al cargar el módulo.
_ALIAS_INDEX: list[tuple[tuple[str, ...], str]] = [
    (tuple(_norm(alias).split()), slug)
    for slug, aliases in ZONAS_CANONICAS.items()
    for alias in aliases
]


# ─── API pública ──────────────────────────────────────────────────────────────

def normalizar(texto: str, fallback: str = "") -> str:
    """Devuelve el slug canónico que aparece en el texto, o SIN_ZONA.

    Estrategia de desambiguación:
      1. Se buscan TODOS los aliases que matchean en el texto.
      2. Si alguna zona específica matchea (no "la-plata"), se devuelve
         la de alias más largo (más específica). "la-plata" se ignora si
         hay competencia, porque aparece en casi toda dirección argentina.
      3. Si solo "la-plata" matchea, se devuelve "la-plata".
      4. Si nada matchea y el texto tiene contenido, se logguea WARNING
         para enriquecer el diccionario de aliases.

    Args:
        texto  : campo zona_texto de la propiedad (más confiable).
        fallback: campo titulo (usado si zona_texto no arroja resultado).
    """
    for raw in (texto, fallback):
        if not raw or not raw.strip():
            continue

        tokens = _norm(raw).split()
        if not tokens:
            continue

        # Encontrar todos los aliases que matchean
        matches: list[tuple[int, str]] = []   # (len_alias, slug)
        for alias_tokens, slug in _ALIAS_INDEX:
            if _tokens_match(tokens, alias_tokens):
                matches.append((len(alias_tokens), slug))

        if not matches:
            continue

        # Separar "la-plata" de zonas específicas
        especificas = [(lng, slug) for (lng, slug) in matches if slug != "la-plata"]
        if especificas:
            # Devolver la zona más específica (alias más largo)
            return max(especificas, key=lambda x: x[0])[1]
        else:
            # Solo "la-plata" matcheó
            return "la-plata"

    # Nada detectado en ninguna fuente
    texto_para_log = (texto or "").strip() or (fallback or "").strip()
    if texto_para_log and len(texto_para_log) > 4:
        log.warning(
            "zonas: zona no reconocida — considerá agregar alias al diccionario: %r",
            texto_para_log[:150],
        )
    return SIN_ZONA


def en_busqueda(zona_canonica: str | None, zonas_busqueda: list[str]) -> bool:
    """True si la zona de la propiedad es compatible con la búsqueda.

    Conservador por diseño:
    - SIN_ZONA o None → True (sin dato, la IA decide)
    - zonas_busqueda vacío → True (búsqueda sin restricción geográfica)
    - zona conocida y no en la lista → False (descarta antes de gastar tokens)
    """
    if not zona_canonica or zona_canonica == SIN_ZONA:
        return True
    if not zonas_busqueda:
        return True
    return zona_canonica in zonas_busqueda
