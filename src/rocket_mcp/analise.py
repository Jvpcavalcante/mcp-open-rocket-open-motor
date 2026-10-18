"""Algoritmos de análise sem dependência do OpenRocket/motorlib (testáveis sem JVM).
- otimização 1D: bisseção para meta numérica, malha + seção áurea para mínimo/máximo;
- estatística da dispersão (Monte Carlo): resumo de distribuições, elipse de pouso, alertas.
"""
from __future__ import annotations

import math
import re
from typing import Callable

import numpy as np

from mcp.server.mcpserver.exceptions import ToolError

# ------------------------------------------------------------------------------------------- métricas

# Métrica -> extrator do resultado de _voar (server.py). Todas numéricas; None vira erro na avaliação.
METRICAS_VOO: dict[str, Callable[[dict], float | None]] = {
    "apogeu_m": lambda r: r["apogeu_m"],
    "velocidade_max_ms": lambda r: r["velocidade_max_ms"],
    "mach_max": lambda r: r["mach_max"],
    "aceleracao_max_ms2": lambda r: r["aceleracao_max_ms2"],
    "tempo_ate_apogeu_s": lambda r: r["tempo_ate_apogeu_s"],
    "tempo_voo_s": lambda r: r["tempo_voo_s"],
    "velocidade_saida_trilho_ms": lambda r: r["velocidade_saida_trilho_ms"],
    "velocidade_abertura_recuperacao_ms": lambda r: r["velocidade_abertura_recuperacao_ms"],
    "velocidade_impacto_ms": lambda r: r["velocidade_impacto_ms"],
    "atraso_otimo_s": lambda r: r["atraso_otimo_s"],
    "estabilidade_saida_trilho_cal": lambda r: r["estabilidade_voo"]["saida_trilho_cal"],
    "estabilidade_min_subida_cal": lambda r: r["estabilidade_voo"]["min_subida_cal"],
    "distancia_pouso_m": lambda r: (r.get("pouso") or {}).get("distancia_m"),
    "margem_estatica_cal": lambda r: r["estabilidade"]["margem_estatica_cal"],
    "margem_estatica_burnout_cal": lambda r: r["estabilidade"]["margem_estatica_burnout_cal"],
    "massa_decolagem_kg": lambda r: r["estabilidade"]["massa_decolagem_kg"],
    "massa_burnout_kg": lambda r: r["estabilidade"]["massa_burnout_kg"],
    "cg_mm": lambda r: r["estabilidade"]["cg_mm"],
    "cp_mm": lambda r: r["estabilidade"]["cp_mm"],
    "q_max_kpa": lambda r: r["seguranca"]["q_max_kpa"],
    "aceleracao_max_subida_g": lambda r: r["seguranca"]["aceleracao_max_subida_g"],
    "aceleracao_max_recuperacao_g": lambda r: r["seguranca"].get("aceleracao_max_recuperacao_g"),
    "empuxo_peso_medio": lambda r: r["seguranca"].get("empuxo_peso_medio"),
    "empuxo_peso_saida_trilho": lambda r: r["seguranca"].get("empuxo_peso_saida_trilho"),
    "velocidade_descida_final_ms": lambda r: (r["seguranca"]["descida"] or [{}])[-1].get("velocidade_ms"),
    "energia_impacto_j": lambda r: r["seguranca"]["energia_impacto_j"],
}
# Saem da estabilidade estática: não precisam simular o voo quando o parâmetro é do foguete
METRICAS_ESTATICAS = {"margem_estatica_cal", "margem_estatica_burnout_cal", "massa_decolagem_kg",
                      "massa_burnout_kg", "cg_mm", "cp_mm"}
# Métrica -> chave do resumo da simulação do motor (om_bridge.resumo)
METRICAS_MOTOR = ("impulso_total_Ns", "empuxo_medio_N", "tempo_queima_s", "isp_s", "pressao_max_MPa",
                  "kn_inicial", "kn_pico", "massa_propelente_kg", "fluxo_massico_pico_kg_m2s")


# ----------------------------------------------------------------------------------------- otimização

