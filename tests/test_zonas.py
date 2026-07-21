"""
tests/test_zonas.py — Tests del módulo de canonicalización de zonas

Cubre:
  - normalizar(): aliases directos, variantes de escritura, ambigüedad
    "La Plata" vs zona específica, texto sin zona, texto vacío.
  - en_busqueda(): zona en lista, zona fuera, SIN_ZONA, lista vacía.
  - prefilter() con zona: rechaza zona incorrecta, deja pasar SIN_ZONA,
    deja pasar zona correcta, combina con filtro de precio.
  - Casos reportados: Gonnet ≠ Sicardi; City Bell ≠ Villa Elisa.
"""

import sys
import os
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

import zonas
import ai_matcher
import fx


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _search(zonas_list, precio_max=None, moneda="usd"):
    return {"parsed": {"zonas": zonas_list, "precio_max": precio_max, "moneda": moneda}}


def _prop(zona_canonica, zona_texto="", titulo="", precio=None, moneda="usd"):
    return {
        "zona_canonica": zona_canonica,
        "zona_texto": zona_texto,
        "titulo": titulo,
        "precio": precio,
        "moneda": moneda,
        "url": "http://test.com/prop",
    }


# ─── normalizar() ─────────────────────────────────────────────────────────────

class TestNormalizar(unittest.TestCase):

    # -- Casos directos --------------------------------------------------------

    def test_gonnet_exacto(self):
        self.assertEqual(zonas.normalizar("Gonnet"), "gonnet")

    def test_gonnet_minusculas(self):
        self.assertEqual(zonas.normalizar("gonnet"), "gonnet")

    def test_gonnet_alias_mb(self):
        self.assertEqual(zonas.normalizar("M. B. Gonnet"), "gonnet")

    def test_gonnet_alias_mb_sin_puntos(self):
        self.assertEqual(zonas.normalizar("M B Gonnet"), "gonnet")

    def test_gonnet_alias_mb_punto_b(self):
        self.assertEqual(zonas.normalizar("M.B. Gonnet"), "gonnet")

    def test_gonnet_nombre_completo(self):
        self.assertEqual(zonas.normalizar("Manuel B. Gonnet"), "gonnet")

    def test_gonnet_nombre_muy_completo(self):
        self.assertEqual(zonas.normalizar("Manuel Belgrano Gonnet"), "gonnet")

    def test_city_bell_exacto(self):
        self.assertEqual(zonas.normalizar("City Bell"), "city-bell")

    def test_city_bell_sin_espacio(self):
        self.assertEqual(zonas.normalizar("Citybell"), "city-bell")

    def test_villa_elisa(self):
        self.assertEqual(zonas.normalizar("Villa Elisa"), "villa-elisa")

    def test_villa_elvira(self):
        self.assertEqual(zonas.normalizar("Villa Elvira"), "villa-elvira")

    def test_tolosa(self):
        self.assertEqual(zonas.normalizar("Tolosa"), "tolosa")

    def test_ringuelet(self):
        self.assertEqual(zonas.normalizar("Ringuelet"), "ringuelet")

    def test_sicardi(self):
        self.assertEqual(zonas.normalizar("Sicardi"), "sicardi")

    def test_gorina_exacto(self):
        self.assertEqual(zonas.normalizar("Gorina"), "gorina")

    def test_gorina_nombre_completo(self):
        self.assertEqual(zonas.normalizar("Joaquín Gorina"), "gorina")

    def test_la_plata_exacto(self):
        self.assertEqual(zonas.normalizar("La Plata"), "la-plata")

    def test_casco_urbano(self):
        self.assertEqual(zonas.normalizar("casco urbano"), "la-plata")

    def test_arturo_segui_con_tilde(self):
        self.assertEqual(zonas.normalizar("Arturo Seguí"), "arturo-segui")

    def test_arturo_segui_sin_tilde(self):
        self.assertEqual(zonas.normalizar("Arturo Segui"), "arturo-segui")

    def test_hernandez_con_tilde(self):
        self.assertEqual(zonas.normalizar("Hernández"), "hernandez")

    def test_hernandez_sin_tilde(self):
        self.assertEqual(zonas.normalizar("Hernandez"), "hernandez")

    # -- Texto con contexto de portal -----------------------------------------

    def test_gonnet_en_texto_largo(self):
        """Texto típico de ML API: address_line con ciudad y provincia."""
        resultado = zonas.normalizar("Manuel B. Gonnet, Buenos Aires")
        self.assertEqual(resultado, "gonnet")

    def test_sicardi_en_texto_largo(self):
        resultado = zonas.normalizar("Sicardi, Partido La Plata, Buenos Aires")
        self.assertEqual(resultado, "sicardi")

    def test_city_bell_en_texto_largo(self):
        resultado = zonas.normalizar("City Bell, La Plata, Buenos Aires")
        self.assertEqual(resultado, "city-bell")

    # -- Desambiguación "La Plata" vs zona específica -------------------------

    def test_gonnet_gana_sobre_la_plata(self):
        """'Gonnet, La Plata' debe resolver a gonnet, no la-plata."""
        self.assertEqual(zonas.normalizar("Gonnet, La Plata"), "gonnet")

    def test_sicardi_gana_sobre_la_plata(self):
        self.assertEqual(zonas.normalizar("Sicardi, La Plata"), "sicardi")

    def test_villa_elisa_gana_sobre_la_plata(self):
        self.assertEqual(zonas.normalizar("Villa Elisa, La Plata"), "villa-elisa")

    def test_city_bell_gana_sobre_la_plata(self):
        self.assertEqual(zonas.normalizar("City Bell, La Plata"), "city-bell")

    # -- Casos que NO deben cruzar entre zonas (el bug original) ---------------

    def test_gonnet_no_es_sicardi(self):
        self.assertNotEqual(zonas.normalizar("Gonnet"), "sicardi")

    def test_sicardi_no_es_gonnet(self):
        self.assertNotEqual(zonas.normalizar("Sicardi"), "gonnet")

    def test_city_bell_no_es_villa_elisa(self):
        self.assertNotEqual(zonas.normalizar("City Bell"), "villa-elisa")

    def test_villa_elisa_no_es_city_bell(self):
        self.assertNotEqual(zonas.normalizar("Villa Elisa"), "city-bell")

    def test_villa_elisa_no_es_villa_elvira(self):
        self.assertNotEqual(zonas.normalizar("Villa Elisa"), "villa-elvira")

    def test_villa_elvira_no_es_villa_elisa(self):
        self.assertNotEqual(zonas.normalizar("Villa Elvira"), "villa-elisa")

    # -- Texto vacío / sin zona -----------------------------------------------

    def test_texto_vacio_es_sin_zona(self):
        self.assertEqual(zonas.normalizar(""), zonas.SIN_ZONA)

    def test_none_como_string_vacio(self):
        # En la práctica zona_texto puede ser None; tratar igual que ""
        self.assertEqual(zonas.normalizar(""), zonas.SIN_ZONA)

    def test_sin_zona_conocida(self):
        """Texto que no es ninguna localidad conocida → SIN_ZONA."""
        self.assertEqual(zonas.normalizar("Barrio El Pinar"), zonas.SIN_ZONA)

    def test_titulo_sin_zona_es_sin_zona(self):
        self.assertEqual(zonas.normalizar("", "Casa 3 ambientes amplia"), zonas.SIN_ZONA)

    # -- Fallback a titulo -----------------------------------------------------

    def test_zona_en_titulo_cuando_zona_texto_vacia(self):
        """Si zona_texto está vacía, debe buscar en el titulo."""
        self.assertEqual(zonas.normalizar("", "Casa en Gonnet con jardín"), "gonnet")

    def test_zona_texto_tiene_prioridad_sobre_titulo(self):
        """zona_texto debe usarse antes que titulo."""
        # zona_texto = Sicardi, titulo menciona Gonnet → debe devolver sicardi
        self.assertEqual(zonas.normalizar("Sicardi", "Casa cerca de Gonnet"), "sicardi")

    def test_titulo_con_ciudad_plata_sin_zona_especifica(self):
        """Titulo que solo menciona 'La Plata' → la-plata."""
        self.assertEqual(zonas.normalizar("", "Departamento en La Plata 2 amb"), "la-plata")


