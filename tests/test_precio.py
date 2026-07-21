"""
tests/test_precio.py — Casos borde del pipeline de precios ARS/USD

Cubre:
  - parse_precio: ausente, 0, "Consultar", ambiguo, parsing correcto
  - prefilter: misma moneda, cross-currency, precio ausente, API caída
  - fx.convert: conversión ARS↔USD, API caída (mock)
  - fx.warn_if_absurd: precios normales y absurdos
"""

import sys
import os
import types
import unittest
from unittest.mock import patch, MagicMock

# ── Path para importar módulos del proyecto ────────────────────────────────────
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

# Importar después de fijar el path
import fx
import scraper
import ai_matcher


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _search(precio_max, moneda="usd"):
    return {"parsed": {"precio_max": precio_max, "moneda": moneda}}


def _prop(precio, moneda):
    return {"precio": precio, "moneda": moneda, "url": "http://test.com/prop"}


TASA_BLUE = 1200.0  # ARS por 1 USD (valor fijo para tests)


# ─── parse_precio ─────────────────────────────────────────────────────────────

class TestParsePrecio(unittest.TestCase):

    def test_usd_variantes(self):
        for txt in ("USD 85.000", "U$S 85.000", "US$85.000", "U$D 85000"):
            with self.subTest(txt=txt):
                monto, moneda = scraper.parse_precio(txt)
                self.assertEqual(monto, 85000)
                self.assertEqual(moneda, "usd")

    def test_ars_pesos(self):
        monto, moneda = scraper.parse_precio("$ 120.000.000")
        self.assertEqual(monto, 120000000)
        self.assertEqual(moneda, "ars")

    def test_ars_sin_separador(self):
        monto, moneda = scraper.parse_precio("$ 5000000")
        self.assertEqual(monto, 5000000)
        self.assertEqual(moneda, "ars")

    def test_texto_vacio(self):
        self.assertEqual(scraper.parse_precio(""), (None, None))
        self.assertEqual(scraper.parse_precio(None), (None, None))

    def test_consultar_variantes(self):
        """'Consultar' y similares no deben pasar como precio 0."""
        for txt in ("Consultar", "Consultar precio", "Precio a consultar",
                    "A Consultar", "CONSULTAR PRECIO"):
            with self.subTest(txt=txt):
                monto, moneda = scraper.parse_precio(txt)
                self.assertIsNone(monto, msg=f"'{txt}' devolvió monto={monto}")

    def test_precio_cero_es_none(self):
        """Un precio de 0 no es válido para un inmueble."""
        monto, moneda = scraper.parse_precio("$ 0")
        self.assertIsNone(monto)
        self.assertEqual(moneda, "ars")

    def test_sin_digitos(self):
        monto, moneda = scraper.parse_precio("USD")
        self.assertIsNone(monto)
        self.assertEqual(moneda, "usd")

    def test_precio_usd_retiene_moneda(self):
        monto, moneda = scraper.parse_precio("USD 250.000")
        self.assertEqual(monto, 250000)
        self.assertEqual(moneda, "usd")


# ─── fx.convert ───────────────────────────────────────────────────────────────

