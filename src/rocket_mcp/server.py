"""Servidor MCP unificado: openMotor (balística interna) + OpenRocket (trajetória)."""
from __future__ import annotations

import csv
import functools
import logging
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import anyio.from_thread
import jpype
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import om_bridge as om
from . import or_bridge as orb
from . import or_foguete as of

logging.basicConfig(stream=sys.stderr, level=logging.INFO)  # nunca stdout
log = logging.getLogger("rocket-mcp")

WORKSPACE = Path(os.environ.get("ROCKET_MCP_WORKSPACE", Path.cwd())).resolve()
MOTORES = WORKSPACE / "motores"

mcp = MCPServer(
    name="rocket-mcp",
    instructions=(
        "Ferramentas de projeto de foguetes. Unidades SI salvo indicação no nome do parâmetro. "
        "Caminhos são relativos ao workspace. Fluxo típico: simular_motor -> exportar_motor_eng -> "
        "simular_voo (ou projetar_e_voar para tudo de uma vez). Para projetar um motor: "
        "listar_tipos_grao -> criar_motor -> editar_motor (simular=True) até atender aos requisitos. "
        "Para o foguete: ler_foguete -> editar_foguete (grava em arquivo novo) -> simular_voo; "
        "varrer_parametro compara vários valores de um parâmetro; exportar_series_* gera CSV."
    ),
)


def _caminho(rel: str, existente: bool = True) -> Path:
    """Impede acesso fora do workspace (o LLM escolhe os caminhos).
    existente=False para destinos de escrita (salvar_como, arquivo novo)."""
    p = (WORKSPACE / rel).resolve()
    if not p.is_relative_to(WORKSPACE):
        raise ToolError(f"Caminho fora do workspace: {rel}")
    if existente and not p.exists() and p.suffix in (".ric", ".ork", ".eng"):
        raise ToolError(f"Arquivo não encontrado: {rel}")
    return p


