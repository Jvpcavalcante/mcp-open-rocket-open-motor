"""otimizar_parametro e os algoritmos de analise.py."""
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from rocket_mcp import analise as an

FINS = "Trapezoidal fin set"


# ------------------------------------------------------------------------- algoritmos (sem JVM, síncronos)

def test_buscar_meta_bissecao():
    r = an.buscar_meta(lambda x: x ** 2, 0, 10, 20, tol_metrica=1e-4, max_avaliacoes=40)
    assert r["convergiu"] and r["valor"] == pytest.approx(20 ** 0.5, abs=1e-4)


def test_buscar_meta_fora_do_alcance():
    r = an.buscar_meta(lambda x: x, 0, 1, 5)
    assert not r["convergiu"] and r["valor"] == 1 and "fora do alcance" in r["mensagem"]
    assert len(r["historico"]) == 2  # não gasta avaliações à toa


def test_buscar_meta_respeita_limite():
    chamadas = []
    r = an.buscar_meta(lambda x: chamadas.append(x) or x, 0, 1, 0.123456789, tol_metrica=1e-12, max_avaliacoes=6)
    assert len(chamadas) == 6 and not r["convergiu"] and "Limite de 6" in r["mensagem"]


@pytest.mark.parametrize("maximizar, f, esperado", [
    (False, lambda x: (x - 3.3) ** 2, 3.3),
    (True, lambda x: -abs(x - 7.1), 7.1),
])
def test_buscar_extremo(maximizar, f, esperado):
    r = an.buscar_extremo(f, 0, 10, maximizar, max_avaliacoes=40)
    assert r["convergiu"] and r["valor"] == pytest.approx(esperado, abs=0.02)


def test_buscar_extremo_no_limite():
    r = an.buscar_extremo(lambda x: x, 0, 10, maximizar=True, max_avaliacoes=20)
    assert not r["convergiu"] and r["valor"] == 10 and "limite do intervalo" in r["mensagem"]


def test_metrica_invalida_vira_erro():
    with pytest.raises(ToolError, match="não pôde ser calculada"):
        an.buscar_meta(lambda x: None, 0, 1, 0.5)


# ------------------------------------------------------------------------------------- tool (via MCP)

async def _span(chamar):
    _, lido = await chamar("ler_foguete", arquivo_ork="foguete.ork")
    return next(c for c in lido["componentes"] if c["caminho"].endswith(FINS))["params"]["span_mm"]


@pytest.mark.anyio
async def test_margem_alvo_pela_aleta(chamar, workspace):
    span = await _span(chamar)
    erro, d = await chamar("otimizar_parametro", arquivo_ork="foguete.ork", parametro=f"{FINS}.span_mm",
                           minimo=span * 0.8, maximo=span * 2, metrica="margem_estatica_cal", meta=3.5,
                           tolerancia=0.01, salvar_como="otim/margem.ork")
    assert not erro, d
    assert d["convergiu"] and d["metrica_no_otimo"] == pytest.approx(3.5, abs=0.01)
    assert span < d["valor_otimo"] < span * 2
    erro, lido = await chamar("ler_foguete", arquivo_ork="otim/margem.ork")
    fins = next(c for c in lido["componentes"] if c["caminho"].endswith(FINS))
    assert fins["params"]["span_mm"] == pytest.approx(d["valor_otimo"], rel=1e-4)
    assert lido["estabilidade"]["margem_estatica_cal"] == pytest.approx(3.5, abs=0.01)


@pytest.mark.anyio
async def test_angulo_do_trilho_para_deriva_minima(chamar):
    erro, d = await chamar("otimizar_parametro", arquivo_ork="foguete.ork", parametro="angulo_trilho_graus",
                           minimo=0, maximo=20, metrica="distancia_pouso_m", meta="min",
                           opcoes_voo={"vento_medio_ms": 5, "vento_direcao_graus": 90, "turbulencia_pct": 0})
    assert not erro, d
    assert d["convergiu"] and 0 < d["valor_otimo"] < 20
    deriva = {h["valor"]: h["metrica"] for h in d["historico"]}
    assert d["metrica_no_otimo"] == min(deriva.values()) < deriva[0.0]


@pytest.mark.anyio
async def test_garganta_para_pressao_maxima(chamar):
    erro, d = await chamar("otimizar_parametro", arquivo_ric="motor.ric", parametro="nozzle.throat_mm",
                           minimo=3.0, maximo=4.0, metrica="pressao_max_MPa", meta=5.0, tolerancia=0.01,
                           salvar_como="otim/motor_5MPa.ric")
    assert not erro, d
    assert d["convergiu"] and d["metrica_no_otimo"] == pytest.approx(5.0, abs=0.01)
    erro, s = await chamar("simular_motor", arquivo_ric="otim/motor_5MPa.ric")
    assert s["pressao_max_MPa"] == pytest.approx(5.0, abs=0.01)


@pytest.mark.parametrize("kw, trecho", [
    ({"arquivo_ric": "motor.ric"}, "OU arquivo_ric"),
    ({"metrica": "altura"}, "metrica 'altura' inválida"),
    ({"meta": "media"}, "'min' ou 'max'"),
    ({"minimo": 5, "maximo": 5}, "minimo precisa ser menor"),
    ({"max_avaliacoes": 100}, "de 3 a 40"),
    ({"salvar_como": "foguete.ork"}, "já existe"),
])
@pytest.mark.anyio
async def test_otimizar_invalido(chamar, kw, trecho):
    args = {"arquivo_ork": "foguete.ork", "parametro": f"{FINS}.span_mm", "minimo": 10, "maximo": 50,
            "metrica": "margem_estatica_cal", "meta": 3.0} | kw
    erro, msg = await chamar("otimizar_parametro", **args)
    assert erro and trecho in msg


@pytest.mark.anyio
async def test_meta_inalcancavel_explica_o_alcance(chamar):
    erro, d = await chamar("otimizar_parametro", arquivo_ork="foguete.ork", parametro=f"{FINS}.span_mm",
                           minimo=10, maximo=50, metrica="margem_estatica_cal", meta=40)
    assert not erro, d
    assert not d["convergiu"] and "fora do alcance" in d["mensagem"]
