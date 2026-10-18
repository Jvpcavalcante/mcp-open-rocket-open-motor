"""Bloco 'seguranca' do simular_voo: q máximo, acelerações, empuxo/peso, descida, impacto e flutter."""
import math

import pytest

from rocket_mcp import or_bridge as orb


# ------------------------------------------------------------------------------ fórmula de flutter (sem JVM)

def _geometria(cr, ct, h, t):
    area = (cr + ct) / 2 * h
    ar = h ** 2 / area
    return ar, ct / cr, t / cr


def _flutter_naca_imperial(cr, ct, h, t, g_psi, a_fps, p_psi):
    """NACA TN-4197 (Martin, 1958), forma usual: G em psi, pressão como razão p/p0 (p0 = 14,696 psi)."""
    ar, lam, tc = _geometria(cr, ct, h, t)
    return a_fps * math.sqrt(g_psi / (39.3 * ar ** 3 / (tc ** 3 * (ar + 2)) * (lam + 1) / 2 * p_psi / 14.696))


def _flutter_pof291_imperial(cr, ct, h, t, g_psi, a_fps, p_psi):
    """Peak of Flight #291 (Apogee), como publicada."""
    ar, lam, tc = _geometria(cr, ct, h, t)
    return a_fps * math.sqrt(g_psi / ((1.337 * ar ** 3 * p_psi * (lam + 1)) / (2 * (ar + 2) * tc ** 3)))


ALETA_IMPERIAL = (8.0, 3.0, 4.0, 0.125, 380_000.0, 1100.0, 12.0)  # pol, pol, pol, pol, psi, pés/s, psi


def test_flutter_em_si_bate_com_a_naca_em_imperial():
    pol, pe, psi = 0.0254, 0.3048, 6894.757
    cr, ct, h, t, g, a, p = ALETA_IMPERIAL
    esperado = _flutter_naca_imperial(*ALETA_IMPERIAL) * pe
    obtido = orb._flutter(cr * pol, ct * pol, h * pol, t * pol, g * psi, a * pe, p * psi)
    assert obtido == pytest.approx(esperado, rel=1e-3)


def test_peak_of_flight_dá_raiz_de_2_a_mais():
    """Registro da escolha: 1,337 = 39,3/(2·14,696) já absorve o ½ de (λ+1)/2, e a expressão publicada no
    PoF #291 ainda divide por 2·(AR+2). Usamos a forma da NACA, que é a mais conservadora (Vf menor)."""
    assert _flutter_pof291_imperial(*ALETA_IMPERIAL) == pytest.approx(
        _flutter_naca_imperial(*ALETA_IMPERIAL) * math.sqrt(2), rel=1e-3)


def test_flutter_cresce_com_espessura_e_rigidez():
    base = orb._flutter(0.27, 0.13, 0.18, 0.005, 26e9, 340, 90_000)
    assert orb._flutter(0.27, 0.13, 0.18, 0.010, 26e9, 340, 90_000) == pytest.approx(base * 2 ** 1.5, rel=1e-9)
    assert orb._flutter(0.27, 0.13, 0.18, 0.005, 4 * 26e9, 340, 90_000) == pytest.approx(base * 2, rel=1e-9)
    assert orb._flutter(0.27, 0.13, 0.18, 0.005, 26e9, 340, 45_000) > base  # ar mais rarefeito: mais margem


# --------------------------------------------------------------------------------------------- via MCP

@pytest.mark.anyio
async def test_bloco_seguranca(chamar):
    erro, d = await chamar("simular_voo", arquivo_ork="foguete.ork")
    assert not erro, d
    s = d["seguranca"]
    assert s["q_max_kpa"] > 0 and 0 < s["t_q_max_s"] < d["tempo_ate_apogeu_s"]
    assert s["aceleracao_max_subida_g"] == pytest.approx(d["aceleracao_max_ms2"] / 9.80665, rel=0.05)
    assert s["empuxo_peso_medio"] > 1 and s["empuxo_peso_saida_trilho"] > 1
    assert len(s["descida"]) == 1 and s["descida"][0]["apos_abertura"] == "Parachute"
    assert 0 < s["descida"][0]["velocidade_ms"] < 15
    assert s["energia_impacto_j"] > 0
    assert "nota" in s["flutter"]  # sem material, sem flutter


@pytest.mark.anyio
async def test_flutter_com_material(chamar):
    erro, d = await chamar("simular_voo", arquivo_ork="foguete.ork", material_aletas="alumínio")
    assert not erro, d
    (f,) = d["seguranca"]["flutter"]
    assert f["aletas"] == "Trapezoidal fin set"
    assert f["fator_min"] > 1.5 and f["velocidade_flutter_ms"] > f["velocidade_ms"]
    erro, fraco = await chamar("simular_voo", arquivo_ork="foguete.ork", modulo_cisalhamento_aletas_gpa=0.001)
    assert fraco["seguranca"]["flutter"][0]["fator_min"] < 1.5
    assert any("Flutter" in a for a in fraco["alertas_projeto"])


@pytest.mark.anyio
async def test_paraquedas_pequeno_gera_alerta_de_descida(chamar):
    erro, e = await chamar("editar_foguete", arquivo_ork="foguete.ork", salvar_como="seg/paraquedas_pequeno.ork",
                           alteracoes={"Parachute.diameter_mm": 80})
    assert not erro, e
    erro, d = await chamar("simular_voo", arquivo_ork="seg/paraquedas_pequeno.ork")
    assert d["seguranca"]["descida"][0]["velocidade_ms"] > 9
    assert any("Descida final" in a for a in d["alertas_projeto"])


@pytest.mark.parametrize("kw, trecho", [
    ({"material_aletas": "balsa"}, "fora da tabela"),
    ({"material_aletas": "aco", "modulo_cisalhamento_aletas_gpa": 80}, "OU"),
    ({"modulo_cisalhamento_aletas_gpa": -1}, "> 0"),
])
@pytest.mark.anyio
async def test_material_invalido(chamar, kw, trecho):
    erro, msg = await chamar("simular_voo", arquivo_ork="foguete.ork", **kw)
    assert erro and trecho in msg


@pytest.mark.anyio
async def test_otimizar_metrica_de_seguranca(chamar):
    erro, d = await chamar("otimizar_parametro", arquivo_ork="foguete.ork", parametro="Parachute.diameter_mm",
                           minimo=150, maximo=600, metrica="velocidade_descida_final_ms", meta=4.0, tolerancia=0.1)
    assert not erro, d
    assert d["convergiu"] and d["metrica_no_otimo"] == pytest.approx(4.0, abs=0.1)
