"""Flutter de aletas compósitas (docs/FLUTTER.md): validações da §14, regressão contra o código original
do documento, configuração, critérios (sem JVM) e as tools analisar_flutter/comparar_aletas sobre o
exemplo de dois estágios do OpenRocket."""
import csv
import math

import numpy as np
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from rocket_mcp import fin_flutter as ff
from rocket_mcp import flutter_estudo as fe
from rocket_mcp import or_bridge as orb

MATS = {"carbono": dict(E1=55e9, E2=55e9, G12=4e9, nu12=0.08, rho=1500),
        "vidro": dict(E1=20e9, E2=20e9, G12=3.5e9, nu12=0.12, rho=1850)}
L6 = [{"mat": "carbono", "theta_deg": 0, "t": .3e-3}, {"mat": "carbono", "theta_deg": 45, "t": .3e-3},
      {"mat": "vidro", "theta_deg": 0, "t": .2e-3}, {"mat": "vidro", "theta_deg": 0, "t": .2e-3},
      {"mat": "carbono", "theta_deg": 45, "t": .3e-3}, {"mat": "carbono", "theta_deg": 0, "t": .3e-3}]
HEMERA = dict(cr=0.27, ct=0.13, s=0.18, sweep_le_deg=45)


def _modos(plies=L6, mats=MATS, fin=HEMERA):
    A, B, D, h, mA = ff.laminate(plies, mats)
    om, kinds = ff.plate_modes(D, mA, fin["cr"], fin["ct"], fin["s"], fin["sweep_le_deg"])
    return (*ff.pick_modes(om, kinds), mA)


def _trajetoria(v_max=600.0):
    """Subida sintética: acelera até v_max em 4 s e desacelera; atmosfera ISA (§3.1)."""
    t = np.linspace(0, 20, 2001)
    V = np.where(t < 4, v_max / 4 * t, v_max - 25 * (t - 4)).clip(0)
    alt = np.cumsum(V) * 0.01
    T = 288.15 - 0.0065 * alt
    p = 101325 * (T / 288.15) ** 5.2559
    rho, a = p / (287 * T), np.sqrt(1.4 * 287 * T)
    return list(zip(t, V, V / a, rho, p, a))


# ---------------------------------------------------------------------------- validação da §14 (sem JVM)

def test_hodges_pierce():
    a, xa, mu, ra2 = -0.2, 0.1, 20.0, 6 / 25
    k, V, g, om = ff.vg(mu, a, xa, ra2, 0.4, 1.0, 1.0, lambda k: ff.aero_theodorsen(k, a))
    assert ff.flutter_from_vg(V, g, om)[0] == pytest.approx(2.17, abs=0.02)


def test_leissa_placa_engastada_livre():
    E, nu, h, rho, aa = 70e9, 0.3, 2e-3, 2700, 0.2
    Dp = E * h ** 3 / (12 * (1 - nu ** 2))
    om, kinds = ff.plate_modes(Dp * np.array([[1, nu, 0], [nu, 1, 0], [0, 0, (1 - nu) / 2]]), rho * h, aa, aa, aa)
    assert om * aa ** 2 * np.sqrt(rho * h / Dp) == pytest.approx([3.471, 8.507, 21.29, 27.20], rel=2e-3)
    assert kinds[:2] == ["flexao", "torcao"]


def test_laminado_isotropico():
    E, nu = 70e9, 0.3
    _, B, D, h, _ = ff.laminate([{"mat": "Al", "theta_deg": 0, "t": 3e-3}],
                                {"Al": dict(E1=E, E2=E, G12=E / 2.6, nu12=nu, rho=2700)})
    assert D[0, 0] == pytest.approx(E * h ** 3 / (12 * (1 - nu ** 2)), rel=1e-12)
    assert np.abs(B).max() < 1e-9


