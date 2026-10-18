"""ler_foguete / editar_foguete / varrer_parametro / exportar_series_* e as melhorias do simular_voo,
sobre o foguete de exemplo do OpenRocket ('A simple model rocket')."""
import csv

import numpy as np
import pytest

pytestmark = pytest.mark.anyio

FINS = "Trapezoidal fin set"


def comp(lido, final):
    return next(c for c in lido["componentes"] if c["caminho"].endswith(final))


async def editar(chamar, salvar_como, alteracoes, **kw):
    return await chamar("editar_foguete", arquivo_ork=kw.pop("arquivo_ork", "foguete.ork"),
                        salvar_como=salvar_como, alteracoes=alteracoes, **kw)


# ------------------------------------------------------------------------------------------- leitura

async def test_ler_foguete(chamar):
    erro, d = await chamar("ler_foguete", arquivo_ork="foguete.ork")
    assert not erro, d
    caminhos = {c["caminho"]: c for c in d["componentes"]}
    fins = caminhos[f"Sustainer/Body tube/{FINS}"]
    assert fins["tipo"] == "TrapezoidFinSet" and fins["params"]["count"] == 3
    assert {"rootChord_mm", "tipChord_mm", "span_mm", "sweepLength_mm", "overrideMass_kg"} <= fins["params"].keys()
    assert fins["params"]["overrideMass_kg"] is None
    # irmãos com o mesmo nome no mesmo pai ganham índice
    assert {"Sustainer/Body tube/Centering ring[0]", "Sustainer/Body tube/Centering ring[1]"} <= caminhos.keys()
    assert caminhos["Sustainer/Body tube/Parachute"]["params"]["deployEvent"] == "EJECTION"
    assert d["estabilidade"]["margem_estatica_cal"] == pytest.approx(2.87, abs=0.05)
    assert d["estabilidade"]["com_motor"]
    assert "APOGEE" in d["opcoes"]["deployEvent"] and "OGIVE" in d["opcoes"]["shape"]


async def test_abrir_foguete_traz_estabilidade(chamar):
    erro, d = await chamar("abrir_foguete", arquivo_ork="foguete.ork")
    assert not erro, d
    assert d["estabilidade"]["cp_mm"] > d["estabilidade"]["cg_mm"]


# -------------------------------------------------------------------------------------------- edição

async def test_editar_grava_em_arquivo_novo(chamar, workspace):
    original = (workspace / "foguete.ork").read_bytes()
    erro, msg = await chamar("editar_foguete", arquivo_ork="foguete.ork", alteracoes={f"{FINS}.count": 4})
    assert erro and "salvar_como" in msg  # sem salvar_como nada é gravado
    erro, d = await editar(chamar, "edit/quatro_aletas.ork", {f"{FINS}.count": 4})
    assert not erro, d
    assert d["arquivo_ork"] == "edit/quatro_aletas.ork"
    assert d["alteracoes"] == [{"parametro": f"Sustainer/Body tube/{FINS}.count", "antes": 3, "depois": 4}]
    assert (workspace / "foguete.ork").read_bytes() == original
    erro, lido = await chamar("ler_foguete", arquivo_ork="edit/quatro_aletas.ork")
    assert comp(lido, FINS)["params"]["count"] == 4
    erro, msg = await editar(chamar, "edit/quatro_aletas.ork", {f"{FINS}.count": 5})
    assert erro and "já existe" in msg


async def test_aletas_maiores_aumentam_margem_e_massa(chamar):
    _, lido = await chamar("ler_foguete", arquivo_ork="foguete.ork")
    span = comp(lido, FINS)["params"]["span_mm"]
    erro, d = await editar(chamar, "edit/aletas_maiores.ork", {f"{FINS}.span_mm": span * 1.5})
    assert not erro, d
    antes, depois = d["estabilidade"]["antes"], d["estabilidade"]["depois"]
    assert depois["margem_estatica_cal"] > antes["margem_estatica_cal"]
    assert depois["massa_decolagem_kg"] > antes["massa_decolagem_kg"]  # sem massa sobrescrita no exemplo
    assert d["efeitos"] == []


async def test_lastro_cria_e_remove(chamar):
    erro, d = await editar(chamar, "edit/lastro.ork", {"Nose cone/Lastro": {"mass_kg": 0.010, "position_mm": 20}})
    assert not erro, d
    antes, depois = d["estabilidade"]["antes"], d["estabilidade"]["depois"]
    assert depois["massa_decolagem_kg"] - antes["massa_decolagem_kg"] == pytest.approx(0.010, abs=0.0015)
    assert depois["cg_mm"] < antes["cg_mm"]  # massa no nariz puxa o CG para frente
    assert depois["margem_estatica_cal"] > antes["margem_estatica_cal"]

    erro, d2 = await editar(chamar, "edit/sem_lastro.ork", {"Lastro": None}, arquivo_ork="edit/lastro.ork")
    assert not erro, d2
    assert d2["estabilidade"]["depois"]["massa_decolagem_kg"] == pytest.approx(antes["massa_decolagem_kg"], abs=1e-3)


