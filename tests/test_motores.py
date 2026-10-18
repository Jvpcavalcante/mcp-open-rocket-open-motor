"""criar_motor / ler_motor / editar_motor / listar_tipos_grao (roadmap item 4)."""
import pytest

pytestmark = pytest.mark.anyio

BATES_54 = {"type": "BATES", "diameter_mm": 54, "length_mm": 100, "coreDiameter_mm": 20}
TUBEIRA = {"throat_mm": 12, "exit_mm": 30, "efficiency": 0.85, "divAngle_deg": 15, "convAngle_deg": 45}


async def criar(chamar, arquivo, **kw):
    args = {"propellant": "KNSB", "grains": [BATES_54] * 3, "nozzle": TUBEIRA} | kw
    return await chamar("criar_motor", arquivo_ric=arquivo, **args)


async def test_listar_tipos_grao(chamar):
    erro, d = await chamar("listar_tipos_grao")
    assert not erro, d
    assert "Custom Grain" not in d["grains"]  # exige DXF
    bates = d["grains"]["BATES"]
    assert bates["coreDiameter_mm"]["obrigatorio"] and bates["coreDiameter_mm"]["tipo"] == "float"
    assert bates["inhibitedEnds"]["valores"] == ["Neither", "Top", "Bottom", "Both"]
    assert d["grains"]["Finocyl"]["numFins"]["tipo"] == "int"
    assert d["nozzle"]["throat_mm"]["obrigatorio"] and not d["nozzle"]["throatLength_mm"]["obrigatorio"]
    assert d["config"]["maxMachNumber"]["padrao"] == 0.7
    assert "Nakka - KNSB" in d["propellants"]


async def test_criar_motor_bates(chamar, workspace):
    erro, d = await criar(chamar, "novos/j.ric", simular=True)
    assert not erro, d
    assert d["arquivo_ric"] == "novos/j.ric" and (workspace / "novos" / "j.ric").exists()
    assert d["motor"]["propellant"] == "Nakka - KNSB"  # 'KNSB' resolvido na biblioteca
    assert d["motor"]["grains"][0] == BATES_54 | {"inhibitedEnds": "Neither"}
    assert d["previa"]["razao_porta_garganta"] == pytest.approx(2.78, abs=0.01)
    assert d["previa"]["massa_propelente_kg"] == pytest.approx(1.037, abs=0.001)
    sim = d["simulacao"]
    assert sim["sucesso"] and sim["designacao"].startswith("J")

    # o .ric gravado é lido de volta pela tool de simulação
    erro, s = await chamar("simular_motor", arquivo_ric="novos/j.ric")
    assert not erro and s["impulso_total_Ns"] == sim["impulso_total_Ns"]


async def test_ida_e_volta_reproduz_o_motor_original(chamar):
    """ler_motor -> criar_motor recria o motor de teste, inclusive o propelente próprio do arquivo."""
    erro, lido = await chamar("ler_motor", arquivo_ric="motor.ric")
    assert not erro, lido
    assert isinstance(lido["motor"]["propellant"], dict)  # difere da versão atual da biblioteca
    erro, d = await chamar("criar_motor", arquivo_ric="copia.ric", simular=True, **lido["motor"])
    assert not erro, d
    assert d["simulacao"]["designacao"] == "F46"
    assert d["simulacao"]["impulso_total_Ns"] == pytest.approx(68.81, abs=0.05)
    assert d["simulacao"]["pressao_max_MPa"] == pytest.approx(5.70, abs=0.01)


async def test_criar_motor_finocyl(chamar):
    fino = {"type": "Finocyl", "diameter_mm": 54, "length_mm": 150, "coreDiameter_mm": 15,
            "numFins": 4, "finWidth_mm": 4, "finLength_mm": 12}
    erro, d = await criar(chamar, "fino.ric", grains=[fino], simular=True)
    assert not erro, d
    assert d["motor"]["grains"][0]["invertedFins"] is False
    assert d["simulacao"]["sucesso"]