def test_viga_subestima_torcao_em_baixo_alongamento():
    """Exemplo da §7 (8 lâminas de tecido de carbono, 0/45 simétrico): viga ~141 Hz, placa ~279 Hz."""
    plies = [{"mat": "carbono", "theta_deg": a, "t": 0.3e-3} for a in (0, 45, 0, 45, 45, 0, 45, 0)]
    A, B, D, h, mA = ff.laminate(plies, MATS)
    _, wa_viga = ff.beam_frequencies(D, mA, 0.30, 0.10, 0.15)
    om, kinds = ff.plate_modes(D, mA, 0.30, 0.10, 0.15, 40)
    assert wa_viga / (2 * np.pi) == pytest.approx(141, abs=1)
    assert ff.pick_modes(om, kinds)[1] / (2 * np.pi) == pytest.approx(279, abs=1)


# ------------------------------------------------- regressão: mesmos números do código do documento

@pytest.mark.parametrize("M, rho, gs, vf_documento", [
    (0.3, 1.2, 0, 116.93580713046259), (0.79, 1.2, 0, 76.60899000064006), (0.9, 1.2, 0, 73.7414196409773),
    (1.21, 0.3, 0, 591.6509065490887), (2.0, 1.2, 0, 506.8113657693621), (4.0, 0.3, 0, 1291.9403575268939),
    (0.5, 0.8, 0.02, 136.94312969354326), (1.5, 0.5, 0.02, 595.0664479007316),
])
def test_vg_vetorizado_igual_ao_documento(M, rho, gs, vf_documento):
    wh, wa, mA = _modos()
    vf, _ = ff.flutter_speed_at(M, wh, wa, mA, 0.27, 0.13, 0.18, rho, gs=gs)
    assert vf == pytest.approx(vf_documento, rel=1e-9)


def test_run_case_igual_ao_documento_e_com_q_maximo():
    traj = _trajetoria()
    r = ff.run_case(HEMERA, L6, MATS, traj)
    # valores do código original do documento na mesma trajetória
    assert r["f_h"] == pytest.approx(45.05933369170475, rel=1e-9)
    assert r["f_a"] == pytest.approx(111.15941587887691, rel=1e-9)
    assert r["margem_flutter"] == pytest.approx(0.2058717505452341, rel=1e-9)
    assert r["regime_critico"] == "transonico" and r["t_critico"] == pytest.approx(2.43)
    assert r["margem_q_divergencia"] == pytest.approx(0.22247582767149965, rel=1e-9)
    # o original pulava o ponto de q máximo na subamostragem (TN-4197 dava 0,1158)
    q = [0.5 * x[3] * x[1] ** 2 for x in traj]
    assert traj[int(np.argmax(q))][0] in {p["t"] for p in r["pontos"]}
    assert r["margem_TN4197"] < 0.1158
    # ponto casado: subsônico, o Mach no flutter é maior que o do ponto -> margem não maior
    assert r["margem_flutter_casada"] > 0 and r["M_flutter_casado"] < 1


# --------------------------------------------------------------------------------- física (sem JVM)

def test_tecido_a_45_e_laminado_simetrico():
    D = ff.laminate(L6, MATS)[2]
    assert abs(D[0, 2]) / D[0, 0] < 1e-12  # tecido (E1 = E2) a 45° não acopla
    ud = {"ud": dict(E1=181e9, E2=10.3e9, G12=7.17e9, nu12=0.28, rho=1600)}
    _, B, D, _, _ = ff.laminate([{"mat": "ud", "theta_deg": 0, "t": 1e-3}, {"mat": "ud", "theta_deg": 45, "t": 1e-3}], ud)
    assert np.abs(B).max() > 1e3 and abs(D[0, 2]) > 0  # assimétrico e desbalanceado
    Q = ff.Q_matrix(181e9, 10.3e9, 7.17e9, 0.28)
    assert ff.Qbar(Q, 0) == pytest.approx(Q)
    assert ff.Qbar(Q, 90)[1, 1] == pytest.approx(Q[0, 0])


def test_tn4197_igual_a_checagem_de_metais():
    """A TN-4197 em SI do documento é a mesma do bloco 'seguranca' (or_bridge._flutter), com G = G_E."""
    g, p, a = 26e9, 60000.0, 320.0
    assert ff.tn4197(g, 0.27, 0.13, 0.18, 0.005, p, a) == pytest.approx(orb._flutter(0.27, 0.13, 0.18, 0.005, g, a, p),
                                                                      rel=1e-3)