class TestFxConvert(unittest.TestCase):

    def _mock_tasa(self, tasa):
        """Parchea get_tasa_blue en el módulo fx."""
        return patch.object(fx, "get_tasa_blue", return_value=tasa)

    def test_misma_moneda_no_convierte(self):
        # No debe llamar a la API si las monedas son iguales
        with patch.object(fx, "get_tasa_blue") as mock_api:
            result = fx.convert(100, "usd", "usd")
            mock_api.assert_not_called()
        self.assertEqual(result, 100.0)

    def test_usd_a_ars(self):
        with self._mock_tasa(TASA_BLUE):
            result = fx.convert(100, "usd", "ars")
        self.assertAlmostEqual(result, 100 * TASA_BLUE)

    def test_ars_a_usd(self):
        with self._mock_tasa(TASA_BLUE):
            result = fx.convert(120000, "ars", "usd")
        self.assertAlmostEqual(result, 120000 / TASA_BLUE)

    def test_api_caida_retorna_none(self):
        with self._mock_tasa(None):
            result = fx.convert(100, "usd", "ars")
        self.assertIsNone(result)

    def test_get_tasa_blue_api_caida_usa_cache_disco(self):
        """Si la API falla, debe usar la caché en disco."""
        import json, tempfile, time as _time

        cache_data = {"tasa": 999.5, "ts": _time.time() - 100}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            json.dump(cache_data, tf)
            tmp_path = tf.name

        # API falla, caché en disco tiene datos
        with patch.object(fx, "_CACHE_FILE", tmp_path), \
             patch.object(fx, "_mem", {}), \
             patch("requests.get", side_effect=ConnectionError("timeout")):
            tasa = fx.get_tasa_blue()

        os.unlink(tmp_path)
        self.assertAlmostEqual(tasa, 999.5)

    def test_get_tasa_blue_sin_cache_retorna_none(self):
        """Si la API falla y no hay caché, debe retornar None sin lanzar excepción."""
        with patch.object(fx, "_mem", {}), \
             patch.object(fx, "_disk_load", return_value={}), \
             patch("requests.get", side_effect=ConnectionError("timeout")):
            tasa = fx.get_tasa_blue()
        self.assertIsNone(tasa)


# ─── prefilter ────────────────────────────────────────────────────────────────