async def test_geometria_invalida_nao_grava(chamar, workspace):
    ruim = BATES_54 | {"coreDiameter_mm": 60}
    erro, msg = await criar(chamar, "ruim.ric", grains=[BATES_54, ruim])
    assert erro
    assert "grains[1]" in msg and "Core diameter must be less than grain diameter" in msg
    assert not (workspace / "ruim.ric").exists()


async def test_end_burner_fora_da_frente(chamar):
    eb = {"type": "End Burner", "diameter_mm": 54, "length_mm": 50}
    erro, msg = await criar(chamar, "eb.ric", grains=[BATES_54, eb])
    assert erro and "forward-most" in msg


@pytest.mark.parametrize("grao, trecho", [
    ({"type": "BATES", "diameter_mm": 54, "length_mm": 100}, "faltam parâmetros obrigatórios ['coreDiameter_mm']"),
    (BATES_54 | {"core_mm": 20}, "desconhecido(s) ['core_mm']"),
    (BATES_54 | {"diameter_mm": 1e7}, "fora da faixa"),  # o motorlib ignoraria em silêncio
    (BATES_54 | {"diameter_mm": "54"}, "esperado um número"),
    (BATES_54 | {"inhibitedEnds": "Ambos"}, "Opções: ['Neither', 'Top', 'Bottom', 'Both']"),
    (BATES_54 | {"type": "Bates"}, "type inválido"),
])
async def test_parametros_de_grao_invalidos(chamar, grao, trecho):
    erro, msg = await criar(chamar, "x.ric", grains=[grao])
    assert erro and trecho in msg


async def test_int_nao_aceita_fracao(chamar):
    fino = {"type": "Finocyl", "diameter_mm": 54, "length_mm": 150, "coreDiameter_mm": 15,
            "numFins": 4.5, "finWidth_mm": 4, "finLength_mm": 12}
    erro, msg = await criar(chamar, "x.ric", grains=[fino])
    assert erro and "esperado um inteiro" in msg


@pytest.mark.parametrize("nome, resolvido", [
    ("knsb", "Nakka - KNSB"),
    ("Nakka - KNDX", "Nakka - KNDX"),
    ("White Lightning", "RCS - White Lightning"),  # não confunde com 'Slow White Lightning'
])
async def test_resolucao_de_propelente(chamar, nome, resolvido):
    erro, d = await criar(chamar, "p.ric", propellant=nome, sobrescrever=True)
    assert not erro, d
    assert d["motor"]["propellant"] == resolvido


@pytest.mark.parametrize("nome, trecho", [("RCS", "ambíguo"), ("Plutônio", "não encontrado")])
async def test_propelente_invalido(chamar, nome, trecho):
    erro, msg = await criar(chamar, "x.ric", propellant=nome)
    assert erro and trecho in msg


async def test_nao_sobrescreve_sem_pedir(chamar):
    erro, d = await criar(chamar, "unico.ric")
    assert not erro, d
    erro, msg = await criar(chamar, "unico.ric")
    assert erro and "já existe" in msg
    erro, d = await criar(chamar, "unico.ric", sobrescrever=True)
    assert not erro, d


@pytest.mark.parametrize("arquivo, trecho", [("../fora.ric", "fora do workspace"), ("motor.txt", "extensão .ric")])
async def test_destino_invalido(chamar, arquivo, trecho):
    erro, msg = await criar(chamar, arquivo)
    assert erro and trecho in msg


async def test_ler_motor(chamar):
    erro, d = await chamar("ler_motor", arquivo_ric="motor.ric")
    assert not erro, d
    assert d["motor"]["grains"][0]["type"] == "BATES"
    assert d["motor"]["nozzle"]["throat_mm"] == pytest.approx(3.048, abs=0.001)
    assert d["previa"]["kn_inicial"] == pytest.approx(286.7, abs=0.1)
    assert d["alertas"] == []


