"""Estudo de flutter de aletas compósitas (docs/FLUTTER.md §3.2, §12 e §13), sem JVM: leitura e validação
do fins_config.yaml, critérios de aceitação, sensibilidade e limpeza do resultado para JSON.
O cálculo em si fica em fin_flutter.py; a ligação com o OpenRocket, em server.py."""
from __future__ import annotations

import math
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import fin_flutter as ff

PROPRIEDADES = ("E1", "E2", "G12", "nu12", "rho")
ANALISE_PADRAO = {"e_ea": 0.40, "eta_ref": 0.75, "g_s": 0.0}
# §12. margem_flutter_calibrada vale quando f_h e f_a vêm de tap test/FEM.
CRITERIOS_PADRAO = {"margem_flutter": 1.5, "margem_flutter_calibrada": 1.15, "margem_transonico": 1.7,
                    "margem_divergencia": 1.32, "margem_tn4197": 1.0, "d16_d11": 0.1,
                    "estabilidade_min_cal": 1.5, "estabilidade_max_cal": 3.0}


def _numero(v, onde: str, minimo: float | None = None, maximo: float | None = None, aberto: bool = False) -> float:
    """Número do YAML. ARMADILHA: o PyYAML (YAML 1.1) lê '55.0e9' como TEXTO (o expoente precisa de sinal:
    55.0e+9); por isso texto numérico também é aceito."""
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            pass
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ToolError(f"{onde}: esperado um número, recebido {v!r}")
    if minimo is not None and (v <= minimo if aberto else v < minimo):
        raise ToolError(f"{onde} = {v} fora da faixa (mínimo {minimo}{', exclusive' if aberto else ''})")
    if maximo is not None and v > maximo:
        raise ToolError(f"{onde} = {v} acima do máximo {maximo}")
    return float(v)


def carregar_config(dados: Any) -> dict:
    """Valida e normaliza o fins_config (já lido do YAML). Devolve materials, laminates, analysis,
    aletas (conjunto do .ork -> laminado e frequências medidas), criterios e placeholders."""
    if not isinstance(dados, dict):
        raise ToolError("Configuração de aletas: esperado um mapeamento com materials e laminates")
    desconhecidas = sorted(set(dados) - {"materials", "laminates", "analysis", "aletas", "criterios"})
    if desconhecidas:
        raise ToolError(f"Configuração: chave(s) desconhecida(s) {desconhecidas}")
    mats_in, lams_in = dados.get("materials"), dados.get("laminates")
    if not isinstance(mats_in, dict) or not mats_in:
        raise ToolError("Configuração: 'materials' vazio")
    if not isinstance(lams_in, dict) or not lams_in:
        raise ToolError("Configuração: 'laminates' vazio")
    materials, placeholders = {}, []
    for nome, m in mats_in.items():
        if not isinstance(m, dict):
            raise ToolError(f"materials.{nome}: esperado um mapeamento com {PROPRIEDADES}")
        faltam = [p for p in PROPRIEDADES if p not in m]
        if faltam:
            raise ToolError(f"materials.{nome}: faltam {faltam} (propriedades da LÂMINA, SI)")
        materials[str(nome)] = {
            "E1": _numero(m["E1"], f"materials.{nome}.E1", 0, aberto=True),
            "E2": _numero(m["E2"], f"materials.{nome}.E2", 0, aberto=True),
            "G12": _numero(m["G12"], f"materials.{nome}.G12", 0, aberto=True),
            "nu12": _numero(m["nu12"], f"materials.{nome}.nu12", 0, 0.5),
            "rho": _numero(m["rho"], f"materials.{nome}.rho", 0, aberto=True),
            "source": str(m.get("source", "não informado")),
        }
        if materials[str(nome)]["E1"] < 1e8:
            raise ToolError(f"materials.{nome}.E1 = {materials[str(nome)]['E1']}: os módulos vão em Pa (55 GPa = 55.0e+9)")
        if materials[str(nome)]["source"].strip().lower() in ("placeholder", "não informado"):
            placeholders.append(str(nome))
    laminates = {}
    for nome, plies in lams_in.items():
        if not isinstance(plies, list) or not plies:
            raise ToolError(f"laminates.{nome}: esperado uma lista de lâminas {{mat, theta_deg, t}}")
        lista = []
        for i, p in enumerate(plies):
            onde = f"laminates.{nome}[{i}]"
            if not isinstance(p, dict) or not {"mat", "theta_deg", "t"} <= p.keys():
                raise ToolError(f"{onde}: esperado {{mat, theta_deg, t}}")
            if str(p["mat"]) not in materials:
                raise ToolError(f"{onde}: material '{p['mat']}' não definido em materials {list(materials)}")
            t = _numero(p["t"], f"{onde}.t", 0, aberto=True)
            if t > 0.01:
                raise ToolError(f"{onde}.t = {t} m: a espessura da lâmina vai em metros (0,3 mm = 0.30e-3)")
            lista.append({"mat": str(p["mat"]), "theta_deg": _numero(p["theta_deg"], f"{onde}.theta_deg", -90, 180),
                          "t": t})
        laminates[str(nome)] = lista
    analise = dict(ANALISE_PADRAO)
    for k, v in (dados.get("analysis") or {}).items():
        if k not in ANALISE_PADRAO:
            raise ToolError(f"analysis.{k}: desconhecida. Aceitas: {list(ANALISE_PADRAO)}")
        analise[k] = _numero(v, f"analysis.{k}", 0, 0.99 if k == "g_s" else 1, aberto=k != "g_s")
    aletas = mapa_aletas(dados.get("aletas") or {}, laminates)
    criterios = dict(CRITERIOS_PADRAO)
    for k, v in (dados.get("criterios") or {}).items():
        if k not in CRITERIOS_PADRAO:
            raise ToolError(f"criterios.{k}: desconhecido. Aceitos: {list(CRITERIOS_PADRAO)}")
        criterios[k] = _numero(v, f"criterios.{k}", 0)
    return {"materials": materials, "laminates": laminates, "analysis": analise, "aletas": aletas,
            "criterios": criterios, "placeholders": placeholders}