def _destino(rel: str, extensao: str) -> Path:
    """Destino de escrita no workspace, com a extensão exigida e a pasta criada."""
    p = _caminho(rel, existente=False)
    if p.suffix.lower() != extensao:
        raise ToolError(f"O arquivo de destino precisa ter extensão {extensao}: {rel}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _rel(p: Path) -> str:
    """Caminho devolvido ao LLM: relativo ao workspace e com '/' em qualquer SO."""
    return p.relative_to(WORKSPACE).as_posix()


@mcp.tool()
async def simular_motor(arquivo_ric: str, ctx: Context) -> dict:
    """Simula a balística interna de um motor sólido do openMotor (.ric).
    Retorna designação, impulso total, empuxo médio, tempo de queima, Isp, pressão máxima,
    Kn inicial/pico, massa de propelente e alertas (erros e avisos de projeto)."""
    motor = om.carregar_ric(_caminho(arquivo_ric))

    ultimo = [0.0]

    def progresso(frac: float) -> bool:
        if frac - ultimo[0] >= 0.05:  # no máximo ~20 notificações por simulação
            ultimo[0] = frac
            anyio.from_thread.run(ctx.report_progress, frac, 1.0)
        return False  # True cancelaria

    def trabalho():
        with orb.LOCK:  # motorlib não é thread-safe; reaproveita o lock global
            return om.simular(motor, progresso)

    r = await anyio.to_thread.run_sync(trabalho)
    return om.resumo(motor, r)


@mcp.tool()
def exportar_motor_eng(arquivo_ric: str, diametro_mm: float, comprimento_mm: float,
                       massa_hardware_kg: float, designacao: str | None = None) -> dict:
    """Simula o motor .ric, exporta a curva de empuxo em formato RASP (.eng) na pasta motores/
    do workspace e registra o motor no banco do OpenRocket.
    diametro_mm/comprimento_mm: envelope externo do motor (case), não do grão.
    massa_hardware_kg: massa do motor vazio (case, tubeira, tampas)."""
    motor = om.carregar_ric(_caminho(arquivo_ric))
    with orb.LOCK:
        r = om.simular(motor)
    res = om.resumo(motor, r)
    if not res["sucesso"]:
        return res
    MOTORES.mkdir(exist_ok=True)
    nome = designacao or res["designacao"] + "-OM"
    eng = MOTORES / f"{nome}.eng"
    om.exportar_eng(r, eng, diametro_mm, comprimento_mm, massa_hardware_kg, designacao=nome)
    orb.registrar_motor(eng)
    return res | {"arquivo_eng": _rel(eng), "nome_motor": nome}


# --------------------------------------------------------------------------------------- OpenRocket

def _erros_java(f):
    """Exceção Java vira ToolError legível (sem isso o LLM só vê 'Error executing tool')."""
    @functools.wraps(f)
    def envolvida(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except jpype.JException as e:
            classe = str(e.getClass().getSimpleName())
            raise ToolError(f"Erro do OpenRocket ({classe}): {e.getMessage() or classe}") from e

    return envolvida


def _destino_novo(rel: str, extensao: str, sobrescrever: bool) -> Path:
    """Destino de escrita que não sobrescreve nada sem pedido explícito (preferência do usuário)."""
    p = _destino(rel, extensao)
    if p.exists() and not sobrescrever:
        raise ToolError(f"'{rel}' já existe. Por padrão nada é sobrescrito: use outro nome ou sobrescrever=True.")
    return p


def _carregar(arquivo_ork: str):
    p = _caminho(arquivo_ork)
    if p.suffix.lower() != ".ork":
        raise ToolError(f"Esperado um arquivo .ork: {arquivo_ork}")
    orb.garantir()
    orb.sincronizar_motores(MOTORES)  # .eng copiados para motores/ com o servidor no ar
    return orb.carregar_ork(p)


def _simulacao(doc, indice: int):
    n = doc.getSimulations().size()
    if not 0 <= indice < n:
        raise ToolError(f"indice_simulacao {indice} inválido: o arquivo tem {n} simulação(ões), índices 0 a {n - 1}")
    return doc.getSimulations().get(indice)


def _opcoes_voo(opcoes: dict | None) -> dict:
    """opcoes_voo de varrer_parametro/exportar_series_voo: mesmos nomes dos argumentos de simular_voo."""
    opcoes = {"semente": 0} | (opcoes or {})
    desconhecidas = sorted(set(opcoes) - set(orb.OPCOES_VOO))
    if desconhecidas:
        raise ToolError(f"opcoes_voo: chave(s) desconhecida(s) {desconhecidas}. Aceitas: {list(orb.OPCOES_VOO)}")
    return opcoes


def _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, opcoes: dict, avisos_carga: list) -> dict:
    """Troca o motor (opcional), aplica as opções, simula e junta estabilidade e alertas de projeto."""
    avisos_val = []
    if motor_eng:
        eng = _caminho(motor_eng)
        if eng.suffix.lower() != ".eng":
            raise ToolError(f"motor_eng precisa ser um .eng: {motor_eng}")
        motor = orb.registrar_motor(eng)
        avisos_val = orb.trocar_motor(doc, sim, motor, atraso_ejecao_s or 0.0, montagem)
    notas = orb.configurar(sim, **opcoes)
    with orb.LOCK:
        estabilidade = of.estabilidade_estatica(doc, sim)  # depois da troca: o motor entra no CG
        lancamento = orb.condicoes_lancamento(sim)
    res = orb.simular(sim) | {"estabilidade": estabilidade, "lancamento": lancamento}
    res |= {"avisos_validacao": avisos_val, "avisos_carregamento": avisos_carga}
    res["alertas_projeto"] = orb.alertas_projeto(res, atraso_ejecao_s)
    if notas:
        res["notas"] = notas
    return res


def _sims(doc) -> list[dict]:
    return [{"indice": i, "nome": str(s.getName())} for i, s in enumerate(doc.getSimulations())]


@mcp.tool()
@_erros_java
def abrir_foguete(arquivo_ork: str) -> dict:
    """Lê um projeto do OpenRocket (.ork): nome, simulações, montagens de motor (diâmetro interno,
    comprimento e motor atual) e estabilidade estática na configuração da simulação 0 (margem em
    calibres, CG, CP, massas). Para a árvore completa e os parâmetros editáveis: ler_foguete."""
    doc, avisos = _carregar(arquivo_ork)
    sims = list(doc.getSimulations())
    out = {
        "foguete": str(doc.getRocket().getName()),
        "avisos_carregamento": avisos,
        "simulacoes": _sims(doc),
        "montagens": [{k: v for k, v in m.items() if k != "componente"}
                      for m in (orb.montagens(doc, sims[0]) if sims else [])],
    }
    if sims:
        with orb.LOCK:
            out["estabilidade"] = of.estabilidade_estatica(doc, sims[0])
    return out


@mcp.tool()
@_erros_java
def simular_voo(arquivo_ork: str, indice_simulacao: int = 0, motor_eng: str | None = None,
                atraso_ejecao_s: float | None = None, montagem: str | None = None,
                trilho_m: float | None = None, angulo_trilho_graus: float | None = None,
                direcao_trilho_graus: float | None = None, lancar_contra_vento: bool | None = None,
                vento_medio_ms: float | None = None,
                vento_direcao_graus: float | None = None, turbulencia_pct: float | None = None,
                altitude_lancamento_m: float | None = None, latitude_graus: float | None = None,
                longitude_graus: float | None = None, temperatura_c: float | None = None,
                pressao_hpa: float | None = None, semente: int | None = 0,
                salvar_como: str | None = None, sobrescrever: bool = False) -> dict:
    """Simula o voo de uma simulação do .ork. Opcionalmente troca o motor por um .eng
    (caminho no workspace, ex.: 'motores/F46-OM.eng') e ajusta o lançamento: trilho (comprimento, ângulo
    com a vertical, direção para onde APONTA), vento (médio, turbulência e direção de onde VEM: 90 = vento
    de leste), local (altitude, latitude, longitude) e atmosfera (temperatura e pressão; sem elas usa a ISA).
    lancar_contra_vento: com True o OpenRocket aponta o trilho contra o vento e ignora a direção do trilho
    (vem ligado em muitos .ork); informar direcao_trilho_graus desliga isso. 'lancamento' na resposta
    mostra as condições efetivas usadas.
    semente fixa o ruído do vento para resultados repetíveis (None = aleatório, como na interface).
    Para comparar vários ventos ou ângulos de trilho numa chamada: varrer_parametro.
    Retorna apogeu, velocidades, Mach, atraso ótimo, estabilidade (margem estática e estabilidade em voo:
    saída do trilho e mínima na subida), ponto de pouso (distância e rumo a partir da base; 0 = norte),
    massas, alertas de projeto e avisos do OpenRocket.
    O .ork original não é alterado; salvar_como grava o resultado em arquivo novo."""
    destino = _destino_novo(salvar_como, ".ork", sobrescrever) if salvar_como else None  # valida antes
    doc, avisos = _carregar(arquivo_ork)
    sim = _simulacao(doc, indice_simulacao)
    opcoes = dict(trilho_m=trilho_m, angulo_trilho_graus=angulo_trilho_graus,
                  direcao_trilho_graus=direcao_trilho_graus, lancar_contra_vento=lancar_contra_vento,
                  vento_medio_ms=vento_medio_ms,
                  vento_direcao_graus=vento_direcao_graus, turbulencia_pct=turbulencia_pct,
                  altitude_lancamento_m=altitude_lancamento_m, latitude_graus=latitude_graus,
                  longitude_graus=longitude_graus, temperatura_c=temperatura_c, pressao_hpa=pressao_hpa,
                  semente=semente)
    res = _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, opcoes, avisos)
    if destino:
        orb.salvar_ork(doc, destino)
        res["arquivo_salvo"] = _rel(destino)
    return res