async def test_massa_sobrescrita_e_avisada(chamar):
    erro, d = await editar(chamar, "edit/override.ork",
                           {"Body tube.overrideMass_kg": 0.05, "Body tube.length_mm": 350})
    assert not erro, d
    assert any("Body tube" in e and "está sobrescrita" in e for e in d["efeitos"])
    erro, d2 = await editar(chamar, "edit/sem_override.ork", {"Body tube.overrideMass_kg": None},
                            arquivo_ork="edit/override.ork")
    assert not erro, d2
    assert d2["alteracoes"][0]["antes"] == 0.05 and d2["alteracoes"][0]["depois"] is None


async def test_abertura_muda_so_na_simulacao_escolhida(chamar):
    erro, d = await editar(chamar, "edit/abertura.ork", indice_simulacao=0,
                           alteracoes={"Parachute.deployEvent": "altitude", "Parachute.deployAltitude_m": 150})
    assert not erro, d
    _, sim0 = await chamar("ler_foguete", arquivo_ork="edit/abertura.ork", indice_simulacao=0)
    _, sim1 = await chamar("ler_foguete", arquivo_ork="edit/abertura.ork", indice_simulacao=1)
    assert comp(sim0, "Parachute")["params"]["deployEvent"] == "ALTITUDE"
    assert comp(sim0, "Parachute")["params"]["deployAltitude_m"] == 150
    assert comp(sim1, "Parachute")["params"]["deployEvent"] == "EJECTION"


@pytest.mark.parametrize("alteracoes, trecho", [
    ({f"{FINS}.diameter_mm": 3}, "não tem o parâmetro 'diameter_mm'"),
    ({"Nose cone.shape": "PONTUDO"}, "Opções"),
    ({f"{FINS}.count": 2.5}, "esperado um inteiro"),
    ({f"{FINS}.count": 0}, "abaixo do mínimo"),
    ({"Centering ring.length_mm": 3}, "ambíguo"),
    ({"Asa.length_mm": 3}, "não encontrado"),
    ({"Nose cone.length_mm": None}, "não aceita null"),
    ({"Lastro": {"mass_kg": 0.1}}, "informe o pai"),
    ({"Parachute": None}, "Só massas"),
    ({}, "ao menos uma alteração"),
])
async def test_editar_invalido_nao_grava(chamar, workspace, alteracoes, trecho):
    erro, msg = await editar(chamar, "edit/invalido.ork", alteracoes)
    assert erro and trecho in msg
    assert not (workspace / "edit" / "invalido.ork").exists()


async def test_irmao_indexado(chamar):
    erro, d = await editar(chamar, "edit/anel.ork", {"Centering ring[1].length_mm": 3})
    assert not erro, d
    assert d["alteracoes"][0]["parametro"] == "Sustainer/Body tube/Centering ring[1].length_mm"


# ------------------------------------------------------------------------------------------------ voo

async def test_simular_voo_repetivel_e_com_estabilidade(chamar):
    erro, a = await chamar("simular_voo", arquivo_ork="foguete.ork")
    assert not erro, a
    _, b = await chamar("simular_voo", arquivo_ork="foguete.ork")
    assert a["apogeu_m"] == b["apogeu_m"]  # semente fixa por padrão (inclusive na turbulência do vento)
    assert a["estabilidade"]["margem_estatica_cal"] > 1
    assert a["estabilidade_voo"]["saida_trilho_cal"] is not None
    assert isinstance(a["alertas_projeto"], list)


async def test_semente_fixa_a_turbulencia_do_vento(chamar):
    """A 24.12 sorteia a semente do ruído do vento por conta própria; _fixar_semente a grava por reflexão."""
    kw = {"arquivo_ork": "foguete.ork", "vento_medio_ms": 6, "turbulencia_pct": 40, "semente": 7}
    apogeus = {(await chamar("simular_voo", **kw))[1]["apogeu_m"] for _ in range(3)}
    assert len(apogeus) == 1


async def test_opcoes_de_lancamento(chamar):
    _, base = await chamar("simular_voo", arquivo_ork="foguete.ork", semente=1)
    _, vento = await chamar("simular_voo", arquivo_ork="foguete.ork", semente=1, vento_medio_ms=8,
                            vento_direcao_graus=90, turbulencia_pct=20)
    _, quente = await chamar("simular_voo", arquivo_ork="foguete.ork", semente=1, temperatura_c=35, pressao_hpa=900)
    assert vento["apogeu_m"] < base["apogeu_m"]  # cata-vento
    assert quente["apogeu_m"] > base["apogeu_m"]  # ar menos denso