def mapa_aletas(d: Any, laminates: dict, onde: str = "aletas") -> dict:
    """{caminho do conjunto de aletas: 'L6' ou {laminado, f_h_hz?, f_a_hz?}} -> {caminho: {laminado, freqs_hz}}."""
    if not isinstance(d, dict):
        raise ToolError(f"{onde}: esperado {{'Caminho/Das/Aletas': 'laminado'}}")
    out = {}
    for caminho, a in d.items():
        a = {"laminado": a} if isinstance(a, str) else a
        if not isinstance(a, dict) or "laminado" not in a:
            raise ToolError(f"{onde}.{caminho}: esperado {{laminado: <nome>, f_h_hz?, f_a_hz?}}")
        if a["laminado"] not in laminates:
            raise ToolError(f"{onde}.{caminho}: laminado '{a['laminado']}' não definido em laminates {list(laminates)}")
        out[str(caminho)] = {"laminado": a["laminado"], "freqs_hz": frequencias(a, f"{onde}.{caminho}")}
    return out


def frequencias(d: dict, onde: str) -> tuple[float, float] | None:
    """(f_h, f_a) medidos, se informados juntos."""
    fh, fa = d.get("f_h_hz"), d.get("f_a_hz")
    if fh is None and fa is None:
        return None
    if fh is None or fa is None:
        raise ToolError(f"{onde}: informe f_h_hz E f_a_hz (flexão e torção medidas)")
    return _numero(fh, f"{onde}.f_h_hz", 0, aberto=True), _numero(fa, f"{onde}.f_a_hz", 0, aberto=True)


def propriedades_laminado(cfg: dict, nome: str) -> dict:
    """h, massa por área, densidade equivalente e matriz D do laminado (para escrever no .ork)."""
    plies = cfg["laminates"][nome]
    A, B, D, h, mA = ff.laminate(plies, cfg["materials"])
    usados = sorted({p["mat"] for p in plies})
    return {"laminado": nome, "laminas": len(plies), "h_mm": round(h * 1e3, 4), "massa_area_kg_m2": round(mA, 4),
            "rho_eq_kg_m3": round(mA / h, 2), "D_Nm": {"D11": _sig(D[0, 0]), "D22": _sig(D[1, 1]),
                                                      "D12": _sig(D[0, 1]), "D66": _sig(D[2, 2]),
                                                      "D16": _sig(D[0, 2]), "D26": _sig(D[1, 2])},
            "materiais": usados, "placeholders": [m for m in usados if m in cfg["placeholders"]],
            "h": h, "rho_eq": mA / h}


def _sig(x: float) -> float:
    return float(f"{x:.5g}")


