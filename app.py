"""
Property Scout v3.1 — Servidor
Cambios: scans serializados (un browser a la vez), guardado incremental
(los resultados aparecen en el dashboard a medida que se evalúan),
logs de inicio/fin de pipeline con duración.

Endpoints:
  GET  /                      → dashboard
  POST /api/search            → {"pedido": "texto libre"} crea una búsqueda
  GET  /api/searches          → búsquedas
  POST /api/search/<id>/toggle→ pausar / reactivar
  DELETE /api/search/<id>     → eliminar
  GET  /api/matches?search_id=→ resultados evaluados
  POST /api/scan              → forzar scan de todas las activas
  GET  /api/status            → estado del agente
  POST /api/properties/ingest → recibir propiedades del scraper local
                                 Header: X-Ingest-Token

Variables de entorno:
  ANTHROPIC_API_KEY   (obligatoria)
  INGEST_TOKEN        (obligatoria para /api/properties/ingest)
  TELEGRAM_BOT_TOKEN  (opcional)
  TELEGRAM_CHAT_ID    (opcional)
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, send_from_directory

import ai_matcher
import scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DATA_DIR = "data"
SEARCHES_FILE = os.path.join(DATA_DIR, "searches.json")
PROPS_FILE = os.path.join(DATA_DIR, "properties.json")
SCAN_INTERVAL_MINUTES = 60
SCORE_CHUNK = 8  # evaluar y guardar de a tandas → resultados en vivo

_lock = threading.Lock()          # acceso a archivos
_pipeline = threading.Semaphore(1)  # un solo scan/browser a la vez
_scan_running = threading.Event()

app = Flask(__name__, static_folder="static")


# ─── Storage ──────────────────────────────────────────────────────────────────

def _load(path, default):
    if not os.path.exists(path):
        return default
    for attempt in range(2):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            if attempt == 0:
                log.warning("JSON corrupto en %s, reintentando en 100 ms…", path)
                time.sleep(0.1)
            else:
                log.error("JSON corrupto en %s tras reintento, usando default", path)
                return default


def _save(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_searches():
    return _load(SEARCHES_FILE, {"searches": []})


def load_props():
    return _load(PROPS_FILE, {"properties": {}, "last_scan": None, "scan_count": 0})


# ─── Telegram (opcional) ──────────────────────────────────────────────────────

def notify(texto: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": texto},
            timeout=15,
        )
    except Exception as e:
        log.warning("Telegram: %s", e)


# ─── Núcleo: procesar una búsqueda ────────────────────────────────────────────

def process_search(search: dict):
    """Scrapea → guarda lo encontrado → evalúa con IA de a tandas → guarda cada
    tanda. Serializado: nunca corren dos browsers a la vez."""
    with _pipeline:
        t0 = time.time()
        titulo = search["parsed"].get("titulo", search["id"])
        log.info("━━ Scan '%s' INICIADO", titulo)

        with _lock:
            db = load_props()
            known_ids = {
                pid for pid, p in db["properties"].items()
                if search["id"] in p.get("scores", {})
            }

        encontradas = scraper.run_scan_for_search(search, known_ids)
        nuevas = [p for p in encontradas if p["id"] not in known_ids]
        candidatas = [p for p in nuevas if ai_matcher.prefilter(search, p)]
        descartadas_pre = [p for p in nuevas if p["id"] not in {c["id"] for c in candidatas}]
        log.info("━━ '%s': %s encontradas | %s nuevas | %s a evaluar con IA",
                 titulo, len(encontradas), len(nuevas), len(candidatas))

        # Guardar TODO lo encontrado ya mismo (sin esperar a la IA)
        with _lock:
            db = load_props()
            for p in encontradas:
                entry = db["properties"].setdefault(p["id"], p)
                entry.update({k: v for k, v in p.items() if v})
                entry.setdefault("scores", {})
            for p in descartadas_pre:
                db["properties"][p["id"]]["scores"][search["id"]] = {
                    "score": 0, "veredicto": "descartar",
                    "razones": ["Fuera de rango de precio (pre-filtro)"],
                    "faltantes": [], "alertas": [],
                }
            _save(PROPS_FILE, db)

        # Evaluar de a tandas y guardar cada tanda → el dashboard se va llenando
        matches_total = 0
        for i in range(0, len(candidatas), SCORE_CHUNK):
            chunk = candidatas[i:i + SCORE_CHUNK]
            resultados = ai_matcher.score_batch(search, chunk)
            matches_chunk = []
            with _lock:
                db = load_props()
                for p in chunk:
                    if p["id"] in resultados:
                        db["properties"][p["id"]].setdefault("scores", {})
                        db["properties"][p["id"]]["scores"][search["id"]] = resultados[p["id"]]
                        if resultados[p["id"]].get("veredicto") == "match":
                            matches_chunk.append(db["properties"][p["id"]])
                _save(PROPS_FILE, db)
            matches_total += len(matches_chunk)
            log.info("━━ '%s': evaluadas %s/%s (%s matches hasta ahora)",
                     titulo, min(i + SCORE_CHUNK, len(candidatas)),
                     len(candidatas), matches_total)
            for m in matches_chunk:
                precio = f"{(m.get('moneda') or '').upper()} {m.get('precio') or 's/p'}"
                sc = m["scores"][search["id"]].get("score")
                notify(f"🎯 MATCH — {titulo}\n{m.get('titulo')}\n"
                       f"{precio} · score {sc}\n{m.get('url')}")

        with _lock:
            db = load_props()
            db["last_scan"] = datetime.now().isoformat(timespec="minutes")
            db["scan_count"] = db.get("scan_count", 0) + 1
            _save(PROPS_FILE, db)

        log.info("━━ Scan '%s' FINALIZADO en %.0fs — %s matches nuevos",
                 titulo, time.time() - t0, matches_total)
        return {"encontradas": len(encontradas),
                "nuevas_evaluadas": len(candidatas),
                "matches_nuevos": matches_total}


def scan_all():
    if _scan_running.is_set():
        log.info("Scan general ya en curso — se saltea")
        return
    _scan_running.set()
    try:
        for s in load_searches()["searches"]:
            if s.get("active", True):
                try:
                    process_search(s)
                except Exception as e:
                    log.error("Búsqueda %s falló: %s", s["id"], e)
    finally:
        _scan_running.clear()


# ─── API ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/portals")
def list_portals():
    """Lista de portales disponibles con metadata para el selector del frontend."""
    return jsonify({"portals": scraper.PORTAL_INFO})


@app.route("/api/search", methods=["POST"])
def create_search():
    body = request.get_json(silent=True) or {}
    pedido = body.get("pedido", "").strip()
    if not pedido:
        return jsonify({"error": "Falta el campo 'pedido'"}), 400

    # Portales opcionales: None = todos; lista validada contra PORTALES conocidos
    portales_raw = body.get("portales")
    portales = None
    if portales_raw is not None:
        portales = [p for p in (portales_raw or []) if p in scraper.PORTALES]
        if not portales:          # lista vacía o inválida → todos
            portales = None

    try:
        parsed = ai_matcher.parse_pedido(pedido)
    except RuntimeError as e:
        msg = str(e)
        if "Saldo de Anthropic" in msg or "credit balance" in msg or "billing" in msg.lower():
            log.error("Anthropic billing: %s", msg)
            return jsonify({"error": msg, "tipo": "billing"}), 503
        log.error("parse_pedido falló: %s", msg)
        return jsonify({"error": f"No se pudo interpretar el pedido: {msg}"}), 500
    except Exception as e:
        log.error("parse_pedido error inesperado: %s", e)
        return jsonify({"error": f"Error inesperado al interpretar el pedido: {e}"}), 500

    search = {
        "id": uuid.uuid4().hex[:8],
        "pedido_raw": pedido,
        "parsed": parsed,
        "portales": portales,     # None = todos los portales
        "active": True,
        "created": datetime.now().isoformat(timespec="minutes"),
    }
    with _lock:
        data = load_searches()
        data["searches"].append(search)
        _save(SEARCHES_FILE, data)

    threading.Thread(target=process_search, args=(search,), daemon=True).start()
    return jsonify({"search": search, "status": "búsqueda creada — primer scan en curso"})


@app.route("/api/searches")
def list_searches():
    return jsonify(load_searches())


@app.route("/api/search/<sid>/toggle", methods=["POST"])
def toggle_search(sid):
    with _lock:
        data = load_searches()
        for s in data["searches"]:
            if s["id"] == sid:
                s["active"] = not s.get("active", True)
                _save(SEARCHES_FILE, data)
                return jsonify({"search": s})
    return jsonify({"error": "no existe"}), 404


@app.route("/api/search/<sid>", methods=["DELETE"])
def delete_search(sid):
    with _lock:
        data = load_searches()
        data["searches"] = [s for s in data["searches"] if s["id"] != sid]
        _save(SEARCHES_FILE, data)
        db = load_props()
        for p in db["properties"].values():
            p.get("scores", {}).pop(sid, None)
        _save(PROPS_FILE, db)
    return jsonify({"ok": True})


@app.route("/api/matches")
def matches():
    sid = request.args.get("search_id")
    min_score = int(request.args.get("min_score", 0))
    db = load_props()
    searches_map = {s["id"]: s for s in load_searches()["searches"]}
    out = []
    for p in db["properties"].values():
        for s_id, score in p.get("scores", {}).items():
            if s_id not in searches_map:
                continue
            if sid and s_id != sid:
                continue
            if score.get("score", 0) < min_score:
                continue
            # Filtro por portal: respetar la restricción de la búsqueda
            # tanto para datos nuevos como para los ya ingestados.
            portales = searches_map[s_id].get("portales")
            if portales and p.get("portal") not in portales:
                continue
            out.append({**{k: p.get(k) for k in
                           ("id", "portal", "url", "titulo", "precio",
                            "moneda", "zona_texto", "fecha_detectada", "imagen")},
                        "search_id": s_id, **score})
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify({"matches": out, "last_scan": db.get("last_scan")})


@app.route("/api/scan", methods=["POST"])
def force_scan():
    threading.Thread(target=scan_all, daemon=True).start()
    return jsonify({"status": "scan iniciado"})


@app.route("/api/properties/ingest", methods=["POST"])
def ingest_properties():
    """Recibe propiedades scrapeadas localmente, las deduplica y las evalúa
    contra todas las búsquedas activas.

    Header requerido: X-Ingest-Token: <INGEST_TOKEN>
    Body: {"properties": [...]} o directamente [...]
    """
    # ── Autenticación ──
    token = request.headers.get("X-Ingest-Token", "")
    expected = os.environ.get("INGEST_TOKEN", "")
    if not expected or token != expected:
        log.warning("Ingest: token inválido (recibido: '%s...')", token[:6])
        return jsonify({"error": "Token inválido o INGEST_TOKEN no configurado"}), 401

    # ── Parsear body ──
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Body JSON inválido"}), 400
    raw = body.get("properties", body) if isinstance(body, dict) else body
    if not isinstance(raw, list):
        return jsonify({"error": "Se esperaba lista o {\"properties\": [...]}"}), 400

    # ── Validar y normalizar ──
    valid = []
    for p in raw:
        if not isinstance(p, dict) or not {"url", "portal"}.issubset(p):
            continue
        p.setdefault("id", scraper.make_id(p["url"], p["portal"]))
        p.setdefault("titulo", "")
        p.setdefault("precio", None)
        p.setdefault("moneda", None)
        p.setdefault("zona_texto", "")
        p.setdefault("descripcion", "")
        p.setdefault("imagen", None)
        p.setdefault("fecha_detectada", datetime.now().strftime("%Y-%m-%d %H:%M"))
        p.setdefault("scores", {})
        valid.append(p)

    if not valid:
        return jsonify({"error": "Sin propiedades válidas (campos mínimos: url, portal)"}), 400

    # ── Guardar y detectar nuevas ──
    nuevas_ids: set = set()
    with _lock:
        db = load_props()
        for p in valid:
            if p["id"] not in db["properties"]:
                nuevas_ids.add(p["id"])
            entry = db["properties"].setdefault(p["id"], {})
            for k, v in p.items():
                if k == "scores":
                    entry.setdefault("scores", {})
                elif v:
                    entry[k] = v
                else:
                    entry.setdefault(k, v)
        _save(PROPS_FILE, db)

    nuevas = [p for p in valid if p["id"] in nuevas_ids]
    log.info("Ingest: %d recibidas | %d nuevas | %d duplicadas",
             len(valid), len(nuevas), len(valid) - len(nuevas))

    if not nuevas:
        return jsonify({
            "recibidas": len(valid), "nuevas": 0,
            "duplicadas": len(valid), "evaluacion": "todo duplicado — nada nuevo",
        })

    # ── Evaluar nuevas contra búsquedas activas (en background, sin browser) ──
    searches = [s for s in load_searches()["searches"] if s.get("active", True)]

    def _score_nuevas():
        for search in searches:
            # Respetar restricción de portales: no gastar tokens en portales
            # que la búsqueda no incluye (el filtro de matches también lo haría,
            # pero es más eficiente no evaluar desde el principio).
            portales = search.get("portales")
            nuevas_para_search = [p for p in nuevas
                                  if not portales or p.get("portal") in portales]
            candidatas = [p for p in nuevas_para_search if ai_matcher.prefilter(search, p)]
            descartadas_pre = [p for p in nuevas_para_search
                               if p["id"] not in {c["id"] for c in candidatas}]

            with _lock:
                db = load_props()
                for p in descartadas_pre:
                    db["properties"][p["id"]].setdefault("scores", {})
                    db["properties"][p["id"]]["scores"][search["id"]] = {
                        "score": 0, "veredicto": "descartar",
                        "razones": ["Fuera de rango de precio (pre-filtro)"],
                        "faltantes": [], "alertas": [],
                    }
                _save(PROPS_FILE, db)

            titulo = search["parsed"].get("titulo", search["id"])
            log.info("Ingest scoring '%s': %d candidatas", titulo, len(candidatas))
            for i in range(0, len(candidatas), SCORE_CHUNK):
                chunk = candidatas[i:i + SCORE_CHUNK]
                try:
                    resultados = ai_matcher.score_batch(search, chunk)
                except Exception as e:
                    log.error("score_batch ingest '%s': %s", titulo, e)
                    continue
                with _lock:
                    db = load_props()
                    for p in chunk:
                        if p["id"] in resultados:
                            db["properties"][p["id"]].setdefault("scores", {})
                            db["properties"][p["id"]]["scores"][search["id"]] = resultados[p["id"]]
                            res = resultados[p["id"]]
                            if res.get("veredicto") == "match":
                                m = db["properties"][p["id"]]
                                precio_str = (
                                    f"{(m.get('moneda') or '').upper()} "
                                    f"{m.get('precio') or 's/p'}"
                                )
                                notify(
                                    f"🎯 MATCH (ingest) — {titulo}\n"
                                    f"{m.get('titulo')}\n"
                                    f"{precio_str} · score {res.get('score')}\n"
                                    f"{m.get('url')}"
                                )
                    _save(PROPS_FILE, db)

    if searches:
        threading.Thread(target=_score_nuevas, daemon=True).start()

    return jsonify({
        "recibidas": len(valid),
        "nuevas": len(nuevas),
        "duplicadas": len(valid) - len(nuevas),
        "evaluacion": (
            f"scoring en background contra {len(searches)} búsqueda(s) activa(s)"
            if searches else "sin búsquedas activas"
        ),
    })


@app.route("/api/status")
def status():
    db = load_props()
    return jsonify({
        "status": "ok",
        "scan_en_curso": _scan_running.is_set() or _pipeline._value == 0,
        "last_scan": db.get("last_scan"),
        "scan_count": db.get("scan_count", 0),
        "total_propiedades": len(db.get("properties", {})),
        "busquedas_activas": [s["parsed"].get("titulo")
                              for s in load_searches()["searches"] if s.get("active", True)],
        "api_key_configurada": bool(os.environ.get("ANTHROPIC_API_KEY")),
    })


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Verificar que /app/data es escribible al arrancar (detecta problemas de volumen temprano)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        test_path = os.path.join(DATA_DIR, ".write_test")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        log.info("DATA_DIR '%s' OK (ruta absoluta: %s)", DATA_DIR, os.path.abspath(DATA_DIR))
    except Exception as e:
        log.error("DATA_DIR '%s' NO escribible: %s — revisá permisos del volumen o seteá RAILWAY_RUN_UID=0", DATA_DIR, e)

    # Inicializar archivos si el volumen está vacío (ej: primer deploy con volumen nuevo)
    if not os.path.exists(SEARCHES_FILE):
        _save(SEARCHES_FILE, {"searches": []})
        log.info("searches.json inicializado (volumen vacío)")
    if not os.path.exists(PROPS_FILE):
        _save(PROPS_FILE, {"properties": {}, "last_scan": None, "scan_count": 0})
        log.info("properties.json inicializado (volumen vacío)")

    scheduler = BackgroundScheduler(timezone="America/Argentina/Buenos_Aires")
    scheduler.add_job(scan_all, "interval", minutes=SCAN_INTERVAL_MINUTES, id="scan")
    scheduler.start()
    log.info("Scheduler activo — scan cada %s min", SCAN_INTERVAL_MINUTES)

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
