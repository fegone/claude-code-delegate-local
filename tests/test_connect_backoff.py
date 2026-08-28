"""Regression tests for the connect-error backoff ladder.

Why this exists: restarting the LiteLLM proxy leaves it refusing NEW connections for
55-65 s (measured 2026-08-28: 65.3 s and 55.4 s). The generic transient ladder waits
1+2+4 = 7 s of base with equal-jitter DOWNWARD, so it gave up somewhere between 3.5 s
and 7 s and discarded whole multi-turn dispatches. These tests pin the ladder that
survives that window, and pin that ordinary transients did NOT get slower.

Run: .venv/bin/python -m pytest tests/test_connect_backoff.py
"""
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

MEASURED_OUTAGE_S = 65.3  # el peor reinicio medido


def _worst_case_total(backoff, max_retries, jitter_low):
    """Suma de esperas en el peor caso (el jitter que menos espera), como hace el codigo:
    se duerme antes de cada reintento, es decir max_retries veces."""
    return sum(
        backoff[min(i, len(backoff) - 1)] * jitter_low
        for i in range(max_retries)
    )


def test_connect_ladder_cubre_el_reinicio_medido():
    """Incluso con el jitter minimo, la escalera tiene que pasar de los 65,3 s medidos."""
    total = _worst_case_total(server.CONNECT_BACKOFF, server.CONNECT_MAX_RETRIES, 1.0)
    assert total > MEASURED_OUTAGE_S, f"solo espera {total}s, la caida medida fue {MEASURED_OUTAGE_S}s"


def test_el_jitter_de_conexion_nunca_espera_MENOS_que_la_base():
    """Volver antes de tiempo es justo el fallo que se quiere evitar."""
    for attempt in range(server.CONNECT_MAX_RETRIES):
        base = server.CONNECT_BACKOFF[min(attempt, len(server.CONNECT_BACKOFF) - 1)]
        for _ in range(50):
            assert server._retry_delay(attempt, connect=True) >= base


def test_la_escalera_normal_sigue_siendo_corta():
    """Un 429 o un corte a mitad de stream no debe esperar un minuto."""
    for attempt in range(server.BACKEND_MAX_RETRIES):
        for _ in range(50):
            d = server._retry_delay(attempt)
            assert d <= server.RETRY_BACKOFF[min(attempt, len(server.RETRY_BACKOFF) - 1)]


def test_conexion_tiene_mas_intentos_que_el_resto():
    assert server.CONNECT_MAX_RETRIES > server.BACKEND_MAX_RETRIES


def test_retry_after_del_servidor_sigue_mandando_sobre_la_escalera():
    """Si el backend dice cuanto esperar, eso gana; el modo connect no lo pisa."""
    d = server._retry_delay(0, 42.0, connect=True)
    assert 42.0 <= d <= 42.0 + 5.0


def test_connecterror_y_connecttimeout_son_los_dos_fallos_de_conexion():
    """El codigo decide por isinstance; si httpx reorganiza la jerarquia, que se note aqui."""
    assert isinstance(httpx.ConnectError("x"), httpx.TransportError)
    assert isinstance(httpx.ConnectTimeout("x"), httpx.TransportError)
    # y NO deben confundirse con un timeout de lectura, que sigue en la escalera corta
    assert not isinstance(httpx.ReadTimeout("x"), (httpx.ConnectError, httpx.ConnectTimeout))
