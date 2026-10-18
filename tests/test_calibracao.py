"""comparar_curvas_empuxo, calibrar_motor, calibrar_voo e os fatores de modelo do simular_voo."""
import csv
import shutil
from pathlib import Path

import numpy as np
import pytest

from rocket_mcp import analise as an

pytestmark = pytest.mark.anyio
O3800 = Path(__file__).resolve().parents[1] / "vendor" / "openMotor" / "test" / "data" / "real" / "o3800"


def _csv(caminho: Path, cabecalho, linhas):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cabecalho)
        w.writerows(linhas)


# ------------------------------------------------------------------------------- leitura (sem JVM)

@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_csv_com_unidades_imperiais(tmp_path, anyio_backend):
    t = np.linspace(0, 2, 50)
    f = 100 * np.sin(np.pi * t / 2)
    _csv(tmp_path / "si.csv", ["time (s)", "force (n)"], zip(t, f))
    _csv(tmp_path / "imp.csv", ["tempo (ms)", "empuxo (lbf)"], zip(t * 1000, f / 4.448222))
    si, imp = an.ler_csv_curva(tmp_path / "si.csv"), an.ler_csv_curva(tmp_path / "imp.csv")
    assert an.metricas_curva(imp["t"], imp["f"]) == an.metricas_curva(si["t"], si["f"])


# ---------------------------------------------------------------------------------- motor (via MCP)

async def test_comparar_ric_com_o_proprio_eng(chamar):
    erro, m = await chamar("exportar_motor_eng", arquivo_ric="motor.ric", diametro_mm=29, comprimento_mm=150,
                           massa_hardware_kg=0.12, designacao="Comparacao")
    assert not erro, m
    erro, d = await chamar("comparar_curvas_empuxo", referencia="motor.ric", comparada=m["arquivo_eng"])
    assert not erro, d
    assert abs(d["diferenca_pct"]["impulso_total_Ns"]) < 1
    assert d["erro_rms_pct_do_medio"] < 3
    assert d["referencia"]["isp_s"] == pytest.approx(201.6, abs=0.5)


async def test_calibrar_motor_recupera_a_eficiencia(chamar, workspace):
    erro, s = await chamar("exportar_series_motor", arquivo_ric="motor.ric", arquivo_csv="cal/medida.csv")
    assert not erro, s
    erro, e = await chamar("editar_motor", arquivo_ric="motor.ric", salvar_como="cal/errado.ric",
                           alteracoes={"nozzle.efficiency": 0.95})
    assert not erro, e
    erro, d = await chamar("calibrar_motor", arquivo_ric="cal/errado.ric", referencia="cal/medida.csv",
                           parametros={"nozzle.efficiency": [0.6, 1.0]}, salvar_como="cal/calibrado.ric")
    assert not erro, d
    assert d["parametros_iniciais"]["nozzle.efficiency"] == 0.95
    assert d["parametros_ajustados"]["nozzle.efficiency"] == pytest.approx(0.8, abs=0.005)
    assert d["depois"]["erro_rms_N"] < 0.1 * d["antes"]["erro_rms_N"]
    erro, lido = await chamar("ler_motor", arquivo_ric="cal/calibrado.ric")
    assert lido["motor"]["nozzle"]["efficiency"] == pytest.approx(0.8, abs=0.005)


async def test_calibrar_motor_com_ensaio_estatico_real(chamar, workspace):
    """Dados reais do openMotor: motor O3800 (4 BATES de 152 mm) e o ensaio estático medido."""
    (workspace / "ensaios").mkdir(exist_ok=True)
    shutil.copy(O3800 / "motor.ric", workspace / "ensaios" / "o3800.ric")
    shutil.copy(O3800 / "225.csv", workspace / "ensaios" / "o3800.csv")
    erro, d = await chamar("calibrar_motor", arquivo_ric="ensaios/o3800.ric", referencia="ensaios/o3800.csv",
                           parametros={"nozzle.efficiency": [0.7, 1.0], "propellant.a_factor": [0.7, 1.3]},
                           max_avaliacoes=80)
    assert not erro, d
    # test.yaml do openMotor: a simulação fica acima do ensaio real (lá, ~10% no empuxo médio). O tempo de
    # queima do test.yaml (7,25 s) usa outro critério; o nosso (5% do pico) dá 7,85 s no mesmo CSV.
    assert d["antes"]["diferenca_pct"]["impulso_total_Ns"] > 3
    assert d["depois"]["erro_rms_N"] < 0.7 * d["antes"]["erro_rms_N"]  # medido: 1123 -> 709 N
    assert abs(d["depois"]["diferenca_pct"]["impulso_total_Ns"]) < abs(d["antes"]["diferenca_pct"]["impulso_total_Ns"])


