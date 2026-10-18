"""analisar_dispersao (Monte Carlo), fatores de massa/impulso/arrasto e estatísticas de analise.py."""
import csv
import math

import numpy as np
import pytest

from rocket_mcp import analise as an

SEM_INCERTEZA = {k: 0 for k in ("vento_ms", "vento_direcao_graus", "angulo_trilho_graus", "massa_pct",
                                "arrasto_pct", "impulso_pct")}


# ------------------------------------------------------------------------------ estatística (sem JVM)

def test_resumo_distribuicao():
    r = an.resumo_distribuicao([1, 2, 3, 4, None, 5])
    assert r["media"] == 3 and r["p50"] == 3 and r["min"] == 1 and r["max"] == 5
    assert r["desvio"] == pytest.approx(np.std([1, 2, 3, 4, 5], ddof=1), rel=1e-5)
    assert an.resumo_distribuicao([None]) is None


def test_elipse_95_de_uma_nuvem_conhecida():
    rng = np.random.default_rng(1)
    leste, norte = rng.normal(100, 10, 20000), rng.normal(-50, 2, 20000)  # alongada no eixo leste-oeste
    e = an.elipse_95(list(leste), list(norte))
    assert e["centro_leste_m"] == pytest.approx(100, abs=0.5) and e["centro_norte_m"] == pytest.approx(-50, abs=0.2)
    assert e["semieixo_maior_m"] == pytest.approx(10 * math.sqrt(5.991), rel=0.03)
    assert e["semieixo_menor_m"] == pytest.approx(2 * math.sqrt(5.991), rel=0.03)
    assert e["rumo_eixo_maior_graus"] in (89, 90, 91)
    dentro = ((leste - 100) / 10) ** 2 + ((norte + 50) / 2) ** 2 <= 5.991
    assert dentro.mean() == pytest.approx(0.95, abs=0.01)


def test_agrupar_alertas_ignora_numeros():
    g = an.agrupar_alertas([["Mach máximo 0.81 (> 0,8)"], ["Mach máximo 0.83 (> 0,8)", "Outro 1"], []])
    assert g[0]["tipo"].startswith("Mach máximo") and g[0]["fracao_voos"] == pytest.approx(0.667, abs=1e-3)
    assert {x["fracao_voos"] for x in g} == {0.667, 0.333}


# ------------------------------------------------------------------- fatores (direto no servidor, com JVM)

def _voo(servidor, fatores=None, **opcoes):
    doc, _ = servidor._carregar("foguete.ork")
    sim = servidor._simulacao(doc, 0)
    return servidor._voar(doc, sim, None, None, None, {"semente": 0} | opcoes, [], None, fatores)


def test_fator_de_massa_mantem_o_cg(servidor):
    """Massa estrutural × fator com o CG estrutural intacto (sem arredondamento: o foguete de exemplo pesa
    65 g e as massas devolvidas pelas tools têm resolução de 1 g)."""
    doc, _ = servidor._carregar("foguete.ork")  # espera a JVM antes do import Java
    from info.openrocket.core.masscalc import MassCalculator

    sim = servidor._simulacao(doc, 0)
    conf = doc.getRocket().getFlightConfiguration(sim.getFlightConfigurationId())
    antes = MassCalculator.calculateStructure(conf)
    m0, cg0, motor0 = float(antes.getMass()), float(antes.getCM().x), float(MassCalculator.calculateMotor(conf).getMass())
    servidor.orb.escalar_massa(doc, sim, 1.2)
    depois = MassCalculator.calculateStructure(conf)
    assert float(depois.getMass()) == pytest.approx(1.2 * m0, rel=1e-9)
    assert float(depois.getCM().x) == pytest.approx(cg0, abs=1e-9)
    assert float(MassCalculator.calculateMotor(conf).getMass()) == pytest.approx(motor0, rel=1e-12)  # motor intocado


