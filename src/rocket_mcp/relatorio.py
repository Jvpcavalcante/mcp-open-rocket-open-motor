"""Relatório de voo em HTML autocontido (gráficos SVG embutidos), para revisão da equipe ou impressão em PDF.
Usa a API orientada a objetos do matplotlib (Figure, sem pyplot): sem estado global, segura em threads e
sem janela. Todo texto vindo de arquivos passa por html.escape."""
from __future__ import annotations

import html
import io
from datetime import datetime

import matplotlib
import numpy as np
from matplotlib.figure import Figure
from matplotlib.patches import Ellipse

matplotlib.rcParams.update({"svg.fonttype": "none", "font.size": 9, "axes.grid": True, "grid.alpha": 0.3,
                            "axes.spines.top": False, "axes.spines.right": False})

AZUL, LARANJA, VERDE, VERMELHO, CINZA = "#2563eb", "#ea580c", "#16a34a", "#dc2626", "#6b7280"
_ROTULOS_EVENTOS = {"LAUNCHROD": "fim do trilho", "BURNOUT": "fim da queima", "APOGEE": "apogeu",
                    "RECOVERY_DEVICE_DEPLOYMENT": "abertura", "GROUND_HIT": "pouso"}


def _svg(fig: Figure) -> str:
    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight")
    s = buf.getvalue()
    return s[s.index("<svg"):]  # sem o cabeçalho XML, para embutir no HTML


def _eventos(ax, eventos, t_max=None):
    """Linhas verticais nos eventos. Rótulos próximos (< 4% da largura) descem de altura para não se
    sobrepor (ex.: apogeu e abertura do piloto 1 s depois)."""
    vistos, colocados = set(), []
    x0, x1 = ax.get_xlim()
    for e in eventos:
        nome = _ROTULOS_EVENTOS.get(e["evento"])
        if nome is None or (t_max is not None and e["t_s"] > t_max):
            continue
        ax.axvline(e["t_s"], color=CINZA, lw=0.8, ls="--")
        if (nome, round(e["t_s"])) in vistos:
            continue
        vistos.add((nome, round(e["t_s"])))
        nivel = sum(1 for x, _ in colocados if abs(x - e["t_s"]) < 0.04 * (x1 - x0))
        colocados.append((e["t_s"], nivel))
        ax.text(e["t_s"], 1.0 - 0.3 * (nivel % 3), f" {nome}", transform=ax.get_xaxis_transform(), rotation=90,
                va="top", ha="right", fontsize=7, color=CINZA)


def grafico_trajetoria(s: dict, eventos) -> str:
    fig = Figure(figsize=(7.2, 3.2))
    ax = fig.add_subplot()
    ax.plot(s["time_s"], s["altitude_m"], color=AZUL, lw=1.6, label="altitude")
    ax.set_xlabel("tempo (s)")
    ax.set_ylabel("altitude (m)", color=AZUL)
    ax2 = ax.twinx()
    ax2.plot(s["time_s"], s["velocity_total_ms"], color=LARANJA, lw=1.2, label="velocidade")
    ax2.set_ylabel("velocidade (m/s)", color=LARANJA)
    ax2.grid(False)
    _eventos(ax, eventos)
    return _svg(fig)


def grafico_subida(s: dict, eventos) -> str:
    t = np.asarray(s["time_s"])
    t_ini = next((e["t_s"] for e in eventos if e["evento"] == "LAUNCHROD"), 0.0)
    t_fim = next((e["t_s"] for e in eventos if e["evento"] == "APOGEE"), t[-1])
    m = (t >= t_ini) & (t <= t_fim)
    fig = Figure(figsize=(7.2, 3.0))
    ax = fig.add_subplot()
    ax.plot(t[m], np.asarray(s["stability_cal"])[m], color=VERDE, lw=1.6)
    ax.axhline(1.0, color=VERMELHO, lw=0.8, ls=":")
    ax.set_xlabel("tempo (s)")
    ax.set_ylabel("estabilidade (cal)", color=VERDE)
    ax2 = ax.twinx()
    ax2.plot(t[m], np.asarray(s["mach"])[m], color=AZUL, lw=1.1)
    ax2.axhline(0.8, color=AZUL, lw=0.6, ls=":")
    ax2.set_ylabel("Mach", color=AZUL)
    ax2.grid(False)
    _eventos(ax, eventos, t_fim)
    return _svg(fig)


