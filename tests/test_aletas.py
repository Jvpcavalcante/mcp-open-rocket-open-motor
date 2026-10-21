"""Estudo de aletas: geometria derivada (sem JVM), parâmetros novos de editar_foguete, analisar_aerodinamica
e comparar_aletas, sobre o foguete de exemplo do OpenRocket (3 aletas trapezoidais, sem massa sobrescrita)."""
import csv
import math

import pytest

from rocket_mcp import aletas

FINS = "Trapezoidal fin set"


# -------------------------------------------------------------------------------- geometria (sem JVM)

def test_geometria_trapezio_hemera():
    """Aletas do Hemera: cr 270, ct 130, s 180, sweep 180 mm."""
    g = aletas.geometria_trapezio(0.27, 0.13, 0.18, 0.18, 0.005)
    assert g["area_mm2"] == pytest.approx(36000)
    assert g["alongamento"] == pytest.approx(0.9)
    assert g["afilamento"] == pytest.approx(13 / 27, abs=1e-4)
    assert g["enflechamento_bordo_ataque_deg"] == pytest.approx(45)
    # meia corda: (sweep + ct/2 - cr/2) / s = 0,11 / 0,18
    assert g["enflechamento_meia_corda_deg"] == pytest.approx(math.degrees(math.atan(0.11 / 0.18)), abs=0.01)
    assert g["corda_media_aerodinamica_mm"] == pytest.approx(208.17, abs=0.01)
    assert g["espessura_relativa_raiz"] == pytest.approx(0.005 / 0.27, abs=1e-5)


def test_geometria_retangular_e_delta():
    ret = aletas.geometria_trapezio(0.1, 0.1, 0.05, 0.0, 0.003)
    assert ret["corda_media_aerodinamica_mm"] == pytest.approx(100)
    assert ret["y_cma_mm"] == pytest.approx(25)
    assert ret["enflechamento_meia_corda_deg"] == pytest.approx(0)
    delta = aletas.geometria_trapezio(0.1, 0.0, 0.1, 0.1, 0.003)  # bordo de fuga reto
    assert delta["corda_media_aerodinamica_mm"] == pytest.approx(200 / 3, abs=0.01)
    assert delta["y_cma_mm"] == pytest.approx(100 / 3, abs=0.01)
    assert delta["enflechamento_bordo_fuga_deg"] == pytest.approx(0)


# --------------------------------------------------------------------------- parâmetros de editar_foguete


def comp(lido, final):
    return next(c for c in lido["componentes"] if c["caminho"].endswith(final))


@pytest.mark.anyio
async def test_ler_foguete_traz_geometria_e_parametros_novos(chamar):
    erro, d = await chamar("ler_foguete", arquivo_ork="foguete.ork")
    assert not erro, d
    fins = comp(d, FINS)
    assert {"sweepAngle_deg", "material", "materialDensity_kg_m3", "finish", "filletRadius_mm",
            "tabHeight_mm", "tabLength_mm"} <= fins["params"].keys()
    p, g = fins["params"], fins["geometria"]
    assert math.tan(math.radians(p["sweepAngle_deg"])) * p["span_mm"] == pytest.approx(p["sweepLength_mm"], rel=1e-4)
    assert g["area_mm2"] == pytest.approx((p["rootChord_mm"] + p["tipChord_mm"]) / 2 * p["span_mm"], rel=1e-4)
    assert "Aluminum" in d["opcoes"]["material"] and "POLISHED" in d["opcoes"]["finish"]


@pytest.mark.anyio
async def test_angulo_de_enflechamento_aplicado_depois_do_span(chamar):
    erro, d = await chamar("editar_foguete", arquivo_ork="foguete.ork", salvar_como="aletas/enflechada.ork",
                           alteracoes={f"{FINS}.sweepAngle_deg": 30, f"{FINS}.span_mm": 60})
    assert not erro, d
    erro, lido = await chamar("ler_foguete", arquivo_ork="aletas/enflechada.ork")
    p = comp(lido, FINS)["params"]
    assert p["span_mm"] == 60 and p["sweepAngle_deg"] == pytest.approx(30)
    assert p["sweepLength_mm"] == pytest.approx(60 * math.tan(math.radians(30)), rel=1e-4)