class _Registro:
    """Avalia f com cache e limite de avaliações, guardando o histórico."""

    def __init__(self, f: Callable[[float], float], max_avaliacoes: int):
        self.f, self.max, self.pontos = f, max_avaliacoes, {}

    def __call__(self, x: float) -> float:
        x = float(x)
        if x not in self.pontos:
            if len(self.pontos) >= self.max:
                raise _Esgotado
            y = self.f(x)
            if y is None or not math.isfinite(y):
                raise ToolError(f"A métrica não pôde ser calculada para o valor {x:g}")
            self.pontos[x] = float(y)
        return self.pontos[x]

    def historico(self) -> list[dict]:
        return [{"valor": _r(x), "metrica": y} for x, y in sorted(self.pontos.items())]


class _Esgotado(Exception):
    pass


def _r(x: float) -> float:
    return float(f"{x:.6g}")


def buscar_meta(f, a: float, b: float, meta: float, tol_metrica: float | None = None,
                max_avaliacoes: int = 20) -> dict:
    """Bisseção de g(x) = f(x) - meta em [a, b]. Exige mudança de sinal; senão devolve o extremo mais próximo
    e explica o alcance da métrica no intervalo."""
    reg = _Registro(f, max_avaliacoes)
    tol = tol_metrica if tol_metrica is not None else max(abs(meta) * 1e-3, 1e-6)
    ya, yb = reg(a), reg(b)
    if (ya - meta) * (yb - meta) > 0:
        x, y = (a, ya) if abs(ya - meta) <= abs(yb - meta) else (b, yb)
        return {"convergiu": False, "valor": _r(x), "metrica": y, "historico": reg.historico(),
                "mensagem": f"A meta {meta:g} está fora do alcance no intervalo: a métrica vai de {ya:g} "
                            f"(em {a:g}) a {yb:g} (em {b:g}). Amplie o intervalo ou revise a meta."}
    lo, hi, ylo = a, b, ya
    try:
        while abs(ya - meta) > tol and abs(yb - meta) > tol:
            m = (lo + hi) / 2
            ym = reg(m)
            if abs(ym - meta) <= tol or abs(hi - lo) <= abs(b - a) * 1e-6:
                break
            if (ylo - meta) * (ym - meta) <= 0:
                hi = m
            else:
                lo, ylo = m, ym
    except _Esgotado:
        pass
    x = min(reg.pontos, key=lambda k: abs(reg.pontos[k] - meta))
    y = reg.pontos[x]
    convergiu = abs(y - meta) <= tol
    return {"convergiu": convergiu, "valor": _r(x), "metrica": y, "historico": reg.historico(),
            "mensagem": "Meta atingida dentro da tolerância" if convergiu else
            f"Limite de {max_avaliacoes} avaliações: melhor ponto a {abs(y - meta):g} da meta"}