def grafico_empuxo_voo(s: dict, eventos) -> str:
    t = np.asarray(s["time_s"])
    t_q = next((e["t_s"] for e in eventos if e["evento"] == "BURNOUT"), t[-1])
    m = t <= t_q * 1.4 + 0.5
    fig = Figure(figsize=(7.2, 2.8))
    ax = fig.add_subplot()
    ax.plot(t[m], np.asarray(s["thrust_N"])[m], color=LARANJA, lw=1.6, label="empuxo")
    ax.plot(t[m], np.asarray(s["drag_N"])[m], color=CINZA, lw=1.1, label="arrasto")
    ax.set_xlabel("tempo (s)")
    ax.set_ylabel("força (N)")
    ax.legend(loc="upper right", frameon=False)
    return _svg(fig)


def grafico_motor(curva: dict) -> str:
    fig = Figure(figsize=(7.2, 2.8))
    ax = fig.add_subplot()
    ax.plot(curva["t"], curva["f"], color=LARANJA, lw=1.6)
    ax.set_xlabel("tempo (s)")
    ax.set_ylabel("empuxo (N)", color=LARANJA)
    if "p" in curva:
        ax2 = ax.twinx()
        ax2.plot(curva["t"], np.asarray(curva["p"]) / 1e6, color=AZUL, lw=1.1)
        ax2.set_ylabel("pressão de câmara (MPa)", color=AZUL)
        ax2.grid(False)
    return _svg(fig)


def grafico_pouso(s: dict, voos: list[dict] | None = None, elipse: dict | None = None) -> str:
    fig = Figure(figsize=(5.2, 5.2))
    ax = fig.add_subplot()
    leste, norte = np.asarray(s["position_east_m"]), np.asarray(s["position_north_m"])
    ax.plot(leste, norte, color=AZUL, lw=1.2, label="trajetória nominal")
    ax.plot([leste[-1]], [norte[-1]], "o", color=AZUL, ms=6, label="pouso nominal")
    if voos:
        pts = [(v["pouso_leste_m"], v["pouso_norte_m"]) for v in voos if v["pouso_leste_m"] is not None]
        ax.scatter(*zip(*pts), s=8, color=LARANJA, alpha=0.6, label=f"pousos ({len(pts)} voos)")
    if elipse:
        ax.add_patch(Ellipse((elipse["centro_leste_m"], elipse["centro_norte_m"]), 2 * elipse["semieixo_maior_m"],
                             2 * elipse["semieixo_menor_m"], angle=90 - elipse["rumo_eixo_maior_graus"],
                             fill=False, color=VERMELHO, lw=1.4, label="elipse de 95%"))
    ax.plot([0], [0], "^", color="black", ms=8, label="base")
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("leste (m)")
    ax.set_ylabel("norte (m)")
    ax.legend(loc="best", frameon=False, fontsize=7)
    return _svg(fig)


# ---------------------------------------------------------------------------------------------- HTML

_CSS = """
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#111827;background:#fff;
  max-width:980px;margin:0 auto;padding:24px 16px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 10px;border-bottom:1px solid #e5e7eb;padding-bottom:4px}
.meta{color:#6b7280;font-size:13px}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.card{border:1px solid #e5e7eb;border-radius:8px;padding:10px 12px}.card .v{font-size:20px;font-weight:600}
.card .r{color:#6b7280;font-size:12px}table{border-collapse:collapse;width:100%;margin:6px 0}
td,th{border-bottom:1px solid #f0f0f0;padding:5px 8px;text-align:left;vertical-align:top}th{color:#374151;font-weight:600}
td.n{text-align:right;font-variant-numeric:tabular-nums}.alerta{border-left:4px solid #dc2626;background:#fef2f2;
  padding:6px 10px;margin:6px 0;border-radius:4px}.aviso{border-left:4px solid #d97706;background:#fffbeb;
  padding:6px 10px;margin:6px 0;border-radius:4px}.ok{border-left:4px solid #16a34a;background:#f0fdf4;padding:6px 10px;
  border-radius:4px}figure{margin:10px 0}figure svg{max-width:100%;height:auto}figcaption{color:#6b7280;font-size:12px}
footer{color:#9ca3af;font-size:12px;margin-top:32px}@media print{body{padding:0}h2{break-after:avoid}figure{break-inside:avoid}}
"""


def _e(x) -> str:
    return html.escape(str(x))


def _fmt(v, casas=2) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, float):
        return f"{v:,.{casas}f}".replace(",", " ").replace(".", ",") if abs(v) < 1e6 else f"{v:.3g}"
    return _e(v)


def _tabela(linhas: list[tuple[str, object]], casas=2) -> str:
    return "<table>" + "".join(f"<tr><th>{_e(k)}</th><td class='n'>{_fmt(v, casas)}</td></tr>" for k, v in linhas) + "</table>"