@mcp.tool()
def projetar_e_voar(arquivo_ric: str, arquivo_ork: str, diametro_mm: float, comprimento_mm: float,
                    massa_hardware_kg: float, atraso_ejecao_s: float, indice_simulacao: int = 0,
                    montagem: str | None = None) -> dict:
    """Pipeline completo: simula o motor (.ric) -> exporta .eng -> coloca no foguete (.ork) ->
    simula o voo. Retorna o resumo do motor e do voo juntos."""
    m = exportar_motor_eng(arquivo_ric, diametro_mm, comprimento_mm, massa_hardware_kg)
    if not m.get("sucesso"):
        return {"motor": m, "voo": None}
    voo = simular_voo(arquivo_ork, indice_simulacao, m["arquivo_eng"], atraso_ejecao_s, montagem)
    return {"motor": m, "voo": voo}


@mcp.tool()
@_erros_java
def ler_foguete(arquivo_ork: str, indice_simulacao: int = 0) -> dict:
    """Árvore completa do foguete: cada componente com o caminho (use-o em editar_foguete), o tipo e os
    parâmetros editáveis com valor atual (nomes em inglês com unidade: length_mm, span_mm, mass_kg,
    overrideMass_kg, deployAltitude_m...). overrideMass_kg/overrideCG_mm = null quando não sobrescritos.
    Os parâmetros de abertura (deploy*) e a estabilidade se referem à simulação indice_simulacao."""
    doc, avisos = _carregar(arquivo_ork)
    sim = _simulacao(doc, indice_simulacao)
    with orb.LOCK:
        return {
            "foguete": str(doc.getRocket().getName()),
            "avisos_carregamento": avisos,
            "simulacoes": _sims(doc),
            "simulacao_usada": {"indice": indice_simulacao, "nome": str(sim.getName())},
            "estabilidade": of.estabilidade_estatica(doc, sim),
            "montagens": [{k: v for k, v in m.items() if k != "componente"} for m in orb.montagens(doc, sim)],
            "componentes": [of.descrever_componente(p, c, sim) for p, c in of.componentes(doc)],
            "opcoes": of.opcoes_enum(),
        }