def buscar_extremo(f, a: float, b: float, maximizar: bool, max_avaliacoes: int = 20,
                   tol_x: float | None = None) -> dict:
    """Malha grossa (até 7 pontos) para achar a região do extremo e seção áurea dentro dela.
    Supõe a métrica unimodal perto do extremo; a malha protege contra extremos locais óbvios."""
    reg = _Registro(f, max_avaliacoes)
    tol_x = tol_x if tol_x is not None else abs(b - a) * 1e-3
    sinal = -1.0 if maximizar else 1.0  # minimiza sinal*f
    n = max(3, min(7, max_avaliacoes // 2))
    malha = [a + (b - a) * i / (n - 1) for i in range(n)]
    try:
        vals = [sinal * reg(x) for x in malha]
        i = min(range(n), key=lambda k: vals[k])
        lo, hi = malha[max(i - 1, 0)], malha[min(i + 1, n - 1)]
        g = (math.sqrt(5) - 1) / 2
        c, d = hi - g * (hi - lo), lo + g * (hi - lo)
        while abs(hi - lo) > tol_x:
            if sinal * reg(c) <= sinal * reg(d):
                hi, d = d, c
                c = hi - g * (hi - lo)
            else:
                lo, c = c, d
                d = lo + g * (hi - lo)
    except _Esgotado:
        pass
    x = min(reg.pontos, key=lambda k: sinal * reg.pontos[k])
    no_limite = x in (a, b)
    return {"convergiu": not no_limite, "valor": _r(x), "metrica": reg.pontos[x], "historico": reg.historico(),
            "mensagem": ("O extremo ficou no limite do intervalo: o ótimo pode estar fora dele" if no_limite
                         else "Extremo encontrado (supondo uma única região de ótimo)")}


# ------------------------------------------------------------------------------------------ dispersão

def resumo_distribuicao(valores: list[float | None]) -> dict | None:
    """Média, desvio, percentis 5/50/95, mínimo e máximo (ignora None)."""
    v = np.array([x for x in valores if x is not None], dtype=float)
    if v.size == 0:
        return None
    p5, p50, p95 = np.percentile(v, [5, 50, 95])
    return {"media": _r(v.mean()), "desvio": _r(v.std(ddof=1) if v.size > 1 else 0.0), "p5": _r(p5),
            "p50": _r(p50), "p95": _r(p95), "min": _r(v.min()), "max": _r(v.max())}


CHI2_95_2GL = 5.991464547107979  # quantil 95% da qui-quadrado com 2 graus de liberdade


def elipse_95(leste: list[float], norte: list[float]) -> dict | None:
    """Elipse que contém ~95% dos pousos, supondo distribuição normal bivariada. O rumo do eixo maior é
    medido a partir do norte, no sentido horário (0-180). Também devolve o raio, a partir da base, que
    contém 95% dos pousos (empírico)."""
    if len(leste) < 3:
        return None
    pts = np.column_stack([leste, norte]).astype(float)
    centro = pts.mean(axis=0)
    autoval, autovet = np.linalg.eigh(np.cov(pts, rowvar=False))  # crescente
    maior = autovet[:, 1]
    rumo = math.degrees(math.atan2(maior[0], maior[1])) % 180
    dist = np.hypot(pts[:, 0], pts[:, 1])
    return {"centro_leste_m": _r(centro[0]), "centro_norte_m": _r(centro[1]),
            "semieixo_maior_m": _r(math.sqrt(max(autoval[1], 0) * CHI2_95_2GL)),
            "semieixo_menor_m": _r(math.sqrt(max(autoval[0], 0) * CHI2_95_2GL)),
            "rumo_eixo_maior_graus": round(rumo), "raio_95_da_base_m": _r(np.percentile(dist, 95)),
            "distancia_max_da_base_m": _r(dist.max())}


def agrupar_alertas(listas: list[list[str]]) -> list[dict]:
    """Fração dos voos com cada tipo de alerta. O tipo é o texto sem os números."""
    n = len(listas)
    contagem: dict[str, int] = {}
    exemplo: dict[str, str] = {}
    for alertas in listas:
        for tipo in {re.sub(r"-?\d+(?:[.,]\d+)?", "#", a) for a in alertas}:
            contagem[tipo] = contagem.get(tipo, 0) + 1
            exemplo.setdefault(tipo, next(a for a in alertas if re.sub(r"-?\d+(?:[.,]\d+)?", "#", a) == tipo))
    return sorted(({"tipo": t, "fracao_voos": round(c / n, 3), "exemplo": exemplo[t]} for t, c in contagem.items()),
                  key=lambda d: -d["fracao_voos"])


# ------------------------------------------------------------------------------- curvas de empuxo

def ler_eng(path) -> dict:
    """Curva RASP: cabeçalho 'nome diam comp atrasos m_prop m_total fab' e pares 't F'. ';' comenta."""
    from pathlib import Path

    linhas = [l.split(";")[0].strip() for l in Path(path).read_text(encoding="utf-8").splitlines()]
    linhas = [l for l in linhas if l]
    if len(linhas) < 3:
        raise ToolError(f"{Path(path).name}: .eng sem pontos de empuxo")
    cab = linhas[0].split()
    try:
        pts = [tuple(map(float, l.split()[:2])) for l in linhas[1:]]
        massa_prop = float(cab[4])
    except (ValueError, IndexError) as e:
        raise ToolError(f"{Path(path).name}: .eng malformado ({e})") from e
    if pts[0][0] > 0:
        pts.insert(0, (0.0, 0.0))  # o RASP começa implicitamente em (0, 0)
    t, f = (np.array(x) for x in zip(*pts))
    return {"t": t, "f": f, "nome": cab[0], "massa_propelente_kg": massa_prop}


_UNIDADES_CSV = {  # trecho do cabeçalho -> fator para SI
    "tempo": [("(ms)", 1e-3), ("ms", 1e-3)],
    "empuxo": [("lbf", 4.448222), ("kgf", 9.80665), ("kn", 1e3)],
    "pressao": [("mpa", 1e6), ("kpa", 1e3), ("psi", 6894.757), ("bar", 1e5)],
}
_NOMES_CSV = {"tempo": ("time", "tempo", "t"), "empuxo": ("force", "thrust", "empuxo", "forca", "força"),
              "pressao": ("pressure", "pressao", "pressão")}


def _coluna(cabecalho: list[str], tipo: str, pedido: str | None) -> int | None:
    norm = [c.strip().lower() for c in cabecalho]
    if pedido is not None:
        if pedido.strip().lower() not in norm:
            raise ToolError(f"Coluna '{pedido}' não existe no CSV. Colunas: {cabecalho}")
        return norm.index(pedido.strip().lower())
    for i, c in enumerate(norm):
        base = c.split("(")[0].strip()
        if base in _NOMES_CSV[tipo] or any(c.startswith(n + s) for n in _NOMES_CSV[tipo] for s in (" ", "(", "_")):
            return i
    return None


def _fator(cabecalho: str, tipo: str) -> float:
    c = cabecalho.lower()
    return next((f for trecho, f in _UNIDADES_CSV[tipo] if trecho in c), 1.0)


def ler_csv_curva(path, coluna_tempo=None, coluna_empuxo=None, coluna_pressao=None) -> dict:
    """CSV de ensaio estático. Detecta colunas por nome (time/tempo, force/thrust/empuxo, pressure/pressao) e
    unidades pelo cabeçalho (s ou ms; N, kN, lbf ou kgf; Pa, kPa, MPa, psi ou bar). Formato do openMotor:
    'time (s),force (n),pressure (pa)'."""
    import csv
    from pathlib import Path

    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        linhas = [l for l in csv.reader(fh) if l and any(x.strip() for x in l)]
    if len(linhas) < 3:
        raise ToolError(f"{Path(path).name}: CSV sem dados suficientes")
    cab = linhas[0]
    it = _coluna(cab, "tempo", coluna_tempo)
    ie = _coluna(cab, "empuxo", coluna_empuxo)
    if it is None or ie is None:
        raise ToolError(f"Não achei as colunas de tempo/empuxo em {cab}: informe coluna_tempo e coluna_empuxo")
    ip = _coluna(cab, "pressao", coluna_pressao)
    try:
        dados = np.array([[float(l[i]) for i in (it, ie) + ((ip,) if ip is not None else ())] for l in linhas[1:]])
    except (ValueError, IndexError) as e:
        raise ToolError(f"{Path(path).name}: valor não numérico ({e})") from e
    out = {"t": dados[:, 0] * _fator(cab[it], "tempo"), "f": dados[:, 1] * _fator(cab[ie], "empuxo"),
           "nome": Path(path).stem}
    if ip is not None:
        out["p"] = dados[:, 2] * _fator(cab[ip], "pressao")
    ordem = np.argsort(out["t"], kind="stable")
    return {k: (v[ordem] if isinstance(v, np.ndarray) else v) for k, v in out.items()}


def metricas_curva(t, f, massa_propelente_kg: float | None = None) -> dict:
    """Impulso (trapézios), pico, queima (entre a 1ª e a última vez acima de 5% do pico) e Isp."""
    t, f = np.asarray(t, float), np.asarray(f, float)
    impulso = float(np.trapezoid(f, t))
    i_pico = int(np.argmax(f))
    acima = np.nonzero(f >= 0.05 * f[i_pico])[0]
    queima = float(t[acima[-1]] - t[acima[0]]) if acima.size else 0.0
    out = {"impulso_total_Ns": _r(impulso), "empuxo_pico_N": _r(f[i_pico]), "t_pico_s": _r(t[i_pico]),
           "tempo_queima_s": _r(queima), "empuxo_medio_N": _r(impulso / queima) if queima > 0 else None}
    if massa_propelente_kg:
        out["isp_s"] = _r(impulso / (massa_propelente_kg * 9.80665))
    return out


def erro_rms(ref: dict, cmp: dict, n: int = 600) -> float:
    """RMS da diferença de empuxo (N) numa grade comum, de 0 ao fim da curva mais longa."""
    fim = max(ref["t"][-1], cmp["t"][-1])
    tg = np.linspace(0, fim, n)
    return float(np.sqrt(np.mean((np.interp(tg, ref["t"], ref["f"], right=0) -
                                  np.interp(tg, cmp["t"], cmp["f"], right=0)) ** 2)))


def comparar_curvas(ref: dict, cmp: dict) -> dict:
    mr = metricas_curva(ref["t"], ref["f"], ref.get("massa_propelente_kg"))
    mc = metricas_curva(cmp["t"], cmp["f"], cmp.get("massa_propelente_kg"))
    dif = {k: _r(100 * (mc[k] - mr[k]) / mr[k]) for k in mr if mr.get(k) and mc.get(k) is not None}
    rms = erro_rms(ref, cmp)
    out = {"referencia": mr, "comparada": mc, "diferenca_pct": dif, "erro_rms_N": _r(rms),
           "erro_rms_pct_do_medio": _r(100 * rms / mr["empuxo_medio_N"]) if mr.get("empuxo_medio_N") else None}
    if "p" in ref and "p" in cmp:
        out["pressao_max_MPa"] = {"referencia": _r(ref["p"].max() / 1e6), "comparada": _r(cmp["p"].max() / 1e6)}
    return out


# ---------------------------------------------------------------------------- log de voo (altímetro)

_NOMES_CSV["altitude"] = ("altitude", "alt", "height", "altura", "agl", "baro_alt", "altitude_agl")
_UNIDADES_CSV["altitude"] = [("(ft)", 0.3048), ("feet", 0.3048), ("_ft", 0.3048)]


def ler_log_altitude(path, coluna_tempo=None, coluna_altitude=None, unidade_altitude=None) -> dict:
    """Log de altímetro em CSV: tempo (s ou ms) e altitude (m ou ft). A altitude fica relativa ao 1º ponto
    (o simulador trabalha acima da base). unidade_altitude 'm'/'ft' força a unidade; sem ela, o cabeçalho."""
    import csv
    from pathlib import Path

    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        linhas = [l for l in csv.reader(fh) if l and any(x.strip() for x in l)]
    if len(linhas) < 10:
        raise ToolError(f"{Path(path).name}: log com poucos pontos")
    cab = linhas[0]
    it, ia = _coluna(cab, "tempo", coluna_tempo), _coluna(cab, "altitude", coluna_altitude)
    if it is None or ia is None:
        raise ToolError(f"Não achei as colunas de tempo/altitude em {cab}: informe coluna_tempo e coluna_altitude")
    try:
        d = np.array([[float(l[it]), float(l[ia])] for l in linhas[1:] if l[it].strip() and l[ia].strip()])
    except (ValueError, IndexError) as e:
        raise ToolError(f"{Path(path).name}: valor não numérico ({e})") from e
    if unidade_altitude not in (None, "m", "ft"):
        raise ToolError("unidade_altitude: 'm' ou 'ft'")
    fa = {"m": 1.0, "ft": 0.3048}[unidade_altitude] if unidade_altitude else _fator(cab[ia], "altitude")
    d = d[np.argsort(d[:, 0], kind="stable")]
    return {"t": d[:, 0] * _fator(cab[it], "tempo"), "alt": (d[:, 1] - d[0, 1]) * fa}


def _cruzamento(t, a, limiar):
    i = int(np.argmax(a >= limiar))
    if a[i] < limiar:
        raise ToolError(f"A série nunca passa de {limiar} m")
    if i == 0:
        return float(t[0])
    return float(t[i - 1] + (limiar - a[i - 1]) * (t[i] - t[i - 1]) / (a[i] - a[i - 1]))


def erro_altitude(log: dict, t_sim, a_sim, limiar: float = 10.0) -> dict:
    """RMS da altitude na subida (do cruzamento de `limiar` m até o apogeu do log), com as séries alinhadas
    no instante em que cada uma passa do limiar (o relógio do altímetro não começa na ignição)."""
    t_log, a_log = log["t"], log["alt"]
    t_sim, a_sim = np.asarray(t_sim, float), np.asarray(a_sim, float)
    t0_log, t0_sim = _cruzamento(t_log, a_log, limiar), _cruzamento(t_sim, a_sim, limiar)
    i_apo = int(np.argmax(a_log))
    janela = (t_log >= t0_log) & (t_log <= t_log[i_apo])
    if janela.sum() < 5:
        raise ToolError("Poucos pontos de subida no log para comparar")
    sim = np.interp(t_log[janela] - t0_log + t0_sim, t_sim, a_sim)
    return {"erro_rms_m": float(np.sqrt(np.mean((sim - a_log[janela]) ** 2))), "pontos": int(janela.sum()),
            "apogeu_log_m": float(a_log[i_apo]), "apogeu_sim_m": float(a_sim.max())}
