"""Tests de los arreglos de cuelgues/rendimiento (2026-08-14).

Cubren tres fallos reales observados en produccion:
  1. Presupuesto inferido del SUFIJO del alias -> qwen-3-8-max-think se quedaba en
     65536, el razonamiento se comia el presupuesto y la respuesta salia VACIA sin error.
  2. Nudges disparados sobre turnos cortados por max_tokens o vacios -> un fallo se
     convertia en TRES llamadas largas, sin poder arreglar nada (el turno siguiente
     lleva el mismo presupuesto y muere igual).
  3. TURN_TIMEOUT es por INTENTO: intentos x turnos daba un techo nominal de ~50h por
     despacho, con el slot del proveedor ocupado por trabajo que ya nadie espera.
"""
import importlib.util
import pathlib

import pytest

SERVER = pathlib.Path(__file__).resolve().parent.parent / "server.py"


def _load():
    spec = importlib.util.spec_from_file_location("srv_hang", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


srv = _load()


# ── 1. Presupuesto por alias ────────────────────────────────────────────────────

def test_qwen_think_recibe_presupuesto_ampliado():
    """El bug original: termina en '-think', no en '-max', y se quedaba en el default.

    Razona alto, asi que con 65536 el razonamiento consume todo y la respuesta sale
    vacia SIN error. Debe recibir el cap real del provider.
    """
    assert srv._resolve_max_tokens("qwen-3-8-max-think", None) == 131_072


def test_alias_de_la_tabla_no_dependen_del_sufijo():
    esperado = {
        "qwen-3-8-max": 131_072,
        "glm-coding-plan": 65_536,
        "glm-coding-plan-think": 65_536,
        "glm-coding-plan-max": 131_072,
        "deepseek-v4-flash": 150_000,
        "deepseek-v4-pro-max": 150_000,
    }
    for alias, budget in esperado.items():
        assert srv._resolve_max_tokens(alias, None) == budget, alias


def test_alias_desconocido_cae_al_default_y_avisa(caplog):
    """No debe romperse con un alias nuevo, pero tampoco fallar en silencio."""
    import logging
    with caplog.at_level(logging.WARNING, logger="delegate"):
        got = srv._resolve_max_tokens("modelo-que-no-existe", None)
    assert got == srv.DEFAULT_MAX_TOKENS
    assert "MODEL_BUDGET_POLICY" in caplog.text


def test_valor_explicito_gana_sobre_la_tabla():
    assert srv._resolve_max_tokens("qwen-3-8-max-think", 4096) == 4096


def test_la_tabla_nunca_excede_el_cap_del_provider():
    """Una politica mal puesta no debe poder provocar el rechazo 1210 del provider."""
    for alias in srv.MODEL_BUDGET_POLICY:
        cap = srv._provider_max_tokens_cap(alias)
        if cap is not None:
            assert srv._resolve_max_tokens(alias, None) <= cap, alias


# ── 2. Condicion del nudge ──────────────────────────────────────────────────────

# Se ejercita la funcion REAL del server, no una copia de la condicion: un test que
# reimplementa la logica que pretende verificar pasa aunque el codigo este roto.
_can_nudge = srv._should_nudge


@pytest.mark.parametrize("stop_reason,text", [
    ("max_tokens", ""),                 # presupuesto agotado, sin respuesta
    ("max_tokens", "texto parcial"),    # cortado a media respuesta
    ("unknown", ""),                    # stream truncado
    ("content_filter", ""),             # cortado por moderacion
    ("end_turn", ""),                   # termino normal pero no dijo nada
    ("end_turn", "   \n  "),            # solo espacios
])
def test_no_nudgear_cuando_no_hay_nada_que_interrogar(stop_reason, text):
    """Re-preguntar no puede arreglar un presupuesto agotado ni una respuesta vacia:
    el turno siguiente lleva el mismo presupuesto. Nudgear ahi solo triplica el costo."""
    assert _can_nudge(stop_reason, text) is False


def test_si_se_nudgea_el_caso_que_motivo_la_funcion():
    """El nudge existe por un fallo real (2026-08-08): el agente ANUNCIO una accion y
    paro, y se acepto como exito. Ese caso debe seguir recibiendo nudge."""
    assert _can_nudge("end_turn", "Ahora voy a correr los tests para verificar.") is True


# ── 3. Deadline total del despacho ──────────────────────────────────────────────

def test_existe_deadline_total_y_es_finito():
    assert isinstance(srv.DISPATCH_TIMEOUT, int)
    assert 0 < srv.DISPATCH_TIMEOUT <= 24 * 3600


def test_el_deadline_total_corta_antes_que_el_peor_caso_por_intentos():
    """El punto del arreglo: sin deadline, intentos x turnos x TURN_TIMEOUT daba un
    techo nominal de ~50h. El deadline debe ser mucho menor que ese producto."""
    peor_caso = (srv.BACKEND_MAX_RETRIES + 1) * 25 * srv.TURN_TIMEOUT
    assert srv.DISPATCH_TIMEOUT < peor_caso / 10


def test_batch_no_corta_antes_que_el_despacho_individual():
    """Cuando ambos valian 1800s, el mismo trabajo se cancelaba en batch y seguia horas
    en individual — por eso los fallos parecian aleatorios. Batch incluye ademas la
    espera de slot, asi que debe cubrir el deadline individual mas la cola."""
    assert srv.BATCH_TASK_TIMEOUT >= srv.DISPATCH_TIMEOUT
    assert srv.BATCH_TASK_TIMEOUT >= srv.DISPATCH_TIMEOUT + srv.BATCH_QUEUE_GRACE


def test_hay_margen_minimo_para_iniciar_un_intento():
    assert 0 < srv.DISPATCH_MIN_SLICE < srv.DISPATCH_TIMEOUT


# ── El logger no puede contaminar stdout (MCP habla por stdio) ───────────────────

def test_el_logger_no_escribe_en_stdout(capsys):
    srv.log.warning("mensaje de prueba")
    capturado = capsys.readouterr()
    assert capturado.out == "", "un MCP stdio se rompe si algo escribe en stdout"


# ── Reintentos: una sola capa, no dos ───────────────────────────────────────────

def test_reconoce_el_endpoint_de_litellm():
    """El header solo debe viajar al proxy, no a un provider directo."""
    assert srv._is_litellm_endpoint(srv.LITELLM_URL) is True
    assert srv._is_litellm_endpoint("https://api.z.ai/api/anthropic/v1/messages") is False
    assert srv._is_litellm_endpoint("") is False
    assert srv._is_litellm_endpoint(None) is False


def test_mismo_host_distinta_ruta_sigue_siendo_litellm():
    """El proxy se identifica por scheme+host+puerto, no por el path exacto."""
    import urllib.parse
    b = urllib.parse.urlsplit(srv.LITELLM_URL)
    otra_ruta = f"{b.scheme}://{b.netloc}/v1/chat/completions"
    assert srv._is_litellm_endpoint(otra_ruta) is True


def test_url_basura_no_revienta():
    for mala in ("://no-es-url", "no-es-una-url", "http://[bad"):
        assert srv._is_litellm_endpoint(mala) is False