@pytest.mark.parametrize("kw, trecho", [
    ({"parametros": {}}, "de 1 a 4 itens"),
    ({"parametros": {"nozzle.efficiency": [1.0, 0.5]}}, "mínimo < máximo"),
    ({"parametros": {"nozzle.garganta_mm": [1, 2]}}, "inexistente"),
    ({"referencia": "nao_existe.csv"}, "não encontrado"),
])
async def test_calibrar_motor_invalido(chamar, kw, trecho):
    args = {"arquivo_ric": "motor.ric", "referencia": "motor.ric", "parametros": {"nozzle.efficiency": [0.6, 1.0]}} | kw
    erro, msg = await chamar("calibrar_motor", **args)
    assert erro and trecho in msg


# ------------------------------------------------------------------------------------ voo (via MCP)

async def test_fatores_no_simular_voo(chamar):
    _, base = await chamar("simular_voo", arquivo_ork="foguete.ork")
    _, arrasto = await chamar("simular_voo", arquivo_ork="foguete.ork", fator_arrasto=1.3)
    assert arrasto["apogeu_m"] < base["apogeu_m"]
    assert arrasto["fatores_modelo"] == {"massa": 1.0, "impulso": 1.0, "arrasto": 1.3}
    erro, msg = await chamar("simular_voo", arquivo_ork="foguete.ork", fator_massa=1.1, salvar_como="x.ork")
    assert erro and "não combina" in msg


async def test_calibrar_voo_recupera_o_arrasto(chamar, workspace):
    erro, s = await chamar("exportar_series_voo", arquivo_ork="foguete.ork", arquivo_csv="cal/voo_real.csv",
                           opcoes_voo={"fator_arrasto": 1.3})
    assert not erro, s
    erro, d = await chamar("calibrar_voo", arquivo_ork="foguete.ork", arquivo_log="cal/voo_real.csv")
    assert not erro, d
    assert d["fatores_ajustados"]["fator_arrasto"] == pytest.approx(1.3, abs=0.02)
    assert d["erro_rms_altitude_m"]["depois"] < 0.5 < d["erro_rms_altitude_m"]["antes"]
    assert d["apogeu_m"]["simulado_depois"] == pytest.approx(d["apogeu_m"]["log"], abs=0.5)

    # o mesmo log em pés e milissegundos, com o relógio atrasado 3 s (altímetro ligado antes)
    with (workspace / "cal" / "voo_real.csv").open(encoding="utf-8") as f:
        linhas = [(float(r["time_s"]), float(r["altitude_m"])) for r in csv.DictReader(f)]
    _csv(workspace / "cal" / "voo_pes.csv", ["Time (ms)", "Altitude (ft)"],
         [((t + 3) * 1000, a / 0.3048) for t, a in linhas])
    erro, p = await chamar("calibrar_voo", arquivo_ork="foguete.ork", arquivo_log="cal/voo_pes.csv")
    assert not erro, p
    assert p["fatores_ajustados"]["fator_arrasto"] == pytest.approx(1.3, abs=0.02)


@pytest.mark.parametrize("kw, trecho", [
    ({"ajustar": ["cd"]}, "'arrasto', 'impulso' e/ou 'massa'"),
    ({"faixa": [2, 1]}, "mín < máx"),
    ({"arquivo_log": "nao_existe.csv"}, "existente no workspace"),
])
async def test_calibrar_voo_invalido(chamar, kw, trecho):
    args = {"arquivo_ork": "foguete.ork", "arquivo_log": "cal/voo_real.csv"} | kw
    erro, msg = await chamar("calibrar_voo", **args)
    assert erro and trecho in msg
