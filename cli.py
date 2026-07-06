"""
Prueba one-shot desde la terminal, sin levantar el servidor:

    export ANTHROPIC_API_KEY=sk-ant-...
    python cli.py "casa apto banco nación zona norte hasta 150000 usd 2 dormitorios y parque"

Interpreta el pedido, barre los portales una vez, evalúa y muestra el ranking.
Útil para verificar que los scrapers siguen funcionando portal por portal.
"""

import json
import logging
import sys

import ai_matcher
import scraper

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    pedido = " ".join(sys.argv[1:])
    print(f"\nPEDIDO: {pedido}\n{'─' * 60}")

    parsed = ai_matcher.parse_pedido(pedido)
    print("INTERPRETACIÓN:")
    print(json.dumps(parsed, ensure_ascii=False, indent=2))

    search = {"id": "cli", "pedido_raw": pedido, "parsed": parsed}
    props = scraper.run_scan_for_search(search)
    candidatas = [p for p in props if ai_matcher.prefilter(search, p)]
    print(f"\n{len(props)} publicaciones encontradas · {len(candidatas)} pasan el pre-filtro")
    print("Evaluando con IA…\n")

    resultados = ai_matcher.score_batch(search, candidatas)
    ranking = sorted(
        ((resultados[p["id"]], p) for p in candidatas if p["id"] in resultados),
        key=lambda x: x[0].get("score", 0), reverse=True,
    )

    for score, p in ranking:
        v = score.get("veredicto", "?").upper()
        precio = f"{(p.get('moneda') or '').upper()} {p.get('precio') or 's/p'}"
        print(f"[{score.get('score'):>3}] {v:9} {p.get('portal'):13} {precio:>14}  {p.get('titulo', '')[:60]}")
        for r in score.get("razones", []):
            print(f"      — {r}")
        if score.get("faltantes"):
            print(f"      a confirmar: {', '.join(score['faltantes'])}")
        print(f"      {p.get('url')}\n")


if __name__ == "__main__":
    main()
