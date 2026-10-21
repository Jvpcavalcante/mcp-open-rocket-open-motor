"""Fixtures: workspace temporário (motor.ric, foguete.ork e dois_estagios.ork tirados de vendor/, e o
fins_config.yaml de exemplo) e o servidor em memória.

O server.py lê ROCKET_MCP_WORKSPACE na importação, então a variável é definida aqui, antes de qualquer
import do servidor. A JVM é única por processo: sobe uma vez para a sessão inteira, em segundo plano,
como em produção."""
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
VENDOR = RAIZ / "vendor"
MOTOR_TINY = VENDOR / "openMotor" / "test" / "data" / "regression" / "tiny" / "motor.ric"
JAR = VENDOR / "OpenRocket-24.12.jar"
ORK_EXEMPLO = "datafiles/examples/A simple model rocket.ork"
ORK_DOIS_ESTAGIOS = "datafiles/examples/Two stage high power rocket.ork"
FINS_CONFIG = RAIZ / "workspace_teste" / "fins_config.yaml"

WORKSPACE = Path(tempfile.mkdtemp(prefix="rocket_mcp_ws_")).resolve()
os.environ["ROCKET_MCP_WORKSPACE"] = str(WORKSPACE)


def pytest_sessionstart(session):
    if not (MOTOR_TINY.exists() and JAR.exists()):
        pytest.exit("vendor/ incompleto: rode `python scripts/preparar_vendor.py`", returncode=2)
    shutil.copy(MOTOR_TINY, WORKSPACE / "motor.ric")
    with zipfile.ZipFile(JAR) as z:
        (WORKSPACE / "foguete.ork").write_bytes(z.read(ORK_EXEMPLO))
        (WORKSPACE / "dois_estagios.ork").write_bytes(z.read(ORK_DOIS_ESTAGIOS))
    shutil.copy(FINS_CONFIG, WORKSPACE / "fins_config.yaml")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(WORKSPACE, ignore_errors=True)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def workspace() -> Path:
    return WORKSPACE


@pytest.fixture(scope="session")
def servidor():
    from rocket_mcp import server

    assert server.WORKSPACE == WORKSPACE
    server.orb.iniciar_em_segundo_plano(server.MOTORES)
    return server


@pytest.fixture
async def chamar(servidor):
    """chamar(nome, **args) -> (erro, valor). valor é o dict devolvido pela tool, ou o texto do erro.
    O cliente fica em chamar.cliente para chamadas com progress_callback."""
    from mcp import Client

    async with Client(servidor.mcp) as cliente:

        async def _chamar(nome, **args):
            res = await cliente.call_tool(nome, args)
            texto = res.content[0].text if res.content else ""
            return res.is_error, (texto if res.is_error else json.loads(texto))

        _chamar.cliente = cliente
        yield _chamar