@mcp.tool()
@_erros_java
def editar_foguete(arquivo_ork: str, alteracoes: dict[str, Any], salvar_como: str | None = None,
                   sobrescrever: bool = False, indice_simulacao: int = 0, simular: bool = False) -> dict:
    """Edita o foguete e grava em ARQUIVO NOVO (salvar_como); sobrescrever=True só se o usuário pedir.
    alteracoes: {"Componente.param": valor}, com os caminhos e parâmetros de ler_foguete. Basta o final
    do caminho, se for único. Exemplos:
      "Fins.span_mm": 200 | "Fins.count": 3 | "Nosecone.shape": "OGIVE" | "Nosecone.length_mm": 550
      "Payload 2U.mass_kg": 3.0 | "Recovery Module.overrideMass_kg": 5.5 (null remove o override)
      "Unreefed Parachute.deployAltitude_m": 400 | "Unreefed Parachute.deployEvent": "ALTITUDE"
      "Payload Module/Lastro": {"mass_kg": 0.5, "position_mm": 50}  cria uma massa (lastro) no pai
      "Payload Module/Lastro": null                                   remove uma massa
    Atenção: com a massa sobrescrita, mudar dimensões não muda a massa (a resposta avisa em 'efeitos').
    deploy* alteram a configuração de abertura da simulação indice_simulacao.
    Retorna o que mudou, efeitos, estabilidade antes/depois (margem, CG, CP, massas) e, com simular=True,
    o voo dessa simulação."""
    origem = _caminho(arquivo_ork)
    if salvar_como is None:
        if not sobrescrever:
            raise ToolError("Por padrão o original não é alterado: informe salvar_como (ex.: 'hemera_v2.ork'), "
                            "ou sobrescrever=True se o usuário pediu para gravar por cima.")
        destino = origem
    else:
        destino = _destino_novo(salvar_como, ".ork", sobrescrever)
    doc, avisos = _carregar(arquivo_ork)
    sim = _simulacao(doc, indice_simulacao)
    with orb.LOCK:
        antes = of.estabilidade_estatica(doc, sim)
        r = of.editar(doc, sim, alteracoes)
        depois = of.estabilidade_estatica(doc, sim)
    orb.salvar_ork(doc, destino)
    out = {"arquivo_ork": _rel(destino), "alteracoes": r["aplicadas"], "efeitos": r["efeitos"],
           "estabilidade": {"antes": antes, "depois": depois}, "avisos_carregamento": avisos}
    if simular:
        out["voo"] = _voar(doc, sim, None, None, None, {"semente": 0}, [])
    return out


