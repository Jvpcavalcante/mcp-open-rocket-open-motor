"""Linha de base das tools existentes (roadmap item 2), pelo protocolo MCP com o servidor em memória.
Referência: saída da §8 do documento de análise."""
import json

import pytest
from mcp import Client

pytestmark = pytest.mark.anyio

TOOLS_BASE = {"simular_motor", "exportar_motor_eng", "abrir_foguete", "simular_voo", "projetar_e_voar"}
CASE_29MM = {"diametro_mm": 29, "comprimento_mm": 150, "massa_hardware_kg": 0.12}


async def test_lista_tools(servidor):
    async with Client(servidor.mcp) as c:
        nomes = {t.name for t in (await c.list_tools()).tools}
    assert TOOLS_BASE <= nomes


async def test_resource_propelentes(servidor):
    async with Client(servidor.mcp) as c:
        r = await c.read_resource("openmotor://propelentes")
    props = json.loads(r.contents[0].text)
    assert len(props) == 20
    assert "Nakka - KNSB" in {p["name"] for p in props}


async def test_simular_motor(chamar):
    progresso = []

    async def cb(p, total, msg):
        progresso.append(p)

    res = await chamar.cliente.call_tool("simular_motor", {"arquivo_ric": "motor.ric"}, progress_callback=cb)
    assert not res.is_error
    d = json.loads(res.content[0].text)
    assert d["sucesso"] and d["alertas"] == []
    assert d["designacao"] == "F46"
    assert d["impulso_total_Ns"] == pytest.approx(68.81, abs=0.01)
    assert d["isp_s"] == pytest.approx(201.6, abs=0.1)
    assert d["pressao_max_MPa"] == pytest.approx(5.70, abs=0.01)
    assert len(progresso) >= 10  # §8: 18 notificações


async def test_exportar_motor_eng(chamar, workspace):
    erro, d = await chamar("exportar_motor_eng", arquivo_ric="motor.ric", **CASE_29MM)
    assert not erro, d
    assert d["nome_motor"] == "F46-OM"
    assert d["arquivo_eng"] == "motores/F46-OM.eng"  # '/' em qualquer SO (D2 do diário)
    linhas = (workspace / d["arquivo_eng"]).read_text(encoding="utf-8").splitlines()
    assert linhas[0] == "F46-OM 29 150 P 0.03481 0.15481 openMotor"
    assert linhas[1].startswith("0.0000 ")  # 1º ponto com empuxo > 0
    assert linhas[-1] == ";"


async def test_abrir_foguete(chamar):
    erro, d = await chamar("abrir_foguete", arquivo_ork="foguete.ork")
    assert not erro, d
    assert d["foguete"] == "A simple model rocket"
    assert len(d["simulacoes"]) == 5
    assert d["montagens"] == [
        {"nome": "Inner Tube", "diametro_interno_mm": 18.0, "comprimento_mm": 75.0, "motor_atual": "A8-3"}
    ]


async def test_simular_voo_motor_original(chamar):
    erro, d = await chamar("simular_voo", arquivo_ork="foguete.ork")
    assert not erro, d
    assert d["apogeu_m"] == pytest.approx(51, abs=5)  # A8-3, §3.3
    assert d["avisos_validacao"] == []


async def test_projetar_e_voar_motor_que_nao_cabe(chamar):
    erro, d = await chamar("projetar_e_voar", arquivo_ric="motor.ric", arquivo_ork="foguete.ork",
                           atraso_ejecao_s=5, **CASE_29MM)
    assert not erro, d
    voo = d["voo"]
    assert voo["apogeu_m"] == pytest.approx(1016, abs=5)  # varia ~0,1 m entre execuções (D3)
    assert voo["mach_max"] == pytest.approx(0.87, abs=0.01)
    assert any("NÃO cabe" in a for a in voo["avisos_validacao"])
    assert any("overhang" in a for a in voo["avisos_validacao"])
    # avisos do OpenRocket em inglês, independentemente do locale da máquina (D1)
    assert any("Recovery device deployment at high speed" in a for a in voo["avisos"])


async def test_motor_que_cabe_nao_gera_aviso_de_validacao(chamar):
    erro, m = await chamar("exportar_motor_eng", arquivo_ric="motor.ric", diametro_mm=18, comprimento_mm=70,
                           massa_hardware_kg=0.03, designacao="F46-18mm")
    assert not erro, m
    erro, d = await chamar("simular_voo", arquivo_ork="foguete.ork", motor_eng=m["arquivo_eng"], atraso_ejecao_s=8)
    assert not erro, d
    assert d["avisos_validacao"] == []


async def test_salvar_como_ork_novo_preserva_motor(chamar, workspace):
    erro, m = await chamar("exportar_motor_eng", arquivo_ric="motor.ric", **CASE_29MM)
    assert not erro, m
    erro, d = await chamar("simular_voo", arquivo_ork="foguete.ork", motor_eng=m["arquivo_eng"],
                           atraso_ejecao_s=5, salvar_como="saidas/foguete_f46.ork")
    assert not erro, d
    assert (workspace / "saidas" / "foguete_f46.ork").exists()

    erro, salvo = await chamar("abrir_foguete", arquivo_ork="saidas/foguete_f46.ork")
    assert not erro, salvo
    assert salvo["avisos_carregamento"] == []  # motor resolvido no banco (armadilha 3 da §3.4)
    assert salvo["montagens"][0]["motor_atual"].startswith("F46-OM")

    erro, original = await chamar("abrir_foguete", arquivo_ork="foguete.ork")
    assert original["montagens"][0]["motor_atual"] == "A8-3"  # original intocado


@pytest.mark.parametrize("caminho", ["../fora.ork", "..\\fora.ork", "sub/../../fora.ork"])
async def test_caminho_relativo_fora_do_workspace(chamar, caminho):
    erro, msg = await chamar("abrir_foguete", arquivo_ork=caminho)
    assert erro and "fora do workspace" in msg


async def test_caminho_absoluto_fora_do_workspace(chamar, workspace):
    erro, msg = await chamar("abrir_foguete", arquivo_ork=str(workspace.parent / "fora.ork"))
    assert erro and "fora do workspace" in msg


async def test_salvar_como_fora_do_workspace(chamar):
    erro, msg = await chamar("simular_voo", arquivo_ork="foguete.ork", salvar_como="../roubado.ork")
    assert erro and "fora do workspace" in msg


async def test_arquivo_inexistente(chamar):
    erro, msg = await chamar("simular_motor", arquivo_ric="nao_existe.ric")
    assert erro and "Arquivo não encontrado" in msg