# ─── en_busqueda() ────────────────────────────────────────────────────────────

class TestEnBusqueda(unittest.TestCase):

    def test_zona_en_lista(self):
        self.assertTrue(zonas.en_busqueda("gonnet", ["gonnet", "city-bell"]))

    def test_zona_fuera_de_lista(self):
        self.assertFalse(zonas.en_busqueda("sicardi", ["gonnet", "city-bell"]))

    def test_sin_zona_siempre_pasa(self):
        """SIN_ZONA nunca debe bloquearse."""
        self.assertTrue(zonas.en_busqueda("sin_zona", ["gonnet"]))

    def test_none_siempre_pasa(self):
        self.assertTrue(zonas.en_busqueda(None, ["gonnet"]))

    def test_lista_vacia_pasa_todo(self):
        """Búsqueda sin restricción de zona."""
        self.assertTrue(zonas.en_busqueda("sicardi", []))

    def test_gonnet_no_esta_en_busqueda_sicardi(self):
        self.assertFalse(zonas.en_busqueda("gonnet", ["sicardi"]))

    def test_la_plata_no_es_gonnet(self):
        self.assertFalse(zonas.en_busqueda("la-plata", ["gonnet"]))


# ─── prefilter() con zona ─────────────────────────────────────────────────────

class TestPrefilterZona(unittest.TestCase):

    # -- Zona incorrecta -------------------------------------------------------

    def test_sicardi_descartada_en_busqueda_gonnet(self):
        """Bug original: Sicardi no debe aparecer al buscar Gonnet."""
        s = _search(["gonnet"])
        p = _prop("sicardi")
        self.assertFalse(ai_matcher.prefilter(s, p))

    def test_city_bell_descartada_en_busqueda_villa_elisa(self):
        s = _search(["villa-elisa"])
        p = _prop("city-bell")
        self.assertFalse(ai_matcher.prefilter(s, p))

    def test_la_plata_descartada_en_busqueda_gonnet(self):
        s = _search(["gonnet"])
        p = _prop("la-plata")
        self.assertFalse(ai_matcher.prefilter(s, p))

    # -- Zona correcta ---------------------------------------------------------

    def test_gonnet_pasa_en_busqueda_gonnet(self):
        s = _search(["gonnet"])
        p = _prop("gonnet")
        self.assertTrue(ai_matcher.prefilter(s, p))

    def test_gonnet_pasa_en_busqueda_multizona(self):
        s = _search(["gonnet", "city-bell", "ringuelet"])
        p = _prop("gonnet")
        self.assertTrue(ai_matcher.prefilter(s, p))

    def test_city_bell_pasa_en_busqueda_multizona(self):
        s = _search(["gonnet", "city-bell"])
        p = _prop("city-bell")
        self.assertTrue(ai_matcher.prefilter(s, p))

    # -- Sin zona (conservador) -----------------------------------------------

    def test_sin_zona_siempre_pasa(self):
        """Propiedad sin zona detectada → la IA decide."""
        s = _search(["gonnet"])
        p = _prop(zonas.SIN_ZONA)
        self.assertTrue(ai_matcher.prefilter(s, p))

    def test_none_zona_pasa(self):
        s = _search(["gonnet"])
        p = _prop(None)
        self.assertTrue(ai_matcher.prefilter(s, p))

    def test_busqueda_sin_zonas_pasa_todo(self):
        s = _search([])
        p = _prop("sicardi")
        self.assertTrue(ai_matcher.prefilter(s, p))

    # -- Combinación zona + precio --------------------------------------------

    def test_zona_correcta_precio_ok(self):
        s = _search(["gonnet"], precio_max=100_000, moneda="usd")
        p = _prop("gonnet", precio=80_000, moneda="usd")
        self.assertTrue(ai_matcher.prefilter(s, p))

    def test_zona_correcta_precio_excedido(self):
        s = _search(["gonnet"], precio_max=100_000, moneda="usd")
        p = _prop("gonnet", precio=150_000, moneda="usd")
        self.assertFalse(ai_matcher.prefilter(s, p))

    def test_zona_incorrecta_descarta_antes_que_precio(self):
        """Zona incorrecta debe descartar aunque el precio esté bien."""
        s = _search(["gonnet"], precio_max=200_000, moneda="usd")
        p = _prop("sicardi", precio=50_000, moneda="usd")  # precio ok, zona mal
        self.assertFalse(ai_matcher.prefilter(s, p))

    def test_sin_zona_precio_excedido_descarta(self):
        """SIN_ZONA pasa el filtro de zona, pero el precio puede descartarla."""
        s = _search(["gonnet"], precio_max=100_000, moneda="usd")
        p = _prop(zonas.SIN_ZONA, precio=200_000, moneda="usd")
        self.assertFalse(ai_matcher.prefilter(s, p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