async def test_simular_voo_salvar_como_nao_sobrescreve(chamar):
    erro, msg = await chamar("simular_voo", arquivo_ork="foguete.ork", salvar_como="foguete.ork")
    assert erro and "já existe" in msg


async def test_indice_simulacao_invalido(chamar):
    erro, msg = await chamar("simular_voo", arquivo_ork="foguete.ork", indice_simulacao=9)
    assert erro and "índices 0 a 4" in msg


async def test_montagem_fantasma_nao_acusa_encaixe(chamar):
    erro, d = await editar(chamar, "edit/fantasma.ork", {"Inner Tube.length_mm": 0})
    assert not erro, d
    erro, m = await chamar("exportar_motor_eng", arquivo_ric="motor.ric", diametro_mm=29, comprimento_mm=150,
                           massa_hardware_kg=0.12)
    assert not erro, m
    erro, v = await chamar("simular_voo", arquivo_ork="edit/fantasma.ork", motor_eng=m["arquivo_eng"],
                           atraso_ejecao_s=5)
    assert not erro, v
    assert any("componente fantasma" in a for a in v["avisos_validacao"])
    assert not any("NÃO cabe" in a or "overhang" in a for a in v["avisos_validacao"])


async def test_motor_copiado_para_a_pasta_e_registrado(chamar, servidor, workspace):
    erro, m = await chamar("exportar_motor_eng", arquivo_ric="motor.ric", diametro_mm=29, comprimento_mm=150,
                           massa_hardware_kg=0.12)
    assert not erro, m
    copia = workspace / "motores" / "Copia-Manual.eng"
    texto = (workspace / m["arquivo_eng"]).read_text(encoding="utf-8")
    copia.write_text(texto.replace(m["nome_motor"], "Copia-Manual", 1), encoding="utf-8")
    erro, _ = await chamar("abrir_foguete", arquivo_ork="foguete.ork")  # sincroniza motores/
    assert not erro
    assert str(copia.resolve()) in servidor.orb._motores_registrados


async def test_ork_corrompido_vira_erro_legivel(chamar, workspace):
    (workspace / "corrompido.ork").write_text("isto não é um .ork", encoding="utf-8")
    erro, msg = await chamar("abrir_foguete", arquivo_ork="corrompido.ork")
    assert erro and "Erro do OpenRocket" in msg


# ------------------------------------------------------------------------------------------- análise

async def test_varrer_parametro(chamar, workspace):
    _, lido = await chamar("ler_foguete", arquivo_ork="foguete.ork")
    span = comp(lido, FINS)["params"]["span_mm"]
    arquivos = set(workspace.rglob("*"))
    erro, d = await chamar("varrer_parametro", arquivo_ork="foguete.ork", parametro=f"{FINS}.span_mm",
                           valores=[span * 0.8, span, span * 1.3])
    assert not erro, d
    margens = [linha["margem_estatica_cal"] for linha in d["linhas"]]
    assert len(margens) == 3 and margens == sorted(margens) and margens[0] < margens[-1]
    assert set(workspace.rglob("*")) == arquivos  # nada gravado


async def test_convencoes_de_direcao(chamar):
    """Verificado: o vento é 'de onde vem' (90 = de leste -> deriva para oeste); o trilho é 'para onde aponta'."""
    _, vento = await chamar("simular_voo", arquivo_ork="foguete.ork", vento_medio_ms=8, vento_direcao_graus=90,
                            turbulencia_pct=0, angulo_trilho_graus=0)
    _, trilho = await chamar("simular_voo", arquivo_ork="foguete.ork", vento_medio_ms=0, turbulencia_pct=0,
                             angulo_trilho_graus=10, direcao_trilho_graus=90)
    assert vento["pouso"]["rumo_graus"] == pytest.approx(270, abs=5)
    assert trilho["pouso"]["rumo_graus"] == pytest.approx(90, abs=5)


async def test_direcao_do_trilho_desliga_contra_o_vento(chamar):
    """O .ork de exemplo (e o Hemera) vêm com 'Launch into wind' ligado, que ignorava a direção do trilho."""
    _, d = await chamar("simular_voo", arquivo_ork="foguete.ork", vento_medio_ms=0, turbulencia_pct=0,
                        angulo_trilho_graus=10, direcao_trilho_graus=270)
    assert d["pouso"]["rumo_graus"] == pytest.approx(270, abs=5)
    assert d["lancamento"]["lancar_contra_vento"] is False
    assert d["lancamento"]["direcao_trilho_efetiva_graus"] == 270
    assert any("contra o vento" in n for n in d["notas"])
    # pedido explícito vence: contra o vento, o trilho aponta para de onde o vento vem
    _, c = await chamar("simular_voo", arquivo_ork="foguete.ork", vento_medio_ms=6, vento_direcao_graus=180,
                        angulo_trilho_graus=10, direcao_trilho_graus=270, lancar_contra_vento=True)
    assert c["lancamento"]["lancar_contra_vento"] is True
    assert c["lancamento"]["direcao_trilho_efetiva_graus"] == 180