def _card(rotulo, valor, unidade="", casas=1) -> str:
    return f"<div class='card'><div class='r'>{_e(rotulo)}</div><div class='v'>{_fmt(valor, casas)} {_e(unidade)}</div></div>"


def montar_html(titulo: str, contexto: dict, voo: dict, graficos: dict[str, str], motor: dict | None = None,
                dispersao: dict | None = None) -> str:
    est, ev, seg, pouso = voo["estabilidade"], voo["estabilidade_voo"], voo["seguranca"], voo.get("pouso") or {}
    partes = [f"<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' "
              f"content='width=device-width,initial-scale=1'><title>{_e(titulo)}</title><style>{_CSS}</style></head><body>",
              f"<h1>{_e(titulo)}</h1><div class='meta'>{_e(contexto['foguete'])} · simulação “{_e(contexto['simulacao'])}” · "
              f"motor {_e(contexto['motor'])} · gerado em {datetime.now():%d/%m/%Y %H:%M}<br>arquivo: "
              f"{_e(contexto['arquivo_ork'])}</div>"]

    partes.append("<h2>Resumo</h2><div class='cards'>" + "".join([
        _card("Apogeu", voo["apogeu_m"], "m", 0), _card("Velocidade máxima", voo["velocidade_max_ms"], "m/s", 0),
        _card("Mach máximo", voo["mach_max"], "", 2), _card("Saída do trilho", voo["velocidade_saida_trilho_ms"], "m/s"),
        _card("Margem estática", est["margem_estatica_cal"], "cal", 2),
        _card("Estabilidade na saída do trilho", ev["saida_trilho_cal"], "cal", 2),
        _card("Distância de pouso", pouso.get("distancia_m"), "m", 0),
        _card("Descida final", (seg["descida"] or [{}])[-1].get("velocidade_ms"), "m/s"),
    ]) + "</div>")

    alertas = voo["alertas_projeto"]
    avisos = voo["avisos"] + voo["avisos_validacao"] + voo.get("avisos_carregamento", [])
    partes.append("<h2>Alertas</h2>")
    partes.append("".join(f"<div class='alerta'>{_e(a)}</div>" for a in alertas) or
                  "<div class='ok'>Nenhum alerta de projeto.</div>")
    partes.append("".join(f"<div class='aviso'>{_e(a)}</div>" for a in avisos))

    partes.append("<h2>Voo</h2>")
    for chave, legenda in (("trajetoria", "Altitude e velocidade"), ("subida", "Estabilidade e Mach na subida "
                           "(linha vermelha: 1 calibre)"), ("empuxo", "Empuxo e arrasto durante a queima"),
                           ("pouso", "Vista de cima: trajetória e ponto de pouso")):
        if chave in graficos:
            partes.append(f"<figure>{graficos[chave]}<figcaption>{_e(legenda)}</figcaption></figure>")

    partes.append("<h2>Estabilidade e massa</h2>" + _tabela([
        ("Margem estática na decolagem (cal)", est["margem_estatica_cal"]),
        ("Margem estática após a queima (cal)", est["margem_estatica_burnout_cal"]),
        ("Estabilidade na saída do trilho (cal)", ev["saida_trilho_cal"]),
        ("Estabilidade mínima na subida (cal)", ev["min_subida_cal"]), ("CP (mm do nariz)", est["cp_mm"]),
        ("CG na decolagem (mm)", est["cg_mm"]), ("CG após a queima (mm)", est["cg_burnout_mm"]),
        ("Massa na decolagem (kg)", est["massa_decolagem_kg"]), ("Massa após a queima (kg)", est["massa_burnout_kg"]),
    ]))
    linhas_seg = [("Pressão dinâmica máxima (kPa)", seg["q_max_kpa"]), ("Instante do q máximo (s)", seg["t_q_max_s"]),
                  ("Aceleração máxima na subida (g)", seg["aceleracao_max_subida_g"]),
                  ("Aceleração máxima na recuperação (g)", seg.get("aceleracao_max_recuperacao_g")),
                  ("Empuxo/peso médio", seg.get("empuxo_peso_medio")),
                  ("Empuxo/peso na saída do trilho", seg.get("empuxo_peso_saida_trilho"))]
    linhas_seg += [(f"Descida após “{d['apos_abertura']}” (m/s)", d["velocidade_ms"]) for d in seg["descida"]]
    linhas_seg += [("Velocidade no impacto (m/s)", seg["velocidade_impacto_ms"]), ("Energia no impacto (J)", seg["energia_impacto_j"])]
    for f in seg["flutter"] if isinstance(seg["flutter"], list) else []:
        linhas_seg.append((f"Flutter “{f['aletas']}”: margem (× velocidade de voo)", f["fator_min"]))
    partes.append("<h2>Segurança e estrutura</h2>" + _tabela(linhas_seg))
    if isinstance(seg["flutter"], dict):
        partes.append(f"<p class='meta'>{_e(seg['flutter']['nota'])}</p>")

    lanc = voo["lancamento"]
    linhas_l = [(k.replace("_", " "), v) for k, v in lanc.items() if k != "perfil_vento"]
    partes.append("<h2>Condições de lançamento</h2>" + _tabela(linhas_l))
    if "perfil_vento" in lanc:
        partes.append("<table><tr><th>altitude (m)</th><th>vento (m/s)</th><th>direção (°)</th><th>turbulência (%)</th></tr>" +
                      "".join(f"<tr><td class='n'>{_fmt(n['altitude_m'], 0)}</td><td class='n'>{_fmt(n['velocidade_ms'])}</td>"
                              f"<td class='n'>{_fmt(n['direcao_graus'], 0)}</td><td class='n'>{_fmt(n['turbulencia_pct'], 0)}</td></tr>"
                              for n in lanc["perfil_vento"]) + "</table>")
    if voo.get("fatores_modelo"):
        partes.append(f"<p class='meta'>Fatores de modelo aplicados: {_e(voo['fatores_modelo'])}</p>")

    if motor:
        r = motor["resumo"]
        partes.append(f"<h2>Motor ({_e(motor['arquivo'])})</h2>")
        if "grafico" in motor:
            partes.append(f"<figure>{motor['grafico']}<figcaption>Empuxo e pressão de câmara (openMotor)</figcaption></figure>")
        partes.append(_tabela([(k.replace("_", " "), v) for k, v in r.items() if k not in ("alertas", "sucesso")]))
        partes.append("".join(f"<div class='aviso'>{_e(a['nivel'])}: {_e(a['descricao'])}</div>" for a in r["alertas"]))

    if dispersao:
        d = dispersao["resumo"]
        partes.append(f"<h2>Dispersão ({d['n_voos']} voos, semente {d['semente']})</h2>")
        partes.append("<p class='meta'>Incertezas (1σ): " + _e(", ".join(f"{k} {v}" for k, v in d["incertezas_1sigma"].items())) + "</p>")
        if "grafico" in dispersao:
            partes.append(f"<figure>{dispersao['grafico']}<figcaption>Pontos de pouso e elipse de 95%</figcaption></figure>")
        partes.append("<table><tr><th>métrica</th><th>média</th><th>desvio</th><th>p5</th><th>p95</th><th>pior</th></tr>" + "".join(
            f"<tr><td>{_e(m.replace('_', ' '))}</td><td class='n'>{_fmt(s['media'])}</td><td class='n'>{_fmt(s['desvio'])}</td>"
            f"<td class='n'>{_fmt(s['p5'])}</td><td class='n'>{_fmt(s['p95'])}</td>"
            f"<td class='n'>{_fmt(s['min'] if 'estabilidade' in m or 'margem' in m or 'apogeu' in m else s['max'])}</td></tr>"
            for m, s in d["estatisticas"].items() if s) + "</table>")
        partes.append(_tabela([(k.replace("_", " ") + " (fração)", v) for k, v in d["probabilidades"].items()], 3))
        if d["pouso_95"]:
            e = d["pouso_95"]
            partes.append(_tabela([("Centro da elipse: leste/norte (m)", f"{e['centro_leste_m']:.0f} / {e['centro_norte_m']:.0f}"),
                                   ("Semieixos (m)", f"{e['semieixo_maior_m']:.0f} × {e['semieixo_menor_m']:.0f}"),
                                   ("Rumo do eixo maior (°)", e["rumo_eixo_maior_graus"]),
                                   ("Raio com 95% dos pousos (m)", e["raio_95_da_base_m"])]))

    partes.append("<footer>Gerado pelo rocket-mcp: openMotor (balística interna) + OpenRocket 24.12 (trajetória). "
                  "Resultados de simulação; confira com ensaios e dados de voo.</footer></body></html>")
    return "".join(partes)


def series_dict(colunas: list[str], linhas: list[list[float]]) -> dict[str, np.ndarray]:
    arr = np.array(linhas, dtype=float) if linhas else np.zeros((0, len(colunas)))
    return {c: arr[:, i] for i, c in enumerate(colunas)}
