"""Perfil de vento por altitude (modelo multinível do OpenRocket)."""
import pytest

pytestmark = pytest.mark.anyio

SEM_RAJADA = {"turbulencia_pct": 0, "angulo_trilho_graus": 0}


async def voo(chamar, **kw):
    erro, d = await chamar("simular_voo", arquivo_ork="foguete.ork", **kw)
    assert not erro, d
    return d


async def test_perfil_constante_equivale_ao_vento_medio(chamar):
    medio = await voo(chamar, vento_medio_ms=8, vento_direcao_graus=90, **SEM_RAJADA)
    perfil = await voo(chamar, perfil_vento=[[0, 8, 90], [2000, 8, 90]], **SEM_RAJADA)
    assert perfil["lancamento"]["modelo_vento"] == "MULTI_LEVEL"
    assert perfil["pouso"]["rumo_graus"] == pytest.approx(270, abs=5)  # direção em graus, "de onde vem"
    assert perfil["pouso"]["distancia_m"] == pytest.approx(medio["pouso"]["distancia_m"], rel=0.05)


async def test_vento_em_altitude_aumenta_a_deriva(chamar):
    calmo = await voo(chamar, perfil_vento=[[0, 0, 90], [100, 0, 90]], **SEM_RAJADA)
    alto = await voo(chamar, perfil_vento=[{"altitude_m": 0, "velocidade_ms": 0, "direcao_graus": 90},
                                           {"altitude_m": 30, "velocidade_ms": 15, "direcao_graus": 90}],
                     **SEM_RAJADA)
    assert alto["pouso"]["distancia_m"] > calmo["pouso"]["distancia_m"] + 20
    niveis = alto["lancamento"]["perfil_vento"]
    assert [(n["altitude_m"], n["velocidade_ms"], n["direcao_graus"]) for n in niveis] == [(0, 0, 90), (30, 15, 90)]
    assert alto["lancamento"]["perfil_vento_referencia"] == "AGL"


async def test_turbulencia_por_nivel(chamar):
    d = await voo(chamar, perfil_vento=[[0, 5, 0, 20], [100, 10, 0]], turbulencia_pct=5, angulo_trilho_graus=0)
    assert [n["turbulencia_pct"] for n in d["lancamento"]["perfil_vento"]] == [20, 5]


async def test_perfil_salvo_no_ork_sobrevive_a_leitura(chamar):
    """Regressão: os getters de vento das SimulationOptions trocam o modelo para o médio, e a leitura das
    condições usava um deles. Um .ork com vento multinível virava vento médio antes de simular."""
    d = await voo(chamar, perfil_vento=[[0, 2, 45], [50, 9, 45]], perfil_vento_referencia="MSL",
                  salvar_como="vento/perfil.ork")
    assert d["arquivo_salvo"] == "vento/perfil.ork"
    erro, relido = await chamar("simular_voo", arquivo_ork="vento/perfil.ork")
    assert not erro, relido
    assert relido["lancamento"]["modelo_vento"] == "MULTI_LEVEL"
    assert relido["lancamento"]["perfil_vento_referencia"] == "MSL"
    assert [n["velocidade_ms"] for n in relido["lancamento"]["perfil_vento"]] == [2, 9]


async def test_varrer_perfis(chamar):
    perfis = [[[0, 0, 90], [100, 0, 90]], [[0, 0, 90], [30, 10, 90]], [[0, 0, 90], [30, 20, 90]]]
    erro, d = await chamar("varrer_parametro", arquivo_ork="foguete.ork", parametro="perfil_vento",
                           valores=perfis, opcoes_voo=SEM_RAJADA)
    assert not erro, d
    distancias = [linha["pouso"]["distancia_m"] for linha in d["linhas"]]
    assert distancias == sorted(distancias)


@pytest.mark.parametrize("kw, trecho", [
    ({"perfil_vento": [[0, 5, 90]], "vento_medio_ms": 3}, "não os dois"),
    ({"perfil_vento": [[0, 5]]}, "[altitude_m, velocidade_ms, direcao_graus]"),
    ({"perfil_vento": [[0, "forte", 90]]}, "esperado um número"),
    ({"perfil_vento": [[0, -1, 90]]}, "não pode ser negativa"),
    ({"perfil_vento": [[0, 5, 90], [0, 6, 90]]}, "altitudes repetidas"),
    ({"perfil_vento": [{"alt": 0, "velocidade_ms": 5, "direcao_graus": 90}]}, "chave(s) desconhecida(s)"),
    ({"perfil_vento": []}, "de 1 a 50 níveis"),
    ({"perfil_vento": [[0, 5, 90]], "perfil_vento_referencia": "TOPO"}, "'AGL'"),
    ({"perfil_vento_referencia": "AGL"}, "só vale junto com perfil_vento"),
])
async def test_perfil_invalido(chamar, kw, trecho):
    erro, msg = await chamar("simular_voo", arquivo_ork="foguete.ork", **kw)
    assert erro and trecho in msg