def test_divergencia_so_no_subsonico():
    wh, wa, mA = _modos()
    assert math.isfinite(ff.divergence_q(mA, wa, 0.27, 0.13, 0.18, 0.5))
    assert ff.divergence_q(mA, wa, 0.27, 0.13, 0.18, 1.5) == math.inf  # CA a 50%, atrás do EA (40%)


def test_mais_rigidez_e_espessura_aumentam_a_margem():
    traj = _trajetoria(300)
    base = ff.run_case(HEMERA, L6, MATS, traj)["margem_flutter"]
    rigido = {k: m | {"G12": m["G12"] * 2} for k, m in MATS.items()}
    assert ff.run_case(HEMERA, L6, rigido, traj)["margem_flutter"] > base
    assert ff.run_case(HEMERA, L6 + L6, MATS, traj)["margem_flutter"] > base


def test_frequencias_medidas_e_trajetoria_vazia():
    r = ff.run_case(HEMERA, L6, MATS, _trajetoria(300), freqs_hz=(60.0, 200.0))
    assert r["f_h"] == pytest.approx(60) and r["f_a"] == pytest.approx(200) and r["frequencias_medidas"]
    assert r["modos_hz"] is None
    with pytest.raises(ValueError, match="V > 30"):
        ff.run_case(HEMERA, L6, MATS, [(0, 10, 0.03, 1.2, 101325, 340)])


# ------------------------------------------------------------------------------ configuração e critérios

CONFIG = {
    "materials": {"carbono": {"E1": "55.0e9", "E2": 55.0e9, "G12": 4.0e9, "nu12": 0.08, "rho": 1500,
                              "source": "placeholder"},
                  "vidro": {"E1": 20.0e9, "E2": 20.0e9, "G12": 3.5e9, "nu12": 0.12, "rho": 1850, "source": "ensaio 2026-09"}},
    "laminates": {"L6": L6},
    "analysis": {"e_ea": 0.35},
}


def test_config_aceita_expoente_sem_sinal_e_marca_placeholder():
    cfg = fe.carregar_config(CONFIG)
    assert cfg["materials"]["carbono"]["E1"] == 55e9  # o PyYAML lê "55.0e9" como texto
    assert cfg["placeholders"] == ["carbono"]
    assert cfg["analysis"] == {"e_ea": 0.35, "eta_ref": 0.75, "g_s": 0.0}
    props = fe.propriedades_laminado(cfg, "L6")
    assert props["h_mm"] == pytest.approx(1.6) and props["rho_eq_kg_m3"] == pytest.approx(1587.5)
    assert props["placeholders"] == ["carbono"]


@pytest.mark.parametrize("mudanca, trecho", [
    ({"materials": {"x": {"E1": 1}}}, "faltam"),
    ({"materials": {"x": {"E1": 55, "E2": 55, "G12": 4, "nu12": 0.1, "rho": 1500}}}, "em Pa"),
    ({"laminates": {"L": [{"mat": "kevlar", "theta_deg": 0, "t": 3e-4}]}}, "não definido"),
    ({"laminates": {"L": [{"mat": "carbono", "theta_deg": 0, "t": 0.3}]}}, "em metros"),
    ({"analysis": {"e_ea": 1.5}}, "analysis.e_ea"),
    ({"aletas": {"Fins": {"laminado": "L6", "f_h_hz": 80}}}, "f_h_hz E f_a_hz"),
    ({"aletas": {"Fins": "L99"}}, "não definido"),
    ({"extra": 1}, "desconhecida"),
])
def test_config_invalida(mudanca, trecho):
    with pytest.raises(ToolError, match=trecho):
        fe.carregar_config(CONFIG | mudanca)


