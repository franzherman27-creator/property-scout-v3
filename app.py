"""
Property Scout v3 — Servidor
Flask + scheduler. Orquesta: scrapear → pre-filtrar → evaluar con IA → guardar → avisar.

Endpoints:
  GET  /                      → dashboard
  POST /api/search            → {"pedido": "texto libre"} crea una búsqueda
  GET  /api/searches          → búsquedas activas
  POST /api/search/<id>/toggle→ pausar / reactivar
  DELETE /api/search/<id>     → eliminar
  GET  /api/matches?search_id=→ resultados evaluados
  POST /api/scan              → forzar scan de todas las búsquedas activas
  GET  /api/status            → estado del agente

Variables de entorno:
  ANTHROPIC_API_KEY   (obligatoria)
  TELEGRAM_BOT_TOKEN  (opcional — avisos de matches nuevos)
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

_lock = threading.Lock()
_scan_running = threading.Event()

app = Flask(__name__, static_folder="static")


# ─── Storage ──────────────────────────────────────────────────────────────────

def _load(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def _save(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
            json={"chat_id": chat, "text": texto, "disable_web_page_preview": False},
            timeout=15,
        )
    except Exception as e:
        log.warning("Telegram: %s", e)


# ─── Núcleo: procesar una búsqueda ────────────────────────────────────────────

def process_search(search: dict):
    """Scrapea el círculo de portales, evalúa lo nuevo con IA y guarda."""
    with _lock:
        db = load_props()
        known_ids = {
            pid for pid, p in db["properties"].items()
            if search["id"] in p.get("scores", {})
        }

    log.info("Scan búsqueda '%s' (%s conocidas)", search["parsed"].get("titulo"), len(known_ids))
    encontradas = scraper.run_scan_for_search(search, known_ids)

    nuevas = [p for p in encontradas if p["id"] not in known_ids]
    candidatas = [p for p in nuevas if ai_matcher.prefilter(search, p)]
    log.info("Encontradas %s | nuevas %s | pasan pre-filtro %s",
             len(encontradas), len(nuevas), len(candidatas))

    resultados = ai_matcher.score_batch(search, candidatas)

    matches_nuevos = []
    with _lock:
        db = load_props()
        for p in encontradas:
            entry = db["properties"].setdefault(p["id"], p)
            entry.update({k: v for k, v in p.items() if v})  # refresca precio/desc
            entry.setdefault("scores", {})
            if p["id"] in resultados:
                entry["scores"][search["id"]] = resultados[p["id"]]
                if resultados[p["id"]].get("veredicto") == "match":
                    matches_nuevos.append(entry)
            elif p["id"] in {x["id"] for x in nuevas}:
                # nueva pero descartada por pre-filtro: dejar constancia
                entry["scores"][search["id"]] = {
                    "score": 0, "veredicto": "descartar",
                    "razones": ["Fuera de rango de precio (pre-filtro)"],
                    "faltantes": [], "alertas": [],
                }
        db["last_scan"] = datetime.now().isoformat(timespec="minutes")
        db["scan_count"] = db.get("scan_count", 0) + 1
        _save(PROPS_FILE, db)

    for m in matches_nuevos:
        precio = f"{(m.get('moneda') or '').upper()} {m.get('precio') or 's/p'}"
        score = m["scores"][search["id"]].get("score")
        notify(f"🎯 MATCH — {search['parsed'].get('titulo')}\n"
               f"{m.get('titulo')}\n{precio} · score {score}\n{m.get('url')}")

    return {"encontradas": len(encontradas), "nuevas_evaluadas": len(candidatas),
            "matches_nuevos": len(matches_nuevos)}


def scan_all():
    if _scan_running.is_set():
        log.info("Scan ya en curso — se saltea")
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


@app.route("/api/search", methods=["POST"])
def create_search():
    pedido = (request.get_json(silent=True) or {}).get("pedido", "").strip()
    if not pedido:
        return jsonify({"error": "Falta el campo 'pedido'"}), 400
    try:
        parsed = ai_matcher.parse_pedido(pedido)
    except Exception as e:
        return jsonify({"error": f"No se pudo interpretar el pedido: {e}"}), 500

    search = {
        "id": uuid.uuid4().hex[:8],
        "pedido_raw": pedido,
        "parsed": parsed,
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
    return jsonify({"ok": True})


@app.route("/api/matches")
def matches():
    sid = request.args.get("search_id")
    min_score = int(request.args.get("min_score", 0))
    db = load_props()
    out = []
    for p in db["properties"].values():
        for s_id, score in p.get("scores", {}).items():
            if sid and s_id != sid:
                continue
            if score.get("score", 0) >= min_score:
                out.append({**{k: p.get(k) for k in
                               ("id", "portal", "url", "titulo", "precio",
                                "moneda", "zona_texto", "fecha_detectada")},
                            "search_id": s_id, **score})
    out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify({"matches": out, "last_scan": db.get("last_scan")})


@app.route("/api/scan", methods=["POST"])
def force_scan():
    threading.Thread(target=scan_all, daemon=True).start()
    return jsonify({"status": "scan iniciado"})


@app.route("/api/status")
def status():
    db = load_props()
    return jsonify({
        "status": "ok",
        "scan_en_curso": _scan_running.is_set(),
        "last_scan": db.get("last_scan"),
        "scan_count": db.get("scan_count", 0),
        "total_propiedades": len(db.get("properties", {})),
        "busquedas_activas": [s["parsed"].get("titulo")
                              for s in load_searches()["searches"] if s.get("active", True)],
        "api_key_configurada": bool(os.environ.get("ANTHROPIC_API_KEY")),
    })


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    scheduler = BackgroundScheduler(timezone="America/Argentina/Buenos_Aires")
    scheduler.add_job(scan_all, "interval", minutes=SCAN_INTERVAL_MINUTES, id="scan")
    scheduler.start()
    log.info("Scheduler activo — scan cada %s min", SCAN_INTERVAL_MINUTES)

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