class TestPrefilter(unittest.TestCase):

    def _mock_tasa(self, tasa):
        return patch.object(fx, "get_tasa_blue", return_value=tasa)

    # -- Misma moneda ----------------------------------------------------------

    def test_mismo_usd_dentro_de_rango(self):
        s = _search(100_000, "usd")
        p = _prop(90_000, "usd")
        self.assertTrue(ai_matcher.prefilter(s, p))

    def test_mismo_usd_fuera_de_rango(self):
        s = _search(100_000, "usd")
        p = _prop(120_000, "usd")   # 20% sobre el máx
        self.assertFalse(ai_matcher.prefilter(s, p))

    def test_mismo_usd_tolerancia_8_pct(self):
        """Hasta 8% sobre el máximo debe pasar."""
        s = _search(100_000, "usd")
        p = _prop(107_999, "usd")   # 7.99% → pasa
        self.assertTrue(ai_matcher.prefilter(s, p))
        p2 = _prop(109_000, "usd")  # 9% → no pasa
        self.assertFalse(ai_matcher.prefilter(s, p2))

    def test_mismo_ars_fuera_de_rango(self):
        s = _search(50_000_000, "ars")
        p = _prop(80_000_000, "ars")
        self.assertFalse(ai_matcher.prefilter(s, p))

    # -- Cross-currency --------------------------------------------------------

    def test_prop_usd_busqueda_ars(self):
        """Propiedad en USD, búsqueda en ARS — debe convertir y filtrar."""
        # tasa=1200: prop U$S 100.000 = ARS 120.000.000
        s = _search(100_000_000, "ars")  # máx 100M ARS
        p = _prop(100_000, "usd")        # 100k USD = 120M ARS → fuera de rango
        with self._mock_tasa(TASA_BLUE):
            resultado = ai_matcher.prefilter(s, p)
        self.assertFalse(resultado)

    def test_prop_usd_busqueda_ars_dentro(self):
        """Propiedad en USD dentro del rango expresado en ARS."""
        s = _search(130_000_000, "ars")  # máx 130M ARS
        p = _prop(100_000, "usd")        # 100k USD = 120M ARS → dentro
        with self._mock_tasa(TASA_BLUE):
            resultado = ai_matcher.prefilter(s, p)
        self.assertTrue(resultado)

    def test_prop_ars_busqueda_usd(self):
        """Propiedad en ARS, búsqueda en USD — debe convertir y filtrar."""
        # tasa=1200: prop ARS 180.000.000 = USD 150.000
        s = _search(100_000, "usd")       # máx 100k USD
        p = _prop(180_000_000, "ars")     # 180M ARS = 150k USD → fuera de rango
        with self._mock_tasa(TASA_BLUE):
            resultado = ai_matcher.prefilter(s, p)
        self.assertFalse(resultado)

    def test_prop_ars_busqueda_usd_dentro(self):
        """Propiedad en ARS dentro del rango en USD."""
        s = _search(100_000, "usd")       # máx 100k USD
        p = _prop(60_000_000, "ars")      # 60M ARS = 50k USD → dentro
        with self._mock_tasa(TASA_BLUE):
            resultado = ai_matcher.prefilter(s, p)
        self.assertTrue(resultado)

    # -- API de cotización caída -----------------------------------------------

    def test_api_caida_no_descarta(self):
        """Si la API de cotización no está disponible, no debe descartar propiedades."""
        s = _search(100_000, "usd")
        p = _prop(200_000_000, "ars")   # claramente fuera de rango
        with self._mock_tasa(None):     # sin tasa → no puede comparar
            resultado = ai_matcher.prefilter(s, p)
        self.assertTrue(resultado, "Sin tasa, la propiedad no debe ser descartada")

    # -- Precio ausente --------------------------------------------------------

    def test_precio_ausente_pasa(self):
        s = _search(100_000, "usd")
        p = _prop(None, "usd")
        self.assertTrue(ai_matcher.prefilter(s, p))

    def test_precio_cero_pasa(self):
        """precio=0 debe tratarse igual que None (sin dato de precio)."""
        s = _search(100_000, "usd")
        p = _prop(0, "usd")
        self.assertTrue(ai_matcher.prefilter(s, p))

    def test_moneda_ausente_pasa(self):
        """Si la propiedad no tiene moneda detectada, deja pasar."""
        s = _search(100_000, "usd")
        p = _prop(200_000, None)   # precio conocido pero moneda desconocida
        self.assertTrue(ai_matcher.prefilter(s, p))

    def test_sin_precio_max_pasa_todo(self):
        """Si la búsqueda no tiene precio_max, nunca filtra por precio."""
        s = _search(None, "usd")
        p = _prop(999_999_999, "usd")
        self.assertTrue(ai_matcher.prefilter(s, p))

    # -- "Consultar precio" ----------------------------------------------------

    def test_consultar_precio_pasa(self):
        """Propiedad con precio 'Consultar' llega con precio=None → pasa al filtro IA."""
        monto, moneda = scraper.parse_precio("Consultar precio")
        s = _search(100_000, "usd")
        p = {"precio": monto, "moneda": moneda, "url": "http://test.com/c"}
        self.assertTrue(ai_matcher.prefilter(s, p))


# ─── warn_if_absurd ───────────────────────────────────────────────────────────

class TestWarnAbsurd(unittest.TestCase):

    def test_precio_normal_no_alerta(self):
        with self.assertLogs("fx", level="WARNING") as cm:
            # Necesitamos generar al menos un log para que assertLogs no falle
            import logging
            logging.getLogger("fx").warning("sentinel")
            fx.warn_if_absurd(150_000, "usd", "http://ok.com/prop")

        # No debe haber otro warning además del sentinel
        warnings = [m for m in cm.output if "absurdo" in m]
        self.assertEqual(len(warnings), 0)

    def test_precio_muy_bajo_usd(self):
        with self.assertLogs("fx", level="WARNING") as cm:
            fx.warn_if_absurd(100, "usd", "http://raro.com/prop")
        self.assertTrue(any("absurdo" in m for m in cm.output))

    def test_precio_muy_alto_ars(self):
        with self.assertLogs("fx", level="WARNING") as cm:
            fx.warn_if_absurd(999_999_999_999, "ars", "http://raro.com/prop")
        self.assertTrue(any("absurdo" in m for m in cm.output))

    def test_precio_none_no_falla(self):
        # No debe lanzar excepción con precio=None
        result = fx.warn_if_absurd(None, "usd", "http://test.com")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