@mcp.tool()
@_erros_java
def varrer_parametro(arquivo_ork: str, parametro: str, valores: list[Any], indice_simulacao: int = 0,
                     motor_eng: str | None = None, atraso_ejecao_s: float | None = None,
                     montagem: str | None = None, opcoes_voo: dict[str, Any] | None = None) -> dict:
    """Simula o voo para cada valor de UM parâmetro (até 25 valores) e devolve uma tabela: apogeu,
    velocidade, Mach, saída do trilho, margem estática, estabilidade em voo, ponto de pouso, massa e alertas.
    parametro pode ser:
      - uma condição de lançamento, com o nome do argumento de simular_voo: 'vento_medio_ms',
        'vento_direcao_graus', 'turbulencia_pct', 'angulo_trilho_graus', 'direcao_trilho_graus',
        'trilho_m', 'altitude_lancamento_m', 'temperatura_c', 'pressao_hpa'... Com 'semente' e
        turbulência em opcoes_voo, vira uma estimativa de dispersão por rajadas;
      - um parâmetro do foguete, no formato de editar_foguete ('Fins.span_mm', 'Payload 2U.mass_kg').
    Convenções (verificadas): vento_direcao_graus é de onde o vento VEM (90 = vento de leste, o foguete
    deriva para oeste); direcao_trilho_graus é para onde o trilho APONTA; ângulo do trilho medido da vertical.
    Com lancar_contra_vento ligado no .ork, o trilho aponta contra o vento: para varrer a direção do trilho,
    ela é desligada automaticamente (ver 'efeitos').
    opcoes_voo: demais condições fixas (mesmos nomes). A semente é 0 por padrão, para a diferença entre as
    linhas vir só do parâmetro varrido. Nenhum arquivo é gravado."""
    if not isinstance(valores, list) or not 1 <= len(valores) <= 25:
        raise ToolError("valores: informe de 1 a 25 valores")
    opcoes = _opcoes_voo(opcoes_voo)
    condicao = parametro.strip() in orb.OPCOES_VOO
    if condicao:
        for v in valores:  # valida antes de simular qualquer linha
            if parametro.strip() == "lancar_contra_vento":
                if not isinstance(v, bool):
                    raise ToolError(f"{parametro}: esperado true/false, recebido {v!r}")
            elif isinstance(v, bool) or not isinstance(v, (int, float)) and not (v is None and parametro == "semente"):
                raise ToolError(f"{parametro}: esperado um número, recebido {v!r}")
    linhas, efeitos, nome_sim = [], [], None
    for v in valores:
        doc, _ = _carregar(arquivo_ork)
        sim = _simulacao(doc, indice_simulacao)
        nome_sim = str(sim.getName())
        if condicao:
            opcoes_linha = opcoes | {parametro.strip(): v}
        else:
            opcoes_linha = opcoes
            with orb.LOCK:
                efeitos += of.editar(doc, sim, {parametro: v})["efeitos"]
        res = _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, opcoes_linha, [])
        est, ev = res["estabilidade"], res["estabilidade_voo"]
        linhas.append({
            "valor": v, "apogeu_m": res["apogeu_m"], "velocidade_max_ms": res["velocidade_max_ms"],
            "mach_max": res["mach_max"], "velocidade_saida_trilho_ms": res["velocidade_saida_trilho_ms"],
            "margem_estatica_cal": est["margem_estatica_cal"], "estabilidade_saida_trilho_cal": ev["saida_trilho_cal"],
            "estabilidade_min_subida_cal": ev["min_subida_cal"], "pouso": res["pouso"],
            "velocidade_abertura_recuperacao_ms": res["velocidade_abertura_recuperacao_ms"],
            "massa_decolagem_kg": est["massa_decolagem_kg"], "alertas_projeto": res["alertas_projeto"],
        })
        efeitos += res.get("notas", [])
    return {"parametro": parametro, "tipo": "condicao_lancamento" if condicao else "foguete",
            "simulacao": nome_sim, "linhas": linhas,
            "efeitos": list(dict.fromkeys(e for e in efeitos if not e.startswith("O OpenRocket ajustou")))}


def _gravar_csv(destino: Path, colunas: list[str], linhas) -> None:
    with destino.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(colunas)
        for linha in linhas:
            w.writerow(["" if x != x else f"{x:.6g}" for x in linha])  # NaN -> vazio