async def test_varrer_direcao_do_trilho(chamar):
    erro, d = await chamar("varrer_parametro", arquivo_ork="foguete.ork", parametro="direcao_trilho_graus",
                           valores=[0, 90, 180, 270], opcoes_voo={"vento_medio_ms": 0, "turbulencia_pct": 0,
                                                                  "angulo_trilho_graus": 10})
    assert not erro, d
    assert [linha["pouso"]["rumo_graus"] for linha in d["linhas"]] == [0, 90, 180, 270]
    assert any("contra o vento" in e for e in d["efeitos"])


async def test_varrer_vento(chamar):
    erro, d = await chamar("varrer_parametro", arquivo_ork="foguete.ork", parametro="vento_medio_ms",
                           valores=[0, 4, 8], opcoes_voo={"vento_direcao_graus": 90, "turbulencia_pct": 0,
                                                          "angulo_trilho_graus": 0})
    assert not erro, d
    assert d["tipo"] == "condicao_lancamento"
    distancias = [linha["pouso"]["distancia_m"] for linha in d["linhas"]]
    assert distancias == sorted(distancias) and distancias[-1] > distancias[0] + 20
    assert d["linhas"][-1]["pouso"]["rumo_graus"] == pytest.approx(270, abs=5)


async def test_varrer_angulo_do_trilho(chamar):
    erro, d = await chamar("varrer_parametro", arquivo_ork="foguete.ork", parametro="angulo_trilho_graus",
                           valores=[0, 10, 20], opcoes_voo={"vento_medio_ms": 0, "turbulencia_pct": 0,
                                                            "direcao_trilho_graus": 90})
    assert not erro, d
    apogeus = [linha["apogeu_m"] for linha in d["linhas"]]
    distancias = [linha["pouso"]["distancia_m"] for linha in d["linhas"]]
    assert apogeus == sorted(apogeus, reverse=True)  # mais inclinado, mais baixo
    assert distancias == sorted(distancias)  # e mais longe


async def test_varrer_semente_mostra_dispersao(chamar):
    erro, d = await chamar("varrer_parametro", arquivo_ork="foguete.ork", parametro="semente",
                           valores=[0, 1, 2, 3, 4], opcoes_voo={"vento_medio_ms": 6, "turbulencia_pct": 40})
    assert not erro, d
    assert len({linha["apogeu_m"] for linha in d["linhas"]}) > 1


@pytest.mark.parametrize("kw, trecho", [
    ({"parametro": "vento_medio_ms", "valores": ["forte"]}, "esperado um número"),
    ({"valores": list(range(26))}, "de 1 a 25"),
    ({"valores": [30], "opcoes_voo": {"vento": 3}}, "chave(s) desconhecida(s)"),
])
async def test_varrer_parametro_invalido(chamar, kw, trecho):
    kw = {"parametro": f"{FINS}.span_mm"} | kw
    erro, msg = await chamar("varrer_parametro", arquivo_ork="foguete.ork", **kw)
    assert erro and trecho in msg


async def test_exportar_series_voo(chamar, workspace):
    erro, d = await chamar("exportar_series_voo", arquivo_ork="foguete.ork", arquivo_csv="series/voo.csv")
    assert not erro, d
    with (workspace / "series" / "voo.csv").open(encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    assert len(linhas) == d["linhas"] > 50
    assert max(float(x["altitude_m"]) for x in linhas) == pytest.approx(d["resumo"]["apogeu_m"], abs=0.5)
    assert any(e["evento"] == "APOGEE" for e in d["eventos"])
    erro, msg = await chamar("exportar_series_voo", arquivo_ork="foguete.ork", arquivo_csv="series/voo.csv")
    assert erro and "já existe" in msg


async def test_exportar_series_motor(chamar, workspace):
    erro, d = await chamar("exportar_series_motor", arquivo_ric="motor.ric", arquivo_csv="series/motor.csv")
    assert not erro, d
    with (workspace / "series" / "motor.csv").open(encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    t = np.array([float(x["time_s"]) for x in linhas])
    f_ = np.array([float(x["thrust_N"]) for x in linhas])
    assert np.trapezoid(f_, t) == pytest.approx(d["resumo"]["impulso_total_Ns"], rel=0.01)
    assert float(linhas[-1]["propellant_mass_kg"]) < float(linhas[0]["propellant_mass_kg"])