@pytest.mark.anyio
async def test_material_e_densidade_mudam_a_massa(chamar):
    _, lido = await chamar("ler_foguete", arquivo_ork="foguete.ork")
    antes = comp(lido, FINS)
    erro, d = await chamar("editar_foguete", arquivo_ork="foguete.ork", salvar_como="aletas/aluminio.ork",
                           alteracoes={FINS: {"materialDensity_kg_m3": 2000, "material": "aluminum"}})
    assert not erro, d  # material aplicado antes da densidade, mesmo vindo depois
    erro, lido = await chamar("ler_foguete", arquivo_ork="aletas/aluminio.ork")
    depois = comp(lido, FINS)
    assert depois["params"]["material"] == "Aluminum" and depois["params"]["materialDensity_kg_m3"] == 2000
    razao = 2000 / antes["params"]["materialDensity_kg_m3"]
    assert depois["massa_material_kg"] == pytest.approx(antes["massa_material_kg"] * razao, rel=0.02)


@pytest.mark.anyio
@pytest.mark.parametrize("alteracoes, trecho", [
    ({f"{FINS}.material": "Unobtainium"}, "não está no banco"),
    ({f"{FINS}.material": "Alumin"}, "Parecidos: ['Aluminum'"),
    ({f"{FINS}.finish": "BRILHANTE"}, "Opções"),
    ({f"{FINS}.sweepAngle_deg": 30, f"{FINS}.sweepLength_mm": 10}, "OU sweepAngle_deg"),
    ({FINS: {"sweepAngle_deg": 30, "sweepLength_mm": 10}}, "OU sweepAngle_deg"),
])
async def test_parametros_de_aleta_invalidos(chamar, alteracoes, trecho):
    erro, msg = await chamar("editar_foguete", arquivo_ork="foguete.ork", salvar_como="aletas/invalido.ork",
                             alteracoes=alteracoes)
    assert erro and trecho in msg, msg


# --------------------------------------------------------------------------------- analisar_aerodinamica

@pytest.mark.anyio
async def test_analisar_aerodinamica(chamar):
    erro, d = await chamar("analisar_aerodinamica", arquivo_ork="foguete.ork", machs=[0.3, 0.8, 1.5])
    assert not erro, d
    _, est = await chamar("abrir_foguete", arquivo_ork="foguete.ork")
    m03 = d["curvas"][0]
    # mesmo CP e mesma margem da estabilidade estática (Barrowman a Mach 0,3)
    assert m03["cp_mm"] == pytest.approx(est["estabilidade"]["cp_mm"], abs=0.2)
    assert m03["margem_estatica_cal"] == pytest.approx(est["estabilidade"]["margem_estatica_cal"], abs=0.01)
    assert m03["cd"] == pytest.approx(m03["cd_atrito"] + m03["cd_pressao"] + m03["cd_base"], rel=1e-3)
    caminho = f"Sustainer/Body tube/{FINS}"
    for linha in d["curvas"]:
        assert 0.5 < linha["aletas"][caminho]["fracao_cna"] < 1  # as aletas dão a maior parte do CNα
    assert d["curvas"][1]["aletas"][caminho]["cna"] > m03["aletas"][caminho]["cna"]  # subsônico: Prandtl-Glauert
    assert d["componentes"]["mach"] == 0.3 and d["componentes"]["lista"][0]["caminho"] == caminho
    assert d["aletas"][0]["caminho"] == caminho and "geometria" in d["aletas"][0]


@pytest.mark.anyio
async def test_espessura_e_acabamento_so_mudam_o_arrasto(chamar):
    _, base = await chamar("analisar_aerodinamica", arquivo_ork="foguete.ork", machs=[0.5], mach_componentes=None)
    _, grossa = await chamar("analisar_aerodinamica", arquivo_ork="foguete.ork", machs=[0.5],
                             alteracoes={f"{FINS}.thickness_mm": 6})
    _, polida = await chamar("analisar_aerodinamica", arquivo_ork="foguete.ork", machs=[0.5],
                             alteracoes={f"{FINS}.finish": "MIRROR"})
    b, g, p = base["curvas"][0], grossa["curvas"][0], polida["curvas"][0]
    assert g["cna"] == pytest.approx(b["cna"]) and g["cd"] > b["cd"]  # Barrowman: CNα não depende de t
    assert p["cd_atrito"] <= b["cd_atrito"]
    assert grossa["alteracoes"][0]["depois"] == 6 and "componentes" not in base


@pytest.mark.anyio
@pytest.mark.parametrize("kw, trecho", [
    ({"machs": [0.3, 7]}, "machs"),
    ({"machs": []}, "machs"),
    ({"aoa_graus": 45}, "aoa_graus"),
])
async def test_analisar_aerodinamica_invalido(chamar, kw, trecho):
    erro, msg = await chamar("analisar_aerodinamica", arquivo_ork="foguete.ork", **kw)
    assert erro and trecho in msg


