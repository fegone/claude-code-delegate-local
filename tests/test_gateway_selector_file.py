"""T16 · DELEGATE_GATEWAY_FILE: selector en archivo JSON (mock-only, sin red)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gateway_t09 import _GATEWAY_ENV, _load_server  # noqa: E402

BIF = {"DELEGATE_BIFROST_URL": "http://127.0.0.1:8081"}


@pytest.fixture(autouse=True)
def _sin_file_env(monkeypatch):
    monkeypatch.delenv("DELEGATE_GATEWAY_FILE", raising=False)


def _sel(tmp_path, content, mode=0o600, name="selector.json"):
    p = tmp_path / name
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    os.chmod(p, mode)
    return str(p)


def _load(env, n):
    # _load_server deja en os.environ lo que se le pasa y no restaura DELEGATE_GATEWAY_FILE:
    # se limpia aquí para no contaminar a los demás tests.
    try:
        return _load_server(env, f"server_t16_{n}")
    finally:
        os.environ.pop("DELEGATE_GATEWAY_FILE", None)


def test_litellm_valido(tmp_path):
    f = _sel(tmp_path, {"selector": "litellm"})
    assert _load({"DELEGATE_GATEWAY_FILE": f}, "ok_litellm").GATEWAY == "litellm"


def test_bifrost_valido_y_manda_sobre_el_env(tmp_path):
    f = _sel(tmp_path, {"selector": "bifrost"})
    mod = _load({"DELEGATE_GATEWAY_FILE": f, "DELEGATE_GATEWAY": "litellm", **BIF}, "ok_bif")
    assert mod.GATEWAY == "bifrost"


def test_archivo_litellm_manda_sobre_env_bifrost(tmp_path):
    f = _sel(tmp_path, {"selector": "litellm"})
    mod = _load({"DELEGATE_GATEWAY_FILE": f, "DELEGATE_GATEWAY": "bifrost", **BIF}, "ok_override")
    assert mod.GATEWAY == "litellm"


def test_bifrost_por_archivo_sigue_exigiendo_url(tmp_path):
    f = _sel(tmp_path, {"selector": "bifrost"})
    with pytest.raises(ValueError, match="DELEGATE_BIFROST_URL"):
        _load({"DELEGATE_GATEWAY_FILE": f}, "sin_url")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o400, 0o700])
def test_modo_distinto_de_0600_es_error(tmp_path, mode):
    f = _sel(tmp_path, {"selector": "litellm"}, mode=mode)
    with pytest.raises(ValueError, match="0600"):
        _load({"DELEGATE_GATEWAY_FILE": f}, f"modo_{mode:o}")


def test_symlink_es_error(tmp_path):
    real = _sel(tmp_path, {"selector": "litellm"}, name="real.json")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="DELEGATE_GATEWAY_FILE"):
        _load({"DELEGATE_GATEWAY_FILE": str(link)}, "symlink")


def test_directorio_no_es_archivo_regular(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    with pytest.raises(ValueError, match="DELEGATE_GATEWAY_FILE"):
        _load({"DELEGATE_GATEWAY_FILE": str(d)}, "dir")


def test_dueno_distinto_es_error(tmp_path, monkeypatch):
    # No se puede chown sin root: se simula que el uid efectivo es otro.
    f = _sel(tmp_path, {"selector": "litellm"})
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(f).st_uid + 1)
    with pytest.raises(ValueError, match="uid"):
        _load({"DELEGATE_GATEWAY_FILE": f}, "dueno")


def test_json_malo(tmp_path):
    f = _sel(tmp_path, "{not json")
    with pytest.raises(ValueError, match="JSON"):
        _load({"DELEGATE_GATEWAY_FILE": f}, "json_malo")


@pytest.mark.parametrize("body", [{"selector": "kong"}, {"selector": 1}, {"x": "litellm"}, ["litellm"], {"selector": "LiteLLM"}])
def test_valor_desconocido_o_forma_mala(tmp_path, body):
    f = _sel(tmp_path, body)
    with pytest.raises(ValueError, match="selector"):
        _load({"DELEGATE_GATEWAY_FILE": f}, "valor_%d" % abs(hash(json.dumps(body))))


def test_archivo_inexistente_es_error_no_fallback(tmp_path):
    with pytest.raises(ValueError, match="DELEGATE_GATEWAY_FILE"):
        _load({"DELEGATE_GATEWAY_FILE": str(tmp_path / "nope.json"), "DELEGATE_GATEWAY": "litellm"}, "nope")


def test_el_error_no_filtra_el_contenido(tmp_path):
    f = _sel(tmp_path, "SECRET-sk-bf-abc")
    with pytest.raises(ValueError) as ei:
        _load({"DELEGATE_GATEWAY_FILE": f}, "leak")
    assert "SECRET" not in str(ei.value)


def test_sin_archivo_comportamiento_de_siempre():
    assert _load({}, "unset").GATEWAY == "litellm"
    assert _load({"DELEGATE_GATEWAY": "bifrost", **BIF}, "unset_env").GATEWAY == "bifrost"
    with pytest.raises(ValueError, match="DELEGATE_GATEWAY"):
        _load({"DELEGATE_GATEWAY": "kong"}, "unset_kong")


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_archivo_env_blank_es_error_no_fallback(value):
    with pytest.raises(ValueError, match="DELEGATE_GATEWAY_FILE"):
        _load({"DELEGATE_GATEWAY_FILE": value, "DELEGATE_GATEWAY": "bifrost", **BIF}, "blank")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo unavailable")
def test_fifo_es_error_sin_bloquear(tmp_path):
    fifo = tmp_path / "selector.fifo"
    os.mkfifo(fifo, 0o600)
    env = {k: v for k, v in os.environ.items() if k not in _GATEWAY_ENV}
    env["DELEGATE_GATEWAY_FILE"] = str(fifo)
    result = subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "DELEGATE_GATEWAY_FILE debe ser un archivo regular" in result.stderr


@pytest.mark.parametrize("url", [None, "", " ", BIF["DELEGATE_BIFROST_URL"]])
def test_reload_valida_url_real(tmp_path, url):
    f = _sel(tmp_path, {"selector": "bifrost", "secret": "SECRET-must-not-print"})
    env = {k: v for k, v in os.environ.items() if k not in _GATEWAY_ENV}
    env["DELEGATE_GATEWAY_FILE"] = f
    if url is not None:
        env["DELEGATE_BIFROST_URL"] = url
    script = Path(__file__).resolve().parent.parent / "scripts" / "reload-delegate.sh"
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=30,
    )
    if url and url.strip():
        assert result.returncode == 0
        assert "next MCP spawn will use gateway: bifrost" in result.stdout
    else:
        assert result.returncode == 1
        assert "DELEGATE_BIFROST_URL" in result.stderr
        assert "next MCP spawn" not in result.stdout
    assert "SECRET-must-not-print" not in result.stdout + result.stderr