def _res(**kw):
    base = dict(margem_flutter=2.0, margem_flutter_casada=1.9, margem_q_divergencia=5.0, B_relativo=0.0,
                margem_TN4197=1.2, D16_D11=0.0, regime_critico="sub", frequencias_medidas=False,
                pontos=[{"margem": 2.0, "regime": "sub"}])
    return base | kw


def test_criterios():
    crit = fe.CRITERIOS_PADRAO
    assert fe.avaliar(_res(), crit)["aprovado"]
    # a margem usada é a menor entre a do ponto e a casada
    assert not fe.avaliar(_res(margem_flutter_casada=1.4), crit)["aprovado"]
    # 1,3 reprova com frequências calculadas e passa com medidas (1,15)
    assert not fe.avaliar(_res(margem_flutter=1.3, margem_flutter_casada=1.3), crit)["aprovado"]
    assert fe.avaliar(_res(margem_flutter=1.3, margem_flutter_casada=1.3, frequencias_medidas=True), crit)["aprovado"]
    # todo ponto transônico precisa de 1,7, mesmo que o crítico seja subsônico
    trans = _res(pontos=[{"margem": 2.0, "regime": "sub"}, {"margem": 1.6, "regime": "transonico"}])
    assert not fe.avaliar(trans, crit)["aprovado"]
    # TN-4197 só alerta; estabilidade reprova o 'aprovado', não o 'aprovado_flutter'
    av = fe.avaliar(_res(margem_TN4197=0.5), crit, {"saida_trilho_cal": 1.2, "min_subida_cal": 1.6, "max_subida_cal": 2})
    assert av["aprovado_flutter"] and not av["aprovado"]
    assert any("validar com ensaio" in a for a in av["alertas"])


def test_sensibilidade():
    cfg = fe.carregar_config(CONFIG)
    s = fe.sensibilidade(HEMERA, L6, cfg["materials"], _trajetoria(300), cfg["analysis"], cfg["criterios"])
    casos = {c["caso"]: c for c in s["casos"]}
    assert list(casos) == ["base", "e_ea = 0,35", "e_ea = 0,50", "eta_ref = 0,70", "eta_ref = 0,80", "G12 -30%"]
    assert casos["G12 -30%"]["margem_flutter"] < casos["base"]["margem_flutter"]
    assert isinstance(s["decisao_muda"], bool)


# --------------------------------------------------------------------------- com o OpenRocket (MCP)

BOOSTER, SUSTENTADOR = "Booster fin section/Trapezoidal Fin Set", "Sustainer Fin Section/Trapezoidal Fin Set"
LAMINADOS = {SUSTENTADOR: "L6_quasi", BOOSTER: "L10_45faces"}