# --------------------------------------------------------------------------------------- comparar_aletas

@pytest.mark.anyio
async def test_comparar_aletas(chamar, workspace):
    erro, d = await chamar("comparar_aletas", arquivo_ork="foguete.ork", material_aletas="aluminio",
                           arquivo_csv="aletas/comparacao.csv",
                           variantes=[{"nome": "maior", "span_mm": 50}, {"count": 4},
                                      {"nome": "nariz", "Nose cone.length_mm": 80}])
    assert not erro, d
    original, maior, quatro, nariz = d["linhas"]
    assert [x["nome"] for x in d["linhas"]] == ["original", "maior", "variante 2", "nariz"]
    assert original["alteracoes"] == [] and maior["alteracoes"][0]["depois"] == 50
    for linha in (maior, quatro):
        assert linha["aerodinamica"]["M0.3"]["cna_aletas"] > original["aerodinamica"]["M0.3"]["cna_aletas"]
        assert linha["estabilidade"]["margem_estatica_cal"] > original["estabilidade"]["margem_estatica_cal"]
        assert linha["aletas"]["massa_material_kg"] > original["aletas"]["massa_material_kg"]
    assert quatro["aletas"]["params"]["count"] == 4
    assert maior["voo"]["apogeu_m"] < original["voo"]["apogeu_m"]  # mais arrasto e massa
    assert original["voo"]["flutter"]["fator_min"] > 1
    with (workspace / "aletas" / "comparacao.csv").open(encoding="utf-8") as f:
        tabela = list(csv.DictReader(f))
    assert len(tabela) == 4 and float(tabela[1]["aletas.params.span_mm"]) == 50
    assert {"aerodinamica.M0.8.cd_aletas", "voo.apogeu_m", "aletas.geometria.alongamento"} <= tabela[0].keys()


@pytest.mark.anyio
async def test_comparar_aletas_compensa_massa_sobrescrita(chamar):
    erro, d = await chamar("editar_foguete", arquivo_ork="foguete.ork", salvar_como="aletas/override.ork",
                           alteracoes={"Body tube.overrideMass_kg": 0.05,
                                       "Body tube.overrideMassIncludesChildren": True})
    assert not erro, d
    variante = [{"nome": "maior", "span_mm": 60}]
    _, com = await chamar("comparar_aletas", arquivo_ork="aletas/override.ork", variantes=variante, simular=False)
    _, sem = await chamar("comparar_aletas", arquivo_ork="aletas/override.ork", variantes=variante, simular=False,
                          compensar_massa=False)
    delta_aletas = com["linhas"][1]["aletas"]["massa_material_kg"] - com["linhas"][0]["aletas"]["massa_material_kg"]
    massa = lambda r, i: r["linhas"][i]["estabilidade"]["massa_decolagem_kg"]  # noqa: E731
    assert massa(com, 1) - massa(com, 0) == pytest.approx(delta_aletas, abs=0.002)
    assert any("ajustada" in e for e in com["linhas"][1]["efeitos"])
    assert massa(sem, 1) == pytest.approx(massa(sem, 0))
    assert any("está sobrescrita" in e for e in sem["linhas"][1]["efeitos"])
    assert "voo" not in com["linhas"][0]


@pytest.mark.anyio
@pytest.mark.parametrize("variantes, trecho", [
    ([], "de 1 a 30"),
    ([{"spam_mm": 3}], "não é parâmetro"),
    ([{"nome": "vazia"}], "nenhuma alteração"),
    ([{"nome": "ruim", "span_mm": -5}], "Variante 'ruim'"),
])
async def test_comparar_aletas_invalido(chamar, variantes, trecho):
    erro, msg = await chamar("comparar_aletas", arquivo_ork="foguete.ork", variantes=variantes)
    assert erro and trecho in msg, msg


@pytest.mark.anyio
async def test_series_voo_trazem_atmosfera(chamar, workspace):
    erro, d = await chamar("exportar_series_voo", arquivo_ork="foguete.ork", arquivo_csv="aletas/series.csv")
    assert not erro, d
    with (workspace / "aletas" / "series.csv").open(encoding="utf-8") as f:
        linha = next(csv.DictReader(f))
    assert float(linha["air_pressure_Pa"]) == pytest.approx(101325, rel=0.05)
    assert float(linha["speed_of_sound_ms"]) == pytest.approx(340, rel=0.03)