@mcp.tool()
@_erros_java
def exportar_series_voo(arquivo_ork: str, arquivo_csv: str, indice_simulacao: int = 0,
                        motor_eng: str | None = None, atraso_ejecao_s: float | None = None,
                        montagem: str | None = None, opcoes_voo: dict[str, Any] | None = None,
                        sobrescrever: bool = False) -> dict:
    """Simula o voo e grava as séries temporais num CSV do workspace (altitude, velocidades, aceleração,
    Mach, empuxo, arrasto, Cd, massa, estabilidade, CG, CP, ângulo de ataque, posição, vento). Devolve o
    caminho, as colunas, os eventos do voo e o resumo, não os dados: abra o CSV para analisar ou plotar.
    opcoes_voo: mesmos nomes dos argumentos de simular_voo."""
    destino = _destino_novo(arquivo_csv, ".csv", sobrescrever)
    doc, avisos = _carregar(arquivo_ork)
    sim = _simulacao(doc, indice_simulacao)
    res = _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, _opcoes_voo(opcoes_voo), avisos)
    colunas, linhas, eventos = orb.series_voo(sim)
    _gravar_csv(destino, colunas, linhas)
    resumo = {k: res[k] for k in ("apogeu_m", "velocidade_max_ms", "mach_max", "tempo_ate_apogeu_s", "tempo_voo_s")}
    return {"arquivo_csv": _rel(destino), "linhas": len(linhas), "colunas": colunas, "eventos": eventos,
            "resumo": resumo, "alertas_projeto": res["alertas_projeto"]}


@mcp.tool()
def exportar_series_motor(arquivo_ric: str, arquivo_csv: str, sobrescrever: bool = False) -> dict:
    """Simula o motor (.ric) e grava as séries temporais num CSV do workspace: empuxo, pressão de câmara,
    Kn, massa de propelente, vazão, fluxo de massa máximo, Mach máximo no núcleo, pressão de saída,
    variação da garganta. Devolve o caminho e as colunas, não os dados."""
    destino = _destino_novo(arquivo_csv, ".csv", sobrescrever)
    with orb.LOCK:
        motor = om.carregar_ric(_caminho(arquivo_ric))
        r = om.simular(motor)
    res = om.resumo(motor, r)
    if not res["sucesso"]:
        return res
    colunas, linhas = om.series_motor(r)
    _gravar_csv(destino, colunas, linhas)
    return {"arquivo_csv": _rel(destino), "linhas": len(linhas), "colunas": colunas,
            "resumo": {k: res[k] for k in ("designacao", "impulso_total_Ns", "tempo_queima_s", "pressao_max_MPa")}}


@mcp.tool()
def listar_tipos_grao() -> dict:
    """Catálogo para criar_motor e editar_motor: tipos de grão (grains) com seus parâmetros, tubeira
    (nozzle), config da simulação e propelentes da biblioteca. Para cada parâmetro: unidade (é o sufixo
    do nome: _mm, _deg, _MPa...), faixa válida, se é obrigatório e o valor padrão."""
    return om.esquema()


@mcp.tool()
def criar_motor(arquivo_ric: str, propellant: str | dict[str, Any], grains: list[dict[str, Any]],
                nozzle: dict[str, Any], config: dict[str, Any] | None = None,
                sobrescrever: bool = False, simular: bool = False) -> dict:
    """Cria um motor sólido do openMotor e grava o .ric no workspace (ex.: 'motores/j510.ric').
    propellant: nome da biblioteca ('KNSB', 'Nakka - KNDX', 'White Lightning'...) ou um propelente próprio
      {"name", "density_kg_m3", "tabs": [{"minPressure_MPa", "maxPressure_MPa", "a_SI", "n", "k", "t_K", "m_g_mol"}]}.
    grains: do grão dianteiro (0) ao traseiro, junto da tubeira. Cada um tem "type" e os parâmetros do tipo,
      ex.: {"type": "BATES", "diameter_mm": 54, "length_mm": 100, "coreDiameter_mm": 20, "inhibitedEnds": "Neither"}.
    nozzle: ex.: {"throat_mm": 12, "exit_mm": 30, "efficiency": 0.85, "divAngle_deg": 15, "convAngle_deg": 45}.
    config: opcional (limites e passo da simulação); o que faltar usa o padrão do openMotor.
    Parâmetros, unidades e faixas de cada tipo: listar_tipos_grao.
    A geometria é validada antes de gravar; se houver erro, nada é gravado. Retorna o motor gravado,
    a prévia (Kn inicial, razão porta/garganta, carregamento, massa) e, com simular=True, a simulação."""
    destino = _destino(arquivo_ric, ".ric")
    if destino.exists() and not sobrescrever:
        raise ToolError(f"{arquivo_ric} já existe. Use sobrescrever=True ou editar_motor.")
    with orb.LOCK:
        motor = om.montar_motor(propellant, grains, nozzle, config)
        return _gravar_motor(motor, destino, simular)