@pytest.mark.anyio
async def test_analisar_flutter_dois_estagios(chamar, workspace):
    erro, d = await chamar("analisar_flutter", arquivo_ork="dois_estagios.ork", arquivo_config="fins_config.yaml",
                           laminados=LAMINADOS, sensibilidade=True, arquivo_csv="flutter/margem.csv",
                           salvar_como="flutter/laminado.ork")
    assert not erro, d
    sust, boost = d["aletas"]
    assert sust["trajetoria"]["origem"] == "ramo 0"
    assert boost["trajetoria"]["origem"] == "ramo 'Booster'"
    assert boost["resultado"]["t_critico"] <= 1.6  # booster: crítico antes da separação (1,535 s)
    assert sust["laminado"]["h_mm"] == pytest.approx(1.6) and boost["laminado"]["h_mm"] == pytest.approx(2.6)
    assert sust["resultado"]["modos_tipo"][:2] == ["flexao", "torcao"]
    assert {"margem_flutter", "margem_flutter_casada", "margem_q_divergencia", "margem_TN4197"} <= sust["resultado"].keys()
    assert any("PLACEHOLDER" in a for a in sust["criterios"]["alertas"]) and d["placeholders"]
    assert len(sust["sensibilidade"]["casos"]) == 6
    assert d["voo"]["apogeu_m"] > 0
    # o laminado foi escrito no foguete: espessura e densidade equivalente
    _, lido = await chamar("ler_foguete", arquivo_ork="flutter/laminado.ork")
    fins = next(c for c in lido["componentes"] if c["caminho"].endswith(BOOSTER))
    assert fins["params"]["thickness_mm"] == pytest.approx(2.6)
    assert fins["params"]["material"] == "Laminado L10_45faces"
    assert fins["params"]["materialDensity_kg_m3"] == pytest.approx(1607.69, abs=0.01)
    with (workspace / "flutter" / "margem.csv").open(encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    assert {x["aletas"] for x in linhas} == {sust["aletas"], boost["aletas"]}
    assert min(float(x["margem"]) for x in linhas if x["aletas"] == sust["aletas"]) == \
        pytest.approx(sust["resultado"]["margem_flutter"], rel=1e-5)


@pytest.mark.anyio
async def test_analisar_flutter_sem_aplicar_avisa_espessura(chamar):
    erro, d = await chamar("analisar_flutter", arquivo_ork="dois_estagios.ork", arquivo_config="fins_config.yaml",
                           laminados={BOOSTER: "L6_quasi"}, aplicar_laminado=False)
    assert not erro, d
    assert len(d["aletas"]) == 1 and any("diferente do laminado" in e for e in d["efeitos"])


@pytest.mark.anyio
@pytest.mark.parametrize("kw, trecho", [
    ({"arquivo_config": "nao_existe.yaml"}, "não encontrado"),
    ({"arquivo_config": "foguete.ork"}, ".yaml"),
    ({"laminados": {BOOSTER: "L99"}}, "não definido"),
    ({}, "Informe o laminado"),  # dois laminados no YAML e nenhum mapa
    ({"laminados": {"Nose cone": "L6_quasi"}}, "não um conjunto de aletas"),
])
async def test_analisar_flutter_invalido(chamar, kw, trecho):
    kw = {"arquivo_config": "fins_config.yaml"} | kw
    erro, msg = await chamar("analisar_flutter", arquivo_ork="dois_estagios.ork", **kw)
    assert erro and trecho in msg, msg


@pytest.mark.anyio
async def test_comparar_aletas_com_laminado(chamar):
    erro, d = await chamar("comparar_aletas", arquivo_ork="dois_estagios.ork", aletas=BOOSTER,
                           arquivo_config="fins_config.yaml", laminado="L6_quasi",
                           variantes=[{"nome": "grosso", "laminado": "L10_45faces"},
                                      {"nome": "curta", "span_mm": 100}])
    assert not erro, d
    original, grosso, curta = d["linhas"]
    fo, fg, fc = original["flutter_laminado"], grosso["flutter_laminado"], curta["flutter_laminado"]
    assert fo["laminado"] == "L6_quasi" and fg["laminado"] == "L10_45faces"
    assert grosso["aletas"]["params"]["thickness_mm"] == pytest.approx(2.6)
    assert fg["margem_flutter"] > fo["margem_flutter"]           # mais espesso e ±45 nas faces
    assert fc["margem_flutter"] > fo["margem_flutter"]           # menos envergadura
    assert grosso["aletas"]["massa_material_kg"] > original["aletas"]["massa_material_kg"]
    assert isinstance(fo["aprovado_flutter"], bool) and "reprovado_em" in fo


@pytest.mark.anyio
@pytest.mark.parametrize("kw, trecho", [
    ({"variantes": [{"laminado": "L6_quasi", "thickness_mm": 3}]}, "tire thickness_mm"),
    ({"variantes": [{"laminado": "L6_quasi"}], "arquivo_config": None}, "exige arquivo_config"),
    ({"simular": False}, "simular=True"),
    ({"laminado": "L99"}, "laminado: exige"),
])
async def test_comparar_aletas_laminado_invalido(chamar, kw, trecho):
    kw = {"arquivo_config": "fins_config.yaml", "variantes": [{"span_mm": 100}]} | kw
    kw = {k: v for k, v in kw.items() if v is not None}
    erro, msg = await chamar("comparar_aletas", arquivo_ork="dois_estagios.ork", aletas=BOOSTER, **kw)
    assert erro and trecho in msg, msg