async def test_editar_motor_salvar_como_mantem_original(chamar):
    erro, d = await chamar("editar_motor", arquivo_ric="motor.ric", salvar_como="editado.ric",
                           alteracoes={"grains[0].coreDiameter_mm": 10, "nozzle.throat_mm": 3.2})
    assert not erro, d
    assert d["motor"]["grains"][0]["coreDiameter_mm"] == 10
    assert d["motor"]["nozzle"]["throat_mm"] == 3.2
    erro, original = await chamar("ler_motor", arquivo_ric="motor.ric")
    assert original["motor"]["grains"][0]["coreDiameter_mm"] == pytest.approx(8.255, abs=0.001)


async def test_editar_acrescenta_substitui_e_remove_graos(chamar):
    erro, d = await criar(chamar, "graos.ric", grains=[BATES_54])
    assert not erro, d
    curto = BATES_54 | {"length_mm": 50}
    erro, d = await chamar("editar_motor", arquivo_ric="graos.ric",
                           alteracoes={"grains[1]": curto, "grains[2]": curto})  # acrescenta 2
    assert [g["length_mm"] for g in d["motor"]["grains"]] == [100, 50, 50]
    erro, d = await chamar("editar_motor", arquivo_ric="graos.ric",
                           alteracoes={"grains[0]": None, "grains[1]": BATES_54 | {"length_mm": 70}})
    assert [g["length_mm"] for g in d["motor"]["grains"]] == [50, 70]
    erro, d = await chamar("editar_motor", arquivo_ric="graos.ric", alteracoes={"propellant": "KNDX"})
    assert d["motor"]["propellant"] == "Nakka - KNDX"


@pytest.mark.parametrize("alteracoes, trecho", [
    ({"grains[5].length_mm": 10}, "o motor tem 1 grão(s)"),
    ({"grains[3]": BATES_54}, "use grains[1]"),
    ({"nozzle.garganta_mm": 3}, "desconhecido(s)"),
    ({"tubeira.throat_mm": 3}, "Caminho inválido"),
    ({}, "ao menos uma alteração"),
])
async def test_editar_invalido(chamar, alteracoes, trecho):
    erro, d = await criar(chamar, "base.ric", grains=[BATES_54], sobrescrever=True)
    assert not erro, d
    erro, msg = await chamar("editar_motor", arquivo_ric="base.ric", alteracoes=alteracoes)
    assert erro and trecho in msg


async def test_editar_invalido_nao_grava(chamar):
    erro, d = await criar(chamar, "intacto.ric", grains=[BATES_54])
    assert not erro, d
    erro, msg = await chamar("editar_motor", arquivo_ric="intacto.ric",
                             alteracoes={"grains[0].coreDiameter_mm": 30, "grains[0].diameter_mm": 25})
    assert erro and "nada foi gravado" in msg
    erro, lido = await chamar("ler_motor", arquivo_ric="intacto.ric")
    assert lido["motor"]["grains"][0] == BATES_54 | {"inhibitedEnds": "Neither"}


async def test_iterar_ate_pressao_maxima(chamar):
    """Critério de aceitação do roadmap: iterar editar_motor(simular=True) até Pmax < 5 MPa.
    O roadmap sugeria abrir o núcleo, mas neste BATES (L > d) isso AUMENTA a área de queima e a
    pressão (5,70 -> 6,71 MPa com +1 mm). O que baixa a pressão é abrir a garganta (Kn menor)."""
    erro, d = await chamar("editar_motor", arquivo_ric="motor.ric", salvar_como="iter.ric",
                           alteracoes={"grains[0].coreDiameter_mm": 9.255}, simular=True)
    assert not erro, d
    assert d["simulacao"]["pressao_max_MPa"] > 5.70  # núcleo maior -> pressão maior

    garganta = d["motor"]["nozzle"]["throat_mm"]
    for _ in range(10):
        if d["simulacao"]["pressao_max_MPa"] < 5:
            break
        garganta += 0.1
        erro, d = await chamar("editar_motor", arquivo_ric="iter.ric", simular=True,
                               alteracoes={"nozzle.throat_mm": garganta})
        assert not erro, d
    assert d["simulacao"]["pressao_max_MPa"] < 5
    assert d["simulacao"]["sucesso"]