@pytest.mark.parametrize("chave", ["impulso", "arrasto"])
def test_fatores_neutros_e_sentido(servidor, chave):
    base = _voo(servidor)["apogeu_m"]
    assert _voo(servidor, {chave: 1.0})["apogeu_m"] == base
    menor, maior = (_voo(servidor, {chave: f})["apogeu_m"] for f in (0.9, 1.1))
    if chave == "impulso":
        assert menor < base < maior
    else:  # mais arrasto, menos apogeu
        assert menor > base > maior


# ------------------------------------------------------------------------------------- tool (via MCP)

@pytest.mark.anyio
async def test_sem_incerteza_todos_os_voos_iguais_ao_nominal(chamar):
    erro, nominal = await chamar("simular_voo", arquivo_ork="foguete.ork", turbulencia_pct=0)
    assert not erro, nominal
    erro, d = await chamar("analisar_dispersao", arquivo_ork="foguete.ork", n_voos=5, incertezas=SEM_INCERTEZA,
                           opcoes_voo={"turbulencia_pct": 0})
    assert not erro, d
    ap = d["estatisticas"]["apogeu_m"]
    assert ap["desvio"] == 0 and ap["media"] == nominal["apogeu_m"]


@pytest.mark.anyio
async def test_dispersao_completa(chamar, workspace):
    progresso = []

    async def cb(p, total, msg):
        progresso.append(p)

    res = await chamar.cliente.call_tool("analisar_dispersao", {
        "arquivo_ork": "foguete.ork", "n_voos": 20, "semente": 3, "arquivo_csv": "mc/voos.csv",
        "opcoes_voo": {"vento_medio_ms": 4, "vento_direcao_graus": 90}}, progress_callback=cb)
    assert not res.is_error, res.content[0].text
    import json
    d = json.loads(res.content[0].text)
    assert d["n_voos"] == 20 and len(progresso) == 20
    ap = d["estatisticas"]["apogeu_m"]
    assert ap["desvio"] > 0 and ap["p5"] <= ap["p50"] <= ap["p95"]
    e = d["pouso_95"]
    assert e["semieixo_maior_m"] >= e["semieixo_menor_m"] > 0
    assert e["centro_leste_m"] < 0  # vento de leste: a nuvem de pouso fica a oeste
    assert set(d["probabilidades"]) >= {"estabilidade_saida_trilho_abaixo_1cal", "descida_final_acima_9ms"}
    assert len(d["piores_voos"]["maior_deriva"]) == 3
    with (workspace / "mc" / "voos.csv").open(encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    assert len(linhas) == 20 and "fator_arrasto" in linhas[0]

    # mesma semente, mesmo resultado; outra semente, outro resultado
    _, repetido = await chamar("analisar_dispersao", arquivo_ork="foguete.ork", n_voos=20, semente=3,
                               opcoes_voo={"vento_medio_ms": 4, "vento_direcao_graus": 90})
    assert repetido["estatisticas"] == d["estatisticas"]
    _, outro = await chamar("analisar_dispersao", arquivo_ork="foguete.ork", n_voos=20, semente=4,
                            opcoes_voo={"vento_medio_ms": 4, "vento_direcao_graus": 90})
    assert outro["estatisticas"]["apogeu_m"] != d["estatisticas"]["apogeu_m"]


@pytest.mark.parametrize("kw, trecho", [
    ({"n_voos": 2}, "de 5 a 200"),
    ({"incertezas": {"chuva": 1}}, "chave(s) desconhecida(s)"),
    ({"incertezas": {"massa_pct": -1}}, ">= 0"),
    ({"opcoes_voo": {"vento": 3}}, "chave(s) desconhecida(s)"),
])
@pytest.mark.anyio
async def test_dispersao_invalida(chamar, kw, trecho):
    erro, msg = await chamar("analisar_dispersao", arquivo_ork="foguete.ork", **kw)
    assert erro and trecho in msg