@mcp.tool()
def ler_motor(arquivo_ric: str) -> dict:
    """Lê um .ric e devolve os parâmetros no formato de criar_motor (os nomes servem de caminho em
    editar_motor), a prévia sem simular e os alertas de geometria."""
    with orb.LOCK:
        origem = _ric_existente(arquivo_ric)
        motor = om.carregar_ric(origem)
        alertas = om.alertas_geometria(motor)
        out = {"arquivo_ric": _rel(origem), "motor": om.descrever(motor), "alertas": alertas}
        if not any(a["nivel"] == "ERROR" for a in alertas):
            out["previa"] = om.previa(motor)
        return out


@mcp.tool()
def editar_motor(arquivo_ric: str, alteracoes: dict[str, Any], salvar_como: str | None = None,
                 simular: bool = False) -> dict:
    """Altera parâmetros de um .ric e grava no próprio arquivo (ou em salvar_como, mantendo o original).
    alteracoes: {caminho: valor}, aplicadas em ordem. Os nomes são os de ler_motor:
      "grains[0].coreDiameter_mm": 12      altera um parâmetro do grão 0 (o dianteiro)
      "nozzle.throat_mm": 9.5              altera a tubeira; "config.maxPressure_MPa": 8 altera a config
      "propellant": "KNDX"                 troca o propelente (nome da biblioteca ou objeto próprio)
      "grains[3]": {"type": "BATES", ...}  substitui o grão 3, ou acrescenta se 3 == número de grãos
      "grains[1]": null                    remove o grão 1
    Valida antes de gravar; se houver erro, nada é gravado. Retorna o mesmo que criar_motor. Para iterar
    um projeto (ex.: abrir o núcleo até a pressão máxima ficar abaixo de um limite), use simular=True."""
    origem = _ric_existente(arquivo_ric)
    destino = _destino(salvar_como, ".ric") if salvar_como else origem
    with orb.LOCK:
        motor = om.carregar_ric(origem)
        om.editar(motor, alteracoes)
        return _gravar_motor(motor, destino, simular)


def _ric_existente(rel: str) -> Path:
    p = _caminho(rel)
    if p.suffix.lower() != ".ric":
        raise ToolError(f"Esperado um arquivo .ric: {rel}")
    return p


def _gravar_motor(motor, destino: Path, simular: bool) -> dict:
    """Valida, grava o .ric e monta o retorno de criar_motor/editar_motor (chamar com orb.LOCK)."""
    alertas = om.validar(motor)
    om.salvar_ric(motor, destino)
    out = {"arquivo_ric": _rel(destino), "motor": om.descrever(motor), "previa": om.previa(motor),
           "alertas": alertas}
    if simular:
        out["simulacao"] = om.resumo(motor, om.simular(motor))
    return out


@mcp.resource("openmotor://propelentes", mime_type="application/json")
def propelentes() -> list[dict]:
    """Biblioteca padrão de propelentes do openMotor (KNSB, KNDX, KNSU, RCS...): densidade e,
    por faixa de pressão, a, n (lei de Saint-Robert r = a·Pⁿ, SI), k, T de combustão e massa molar."""
    return om.PROPELENTES_PADRAO


def main() -> None:
    log.info("workspace=%s | perímetro openMotor via %s", WORKSPACE, om.PERIMETRO)
    orb.iniciar_em_segundo_plano(MOTORES)  # JVM sobe em paralelo ao handshake MCP
    mcp.run("stdio")


if __name__ == "__main__":
    main()