def avaliar(res: dict, criterios: dict, estabilidade: dict | None = None) -> dict:
    """Critérios da §12 sobre o resultado de run_case (e a estabilidade do OpenRocket, se houver).
    A margem de flutter usada é a menor entre a do documento (Mach do ponto) e a do ponto casado.
    O critério transônico vale para TODO ponto transônico, não só para o crítico."""
    calibrada = bool(res.get("frequencias_medidas"))
    lim_flutter = criterios["margem_flutter_calibrada" if calibrada else "margem_flutter"]
    margem = min(res["margem_flutter"], res.get("margem_flutter_casada", math.inf))
    trans = [p["margem"] for p in res.get("pontos", []) if p["regime"] == "transonico"]
    verificacoes = [
        _verif("margem_flutter", margem, ">=", lim_flutter,
               "V-g, mínimo na trajetória" + (" (frequências medidas)" if calibrada else "")),
        _verif("margem_divergencia", res["margem_q_divergencia"], ">=", criterios["margem_divergencia"], "q_D/q"),
        _verif("laminado_simetrico", res["B_relativo"], "<=", 1e-9, "B = 0 (relativo a A·h)"),
    ]
    if trans:
        verificacoes.append(_verif("margem_transonico", min(trans), ">=", criterios["margem_transonico"],
                                   "método linear fora da validade entre Mach 0,8 e 1,2"))
    if estabilidade:
        for nome, chave, op, lim in (("estabilidade_saida_trilho", "saida_trilho_cal", ">=", "estabilidade_min_cal"),
                                     ("estabilidade_min_subida", "min_subida_cal", ">=", "estabilidade_min_cal"),
                                     ("estabilidade_max_subida", "max_subida_cal", "<=", "estabilidade_max_cal")):
            if estabilidade.get(chave) is not None:
                verificacoes.append(_verif(nome, estabilidade[chave], op, criterios[lim],
                                           "OpenRocket, calibres (subida = saída do trilho até o apogeu)",
                                           "estabilidade"))
    alertas = []
    if res["margem_TN4197"] < criterios["margem_tn4197"]:
        alertas.append(f"TN-4197 abaixo de {criterios['margem_tn4197']} ({res['margem_TN4197']:.2f})"
                       + (": V-g passa, validar com ensaio (tap test)" if margem >= lim_flutter else ""))
    if res["D16_D11"] > criterios["d16_d11"]:
        alertas.append(f"D16/D11 = {res['D16_D11']:.3f} > {criterios['d16_d11']}: acoplamento flexão-torção que a "
                       "seção típica não representa (laminado desbalanceado)")
    if res.get("regime_critico") == "transonico":
        alertas.append("Ponto crítico no transônico (Mach 0,8–1,2): nenhum método linear é confiável")
    if not calibrada:
        alertas.append("Frequências calculadas (placa engastada ideal, não conservador se a raiz for flexível): "
                       "meça f_h e f_a por tap test")
    return {"aprovado": all(v["ok"] for v in verificacoes),
            "aprovado_flutter": all(v["ok"] for v in verificacoes if v["tipo"] == "estrutural"),
            "verificacoes": verificacoes, "alertas": alertas}


def _verif(nome, valor, op, limite, nota, tipo: str = "estrutural") -> dict:
    ok = valor <= limite if op == "<=" else valor >= limite
    return {"verificacao": nome, "tipo": tipo, "valor": _limpo(valor), "criterio": f"{op} {limite}", "ok": bool(ok),
            "nota": nota}


CASOS_SENSIBILIDADE = (("base", {}), ("e_ea = 0,35", {"e_ea": 0.35}), ("e_ea = 0,50", {"e_ea": 0.50}),
                       ("eta_ref = 0,70", {"eta_ref": 0.70}), ("eta_ref = 0,80", {"eta_ref": 0.80}),
                       ("G12 -30%", {"g12": 0.7}))


def sensibilidade(fin, plies, materials, traj, analise: dict, criterios: dict, freqs_hz=None) -> dict:
    """§13: e_ea 0,35/0,50, eta_ref 0,70/0,80 e G12 -30% (todos os materiais), na mesma trajetória.
    Com frequências medidas, G12 só muda a TN-4197 (as frequências não são recalculadas)."""
    linhas = []
    for nome, mudanca in CASOS_SENSIBILIDADE:
        kw = {"e_ea": analise["e_ea"], "eta_ref": analise["eta_ref"], "gs": analise["g_s"]}
        kw |= {k: v for k, v in mudanca.items() if k != "g12"}
        mats = materials
        if "g12" in mudanca:
            mats = {k: m | {"G12": m["G12"] * mudanca["g12"]} for k, m in materials.items()}
        r = ff.run_case(fin, plies, mats, traj, freqs_hz=freqs_hz, **kw)
        av = avaliar(r, criterios)
        linhas.append({"caso": nome, "margem_flutter": _limpo(r["margem_flutter"]),
                       "margem_flutter_casada": _limpo(r["margem_flutter_casada"]),
                       "regime_critico": r["regime_critico"], "t_critico_s": _limpo(r["t_critico"]),
                       "margem_q_divergencia": _limpo(r["margem_q_divergencia"]), "aprovado": av["aprovado"]})
    return {"casos": linhas, "decisao_muda": any(x["aprovado"] != linhas[0]["aprovado"] for x in linhas[1:])}


def _limpo(v):
    """inf/NaN não existem em JSON: viram None (as margens infinitas significam 'sem flutter na faixa')."""
    if isinstance(v, float):
        return round(v, 6) if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _limpo(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_limpo(x) for x in v]
    return v


def resumo(res: dict) -> dict:
    """Resultado de run_case pronto para JSON, sem a lista de pontos (vai para o CSV)."""
    out = _limpo({k: v for k, v in res.items() if k != "pontos"})
    for k in ("margem_flutter", "margem_flutter_casada", "margem_q_divergencia"):
        if res.get(k) is not None and not math.isfinite(res[k]):
            out.setdefault("notas", []).append(f"{k} = null: sem instabilidade na faixa analisada")
    return out
