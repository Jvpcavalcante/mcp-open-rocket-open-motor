"""Servidor MCP unificado: openMotor (balística interna) + OpenRocket (trajetória)."""
from __future__ import annotations

import csv
import functools
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import anyio.from_thread
import jpype
import numpy as np
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import analise as an
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
        "varrer_parametro compara vários valores de um parâmetro; otimizar_parametro busca o valor que atinge "
        "uma meta; analisar_dispersao estima o risco (Monte Carlo); exportar_series_* gera CSV. "
        "Com dados reais: comparar_curvas_empuxo, calibrar_motor (ensaio estático) e calibrar_voo (log do "
        "altímetro; os fatores achados entram em simular_voo/opcoes_voo). gerar_relatorio produz um HTML."
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


# Fatores de modelo (calibração/dispersão): não são opções do OpenRocket, o _voar os aplica por fora
FATORES_VOO = ("fator_arrasto", "fator_impulso", "fator_massa")
OPCOES_TODAS = orb.OPCOES_VOO + FATORES_VOO


def _opcoes_voo(opcoes: dict | None) -> dict:
    """opcoes_voo de varrer_parametro/exportar_series_voo: mesmos nomes dos argumentos de simular_voo."""
    opcoes = {"semente": 0} | (opcoes or {})
    desconhecidas = sorted(set(opcoes) - set(OPCOES_TODAS))
    if desconhecidas:
        raise ToolError(f"opcoes_voo: chave(s) desconhecida(s) {desconhecidas}. Aceitas: {list(OPCOES_TODAS)}")
    for k in FATORES_VOO:
        v = opcoes.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.1 <= v <= 10):
            raise ToolError(f"{k}: esperado um número entre 0,1 e 10, recebido {v!r}")
    return opcoes


def _g_aletas(material: str | None, gpa: float | None) -> float | None:
    """Módulo de cisalhamento das aletas em Pa, de um material da tabela ou do valor informado."""
    if material is not None and gpa is not None:
        raise ToolError("Informe material_aletas OU modulo_cisalhamento_aletas_gpa")
    if material is not None:
        chave = material.strip().lower().replace("í", "i").replace("ç", "c")
        if chave not in orb.MATERIAIS_ALETA_GPA:
            raise ToolError(f"material_aletas '{material}' fora da tabela {list(orb.MATERIAIS_ALETA_GPA)}. "
                            "Para compósitos e madeira, informe modulo_cisalhamento_aletas_gpa (medido ou do "
                            "fornecedor): o valor varia muito com a laminação.")
        return orb.MATERIAIS_ALETA_GPA[chave] * 1e9
    if gpa is not None:
        if isinstance(gpa, bool) or not isinstance(gpa, (int, float)) or gpa <= 0:
            raise ToolError("modulo_cisalhamento_aletas_gpa: esperado um número > 0")
        return float(gpa) * 1e9
    return None


def _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, opcoes: dict, avisos_carga: list,
          g_aletas_pa: float | None = None, fatores: dict | None = None) -> dict:
    """Troca o motor (opcional), aplica as opções, simula e junta estabilidade, segurança e alertas.
    fatores: {'massa': f, 'impulso': f, 'arrasto': f} multiplicativos (análise de dispersão); são
    combinados com fator_* das opções (valores calibrados)."""
    opcoes = dict(opcoes)
    fatores = {k: (fatores or {}).get(k, 1.0) * (opcoes.pop(f"fator_{k}", None) or 1.0)
               for k in ("massa", "impulso", "arrasto")}
    avisos_val = []
    if motor_eng:
        eng = _caminho(motor_eng)
        if eng.suffix.lower() != ".eng":
            raise ToolError(f"motor_eng precisa ser um .eng: {motor_eng}")
        motor = orb.registrar_motor(eng)
        avisos_val = orb.trocar_motor(doc, sim, motor, atraso_ejecao_s or 0.0, montagem)
    notas = orb.configurar(sim, **opcoes)
    with orb.LOCK:
        if fatores.get("impulso", 1.0) != 1.0:
            orb.escalar_impulso(doc, sim, fatores["impulso"])
        if fatores.get("massa", 1.0) != 1.0:
            orb.escalar_massa(doc, sim, fatores["massa"])
        estabilidade = of.estabilidade_estatica(doc, sim)  # depois da troca: o motor entra no CG
        lancamento = orb.condicoes_lancamento(sim)
    listeners = (orb.listener_arrasto(fatores["arrasto"]),) if fatores.get("arrasto", 1.0) != 1.0 else ()
    res = orb.simular(sim, listeners) | {"estabilidade": estabilidade, "lancamento": lancamento}
    res["seguranca"] = orb.seguranca(doc, sim, g_aletas_pa)
    res |= {"avisos_validacao": avisos_val, "avisos_carregamento": avisos_carga}
    res["alertas_projeto"] = orb.alertas_projeto(res, atraso_ejecao_s)
    if any(f != 1.0 for f in fatores.values()):
        res["fatores_modelo"] = {k: round(v, 4) for k, v in fatores.items()}
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
                perfil_vento: list[Any] | None = None, perfil_vento_referencia: str | None = None,
                altitude_lancamento_m: float | None = None, latitude_graus: float | None = None,
                longitude_graus: float | None = None, temperatura_c: float | None = None,
                pressao_hpa: float | None = None, semente: int | None = 0,
                material_aletas: str | None = None, modulo_cisalhamento_aletas_gpa: float | None = None,
                fator_arrasto: float | None = None, fator_impulso: float | None = None,
                fator_massa: float | None = None,
                salvar_como: str | None = None, sobrescrever: bool = False) -> dict:
    """Simula o voo de uma simulação do .ork. Opcionalmente troca o motor por um .eng
    (caminho no workspace, ex.: 'motores/F46-OM.eng') e ajusta o lançamento: trilho (comprimento, ângulo
    com a vertical, direção para onde APONTA), vento (médio, turbulência e direção de onde VEM: 90 = vento
    de leste), local (altitude, latitude, longitude) e atmosfera (temperatura e pressão; sem elas usa a ISA).
    perfil_vento: vento por altitude (o OpenRocket interpola entre os níveis), no lugar de vento_medio_ms:
      [[altitude_m, velocidade_ms, direcao_graus], ...] ou com turbulencia_pct como 4º valor; também aceita
      [{"altitude_m":..., "velocidade_ms":..., "direcao_graus":..., "turbulencia_pct":...}].
      perfil_vento_referencia: 'AGL' (acima do solo, padrão) ou 'MSL'.
    lancar_contra_vento: com True o OpenRocket aponta o trilho contra o vento e ignora a direção do trilho
    (vem ligado em muitos .ork); informar direcao_trilho_graus desliga isso. 'lancamento' na resposta
    mostra as condições efetivas usadas.
    semente fixa o ruído do vento para resultados repetíveis (None = aleatório, como na interface).
    Para comparar vários ventos ou ângulos de trilho numa chamada: varrer_parametro.
    Retorna apogeu, velocidades, Mach, atraso ótimo, estabilidade (margem estática e estabilidade em voo:
    saída do trilho e mínima na subida), ponto de pouso (distância e rumo a partir da base; 0 = norte),
    massas, alertas de projeto e avisos do OpenRocket, e 'seguranca': pressão dinâmica máxima, aceleração
    máxima na subida e na recuperação (choque de abertura), empuxo/peso, velocidade de descida por fase,
    energia no impacto e, com material_aletas ('aluminio', 'aco', 'titanio') ou
    modulo_cisalhamento_aletas_gpa (compósitos/madeira: valor medido), a margem de flutter das aletas
    trapezoidais (NACA TN-4197; recomendado ≥ 1,5×).
    fator_arrasto/fator_impulso/fator_massa: multiplicadores de modelo (ex.: o fator de arrasto achado por
    calibrar_voo); o arrasto é escalado preservando a variação com o Mach.
    O .ork original não é alterado; salvar_como grava o resultado em arquivo novo."""
    if salvar_como and any(f not in (None, 1, 1.0) for f in (fator_arrasto, fator_impulso, fator_massa)):
        raise ToolError("salvar_como não combina com fator_*: os fatores são do modelo do MCP e alterariam o "
                        "foguete gravado (massa sobrescrita, motor escalado)")
    destino = _destino_novo(salvar_como, ".ork", sobrescrever) if salvar_como else None  # valida antes
    g_aletas = _g_aletas(material_aletas, modulo_cisalhamento_aletas_gpa)
    opcoes = _opcoes_voo(dict(
                  fator_arrasto=fator_arrasto, fator_impulso=fator_impulso, fator_massa=fator_massa,
                  trilho_m=trilho_m, angulo_trilho_graus=angulo_trilho_graus,
                  direcao_trilho_graus=direcao_trilho_graus, lancar_contra_vento=lancar_contra_vento,
                  vento_medio_ms=vento_medio_ms,
                  vento_direcao_graus=vento_direcao_graus, turbulencia_pct=turbulencia_pct,
                  perfil_vento=perfil_vento, perfil_vento_referencia=perfil_vento_referencia,
                  altitude_lancamento_m=altitude_lancamento_m, latitude_graus=latitude_graus,
                  longitude_graus=longitude_graus, temperatura_c=temperatura_c, pressao_hpa=pressao_hpa,
                  semente=semente))
    doc, avisos = _carregar(arquivo_ork)
    sim = _simulacao(doc, indice_simulacao)
    res = _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, opcoes, avisos, g_aletas)
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
        turbulência em opcoes_voo, vira uma estimativa de dispersão por rajadas; com 'perfil_vento', cada
        valor é um perfil inteiro (comparar sondagens);
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
    condicao = parametro.strip() in OPCOES_TODAS
    if condicao:
        for v in valores:  # valida antes de simular qualquer linha
            if parametro.strip() == "lancar_contra_vento":
                if not isinstance(v, bool):
                    raise ToolError(f"{parametro}: esperado true/false, recebido {v!r}")
            elif parametro.strip() == "perfil_vento":  # comparar sondagens/perfis
                if not isinstance(v, list):
                    raise ToolError(f"{parametro}: cada valor deve ser um perfil (lista de níveis)")
            elif parametro.strip() == "perfil_vento_referencia":
                if v not in ("AGL", "MSL"):
                    raise ToolError(f"{parametro}: use 'AGL' ou 'MSL'")
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


def _avaliador_voo(arquivo_ork, parametro, indice_simulacao, motor_eng, atraso_ejecao_s, montagem, opcoes,
                   metrica):
    """f(valor) -> métrica, recarregando o .ork a cada avaliação (o documento é mutável)."""
    condicao = parametro.strip() in OPCOES_TODAS
    estatica = metrica in an.METRICAS_ESTATICAS and not condicao

    def f(v):
        doc, _ = _carregar(arquivo_ork)
        sim = _simulacao(doc, indice_simulacao)
        if condicao:
            return an.METRICAS_VOO[metrica](
                _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, opcoes | {parametro.strip(): v}, []))
        with orb.LOCK:
            of.editar(doc, sim, {parametro: v})
        if not estatica:
            return an.METRICAS_VOO[metrica](_voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, opcoes, []))
        if motor_eng:  # o motor entra no CG
            orb.trocar_motor(doc, sim, orb.registrar_motor(_caminho(motor_eng)), atraso_ejecao_s or 0.0, montagem)
        with orb.LOCK:
            return an.METRICAS_VOO[metrica]({"estabilidade": of.estabilidade_estatica(doc, sim)})

    return f


def _avaliador_motor(arquivo_ric, parametro, metrica):
    origem = _ric_existente(arquivo_ric)

    def f(v):
        with orb.LOCK:
            motor = om.carregar_ric(origem)
            om.editar(motor, {parametro: v})
            om.validar(motor)
            res = om.resumo(motor, om.simular(motor))
        if not res["sucesso"]:
            raise ToolError(f"Simulação do motor falhou com {parametro}={v}: {res['alertas']}")
        return res[metrica]

    return f


@mcp.tool()
@_erros_java
def otimizar_parametro(parametro: str, minimo: float, maximo: float, metrica: str, meta: float | str,
                       arquivo_ork: str | None = None, arquivo_ric: str | None = None, indice_simulacao: int = 0,
                       motor_eng: str | None = None, atraso_ejecao_s: float | None = None,
                       montagem: str | None = None, opcoes_voo: dict[str, Any] | None = None,
                       tolerancia: float | None = None, max_avaliacoes: int = 20,
                       salvar_como: str | None = None, sobrescrever: bool = False) -> dict:
    """Procura o valor de UM parâmetro, entre minimo e maximo, que leva uma métrica a uma meta.
    meta: um número (ex.: 1.5 para margem de 1,5 cal) ou "min"/"max" (ex.: deriva mínima).
    Foguete (arquivo_ork): parametro como em editar_foguete ('Payload Module/Lastro.mass_kg',
      'Fins.span_mm') ou uma condição de lançamento ('angulo_trilho_graus'); métricas: apogeu_m,
      velocidade_max_ms, mach_max, velocidade_saida_trilho_ms, margem_estatica_cal (rápida, sem simular),
      estabilidade_saida_trilho_cal, estabilidade_min_subida_cal, distancia_pouso_m, massa_decolagem_kg,
      velocidade_abertura_recuperacao_ms, velocidade_impacto_ms, tempo_voo_s...
    Motor (arquivo_ric): parametro como em editar_motor ('nozzle.throat_mm', 'grains[0].coreDiameter_mm');
      métricas: pressao_max_MPa, impulso_total_Ns, empuxo_medio_N, tempo_queima_s, isp_s, kn_pico...
    Meta numérica usa bisseção: a métrica precisa cruzar a meta no intervalo (senão a resposta mostra o
    alcance). "min"/"max" usa malha + seção áurea. tolerancia: na unidade da métrica (padrão 0,1% da meta).
    salvar_como grava o resultado em arquivo NOVO (.ork ou .ric). Devolve o valor, a métrica e o histórico."""
    if (arquivo_ork is None) == (arquivo_ric is None):
        raise ToolError("Informe arquivo_ork (foguete/voo) OU arquivo_ric (motor)")
    if not minimo < maximo:
        raise ToolError("minimo precisa ser menor que maximo")
    if not 3 <= max_avaliacoes <= 40:
        raise ToolError("max_avaliacoes: de 3 a 40")
    metricas = an.METRICAS_MOTOR if arquivo_ric else tuple(an.METRICAS_VOO)
    if metrica not in metricas:
        raise ToolError(f"metrica '{metrica}' inválida. Opções: {list(metricas)}")
    if isinstance(meta, str) and meta not in ("min", "max"):
        raise ToolError("meta: um número, 'min' ou 'max'")
    ext = ".ric" if arquivo_ric else ".ork"
    if salvar_como and parametro.strip() in FATORES_VOO:
        raise ToolError(f"{parametro} é um fator de modelo do MCP e não cabe no .ork: use o valor encontrado "
                        "em opcoes_voo/simular_voo, sem salvar_como")
    destino = _destino_novo(salvar_como, ext, sobrescrever) if salvar_como else None
    if arquivo_ric:
        f = _avaliador_motor(arquivo_ric, parametro, metrica)
    else:
        f = _avaliador_voo(arquivo_ork, parametro, indice_simulacao, motor_eng, atraso_ejecao_s, montagem,
                           _opcoes_voo(opcoes_voo), metrica)
    if isinstance(meta, str):
        r = an.buscar_extremo(f, minimo, maximo, meta == "max", max_avaliacoes)
    else:
        r = an.buscar_meta(f, minimo, maximo, float(meta), tolerancia, max_avaliacoes)
    out = {"parametro": parametro, "metrica": metrica, "meta": meta, "valor_otimo": r["valor"],
           "metrica_no_otimo": r["metrica"], "convergiu": r["convergiu"], "mensagem": r["mensagem"],
           "avaliacoes": len(r["historico"]), "historico": r["historico"]}
    if destino:
        if arquivo_ric:
            with orb.LOCK:
                motor = om.carregar_ric(_ric_existente(arquivo_ric))
                om.editar(motor, {parametro: r["valor"]})
                om.salvar_ric(motor, destino)
        else:
            doc, _ = _carregar(arquivo_ork)
            sim = _simulacao(doc, indice_simulacao)
            if parametro.strip() in OPCOES_TODAS:
                orb.configurar(sim, **{parametro.strip(): r["valor"]})
            else:
                with orb.LOCK:
                    of.editar(doc, sim, {parametro: r["valor"]})
            orb.salvar_ork(doc, destino)
        out["arquivo_salvo"] = _rel(destino)
    return out


INCERTEZAS_PADRAO = {"vento_ms": 2.0, "vento_direcao_graus": 30.0, "angulo_trilho_graus": 1.0,
                     "massa_pct": 2.0, "arrasto_pct": 5.0, "impulso_pct": 3.0}


@mcp.tool()
async def analisar_dispersao(arquivo_ork: str, ctx: Context, n_voos: int = 50, indice_simulacao: int = 0,
                             motor_eng: str | None = None, atraso_ejecao_s: float | None = None,
                             montagem: str | None = None, opcoes_voo: dict[str, Any] | None = None,
                             incertezas: dict[str, float] | None = None, semente: int = 0,
                             material_aletas: str | None = None,
                             modulo_cisalhamento_aletas_gpa: float | None = None,
                             arquivo_csv: str | None = None, sobrescrever: bool = False) -> dict:
    """Análise de dispersão (Monte Carlo): simula n_voos (5 a 200) sorteando as incertezas em torno das
    condições nominais (as do .ork + opcoes_voo) e devolve estatísticas (média, desvio, p5/p50/p95, pior
    caso) de apogeu, velocidades, estabilidade e deriva; a elipse de pouso de 95%; a probabilidade de cada
    problema (estabilidade < 1 cal, saída do trilho < 15 m/s...); e os piores voos.
    incertezas (1 desvio-padrão, distribuição normal; 0 desliga), padrões: vento_ms 2, vento_direcao_graus 30,
      angulo_trilho_graus 1, massa_pct 2 (massa estrutural, CG mantido), arrasto_pct 5 (fator no Cd, que
      preserva a variação com o Mach), impulso_pct 3 (curva de empuxo escalada). Cada voo também tem uma
      realização própria da turbulência. Com perfil de vento, vento_ms soma em todos os níveis.
    semente fixa todos os sorteios: a mesma chamada dá o mesmo resultado. arquivo_csv grava todos os voos.
    Leva ~0,5-1 s por voo no Hemera; o progresso é notificado."""
    sig, opcoes, sorteios = _preparar_dispersao(n_voos, incertezas, opcoes_voo, semente)
    g_aletas = _g_aletas(material_aletas, modulo_cisalhamento_aletas_gpa)
    destino_csv = _destino_novo(arquivo_csv, ".csv", sobrescrever) if arquivo_csv else None

    def trabalho():
        return _rodar_dispersao(arquivo_ork, indice_simulacao, motor_eng, atraso_ejecao_s, montagem, opcoes,
                                sorteios, semente, g_aletas,
                                lambda i: anyio.from_thread.run(ctx.report_progress, i, n_voos))

    voos, nominal, avisos, duracao = await anyio.to_thread.run_sync(_erros_java(trabalho))
    out = _resumir_dispersao(voos, nominal, avisos, duracao, semente, sig)
    if destino_csv:
        colunas = [k for k in voos[0] if k != "alertas"] + ["alertas"]
        with destino_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(colunas)
            for v in voos:
                w.writerow([*(v[k] for k in colunas[:-1]), " | ".join(v["alertas"])])
        out["arquivo_csv"] = _rel(destino_csv)
    return out


def _preparar_dispersao(n_voos, incertezas, opcoes_voo, semente) -> tuple[dict, dict, list[dict]]:
    """Valida e sorteia tudo antes de simular (a mesma semente dá os mesmos sorteios)."""
    if isinstance(n_voos, bool) or not isinstance(n_voos, int) or not 5 <= n_voos <= 200:
        raise ToolError("n_voos: de 5 a 200")
    desconhecidas = sorted(set(incertezas or {}) - set(INCERTEZAS_PADRAO))
    if desconhecidas:
        raise ToolError(f"incertezas: chave(s) desconhecida(s) {desconhecidas}. Aceitas: {list(INCERTEZAS_PADRAO)}")
    sig = INCERTEZAS_PADRAO | (incertezas or {})
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in sig.values()):
        raise ToolError("incertezas: valores numéricos >= 0 (desvio-padrão)")
    opcoes = _opcoes_voo(opcoes_voo)
    opcoes.pop("semente")
    rng = np.random.default_rng(semente)
    sorteios = [{"dv": rng.normal(0, sig["vento_ms"]), "ddir": rng.normal(0, sig["vento_direcao_graus"]),
                 "dang": rng.normal(0, sig["angulo_trilho_graus"]), "fm": 1 + rng.normal(0, sig["massa_pct"] / 100),
                 "fa": 1 + rng.normal(0, sig["arrasto_pct"] / 100), "fi": 1 + rng.normal(0, sig["impulso_pct"] / 100)}
                for _ in range(n_voos)]
    return sig, opcoes, sorteios


def _rodar_dispersao(arquivo_ork, indice_simulacao, motor_eng, atraso_ejecao_s, montagem, opcoes, sorteios,
                     semente, g_aletas, progresso=None):
    """Simula os voos sorteados (síncrono). progresso(i) é chamado após cada voo."""
    t0 = time.time()
    doc, avisos = _carregar(arquivo_ork)
    sim = _simulacao(doc, indice_simulacao)
    orb.configurar(sim, **{k: v for k, v in opcoes.items() if k not in FATORES_VOO})
    with orb.LOCK:
        nominal = orb.condicoes_lancamento(sim)
    voos = []
    for i, s in enumerate(sorteios):
        doc, _ = _carregar(arquivo_ork)
        sim = _simulacao(doc, indice_simulacao)
        op = dict(opcoes) | {"semente": semente * 100_000 + i,
                             "angulo_trilho_graus": abs(nominal["angulo_trilho_graus"] + s["dang"])}
        if nominal["modelo_vento"] == "AVERAGE":
            op |= {"vento_medio_ms": max(0.0, nominal["vento_medio_ms"] + s["dv"]),
                   "vento_direcao_graus": (nominal["vento_direcao_graus"] + s["ddir"]) % 360}
        else:
            op |= {"perfil_vento": [[n["altitude_m"], max(0.0, n["velocidade_ms"] + s["dv"]),
                                     (n["direcao_graus"] + s["ddir"]) % 360, n["turbulencia_pct"]]
                                    for n in nominal["perfil_vento"]],
                   "perfil_vento_referencia": nominal["perfil_vento_referencia"]}
        r = _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, op, [], g_aletas,
                  {"massa": s["fm"], "impulso": s["fi"], "arrasto": s["fa"]})
        seg, est, ev, pouso = r["seguranca"], r["estabilidade"], r["estabilidade_voo"], r["pouso"] or {}
        voos.append({
            "voo": i, "vento_ms": round(op.get("vento_medio_ms", nominal.get("vento_medio_ms") or 0) or 0, 2),
            "delta_vento_ms": round(s["dv"], 2), "vento_direcao_graus": round((op.get("vento_direcao_graus") or 0), 1),
            "angulo_trilho_graus": round(op["angulo_trilho_graus"], 2), "fator_massa": round(s["fm"], 4),
            "fator_arrasto": round(s["fa"], 4), "fator_impulso": round(s["fi"], 4),
            "apogeu_m": r["apogeu_m"], "velocidade_max_ms": r["velocidade_max_ms"], "mach_max": r["mach_max"],
            "velocidade_saida_trilho_ms": r["velocidade_saida_trilho_ms"],
            "margem_estatica_cal": est["margem_estatica_cal"], "estabilidade_saida_trilho_cal": ev["saida_trilho_cal"],
            "estabilidade_min_subida_cal": ev["min_subida_cal"], "pouso_leste_m": pouso.get("leste_m"),
            "pouso_norte_m": pouso.get("norte_m"), "distancia_pouso_m": pouso.get("distancia_m"),
            "velocidade_abertura_recuperacao_ms": r["velocidade_abertura_recuperacao_ms"],
            "velocidade_descida_final_ms": (seg["descida"] or [{}])[-1].get("velocidade_ms"),
            "velocidade_impacto_ms": r["velocidade_impacto_ms"], "q_max_kpa": seg["q_max_kpa"],
            "alertas": r["alertas_projeto"],
        })
        if progresso:
            progresso(i + 1)
    return voos, nominal, avisos, time.time() - t0


def _resumir_dispersao(voos, nominal, avisos, duracao, semente, sig) -> dict:
    def frac(cond):
        return round(sum(1 for v in voos if cond(v)) / len(voos), 3)

    metricas = ("apogeu_m", "velocidade_max_ms", "mach_max", "velocidade_saida_trilho_ms", "margem_estatica_cal",
                "estabilidade_saida_trilho_cal", "estabilidade_min_subida_cal", "distancia_pouso_m",
                "velocidade_abertura_recuperacao_ms", "velocidade_descida_final_ms", "velocidade_impacto_ms",
                "q_max_kpa")
    menor = lambda v: v["estabilidade_min_subida_cal"] if v["estabilidade_min_subida_cal"] is not None else 99  # noqa: E731
    return {
        "n_voos": len(voos), "semente": semente, "incertezas_1sigma": sig, "nominal": nominal,
        "avisos_carregamento": avisos, "tempo_s": round(duracao, 1),
        "estatisticas": {m: an.resumo_distribuicao([v[m] for v in voos]) for m in metricas},
        "pouso_95": an.elipse_95([v["pouso_leste_m"] for v in voos if v["pouso_leste_m"] is not None],
                                 [v["pouso_norte_m"] for v in voos if v["pouso_norte_m"] is not None]),
        "probabilidades": {
            "estabilidade_saida_trilho_abaixo_1cal": frac(lambda v: (v["estabilidade_saida_trilho_cal"] or 99) < 1),
            "estabilidade_min_subida_abaixo_1cal": frac(lambda v: menor(v) < 1),
            "saida_trilho_abaixo_15ms": frac(lambda v: v["velocidade_saida_trilho_ms"] < 15),
            "mach_acima_0_8": frac(lambda v: v["mach_max"] > 0.8),
            "descida_final_acima_9ms": frac(lambda v: (v["velocidade_descida_final_ms"] or 0) > 9),
        },
        "alertas": an.agrupar_alertas([v["alertas"] for v in voos]),
        "piores_voos": {
            "menor_estabilidade": [{k: v for k, v in x.items() if k != "alertas"} for x in sorted(voos, key=menor)[:3]],
            "maior_deriva": [{k: v for k, v in x.items() if k != "alertas"}
                             for x in sorted(voos, key=lambda v: -(v["distancia_pouso_m"] or 0))[:3]],
        },
    }


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


# ------------------------------------------------------------------------------------ calibração

def _curva(arquivo: str, colunas: dict) -> dict:
    """Curva de empuxo de um .eng, de um CSV de ensaio estático ou de um .ric (simulado)."""
    p = _caminho(arquivo)
    if not p.exists():
        raise ToolError(f"Arquivo não encontrado: {arquivo}")
    suf = p.suffix.lower()
    if suf == ".eng":
        return an.ler_eng(p)
    if suf == ".csv":
        return an.ler_csv_curva(p, **colunas)
    if suf == ".ric":
        with orb.LOCK:
            motor = om.carregar_ric(p)
            r = om.simular(motor)
        if not om.resumo(motor, r)["sucesso"]:
            raise ToolError(f"A simulação de {arquivo} falhou: {om.resumo(motor, r)['alertas']}")
        return om.curva_motor(r)
    raise ToolError(f"{arquivo}: use .eng, .csv (ensaio estático) ou .ric")


@mcp.tool()
def comparar_curvas_empuxo(referencia: str, comparada: str, coluna_tempo: str | None = None,
                           coluna_empuxo: str | None = None, coluna_pressao: str | None = None) -> dict:
    """Compara duas curvas de empuxo: .eng, .ric (simulado pelo openMotor) ou CSV de ensaio estático
    (colunas detectadas pelo nome: time/tempo, force/thrust/empuxo, pressure/pressao; unidades pelo
    cabeçalho, ex.: 'force (lbf)'). Devolve impulso, pico, instante do pico, queima (entre a 1ª e a
    última vez acima de 5% do pico), empuxo médio, Isp (se houver massa de propelente), as diferenças em %
    e o erro RMS ponto a ponto. Ex.: referencia='ensaios/estatico_1.csv', comparada='motores/m.ric'."""
    colunas = {"coluna_tempo": coluna_tempo, "coluna_empuxo": coluna_empuxo, "coluna_pressao": coluna_pressao}
    ref, cmp = _curva(referencia, colunas), _curva(comparada, colunas)
    return {"referencia": referencia, "comparada": comparada} | an.comparar_curvas(ref, cmp)


def _faixas(parametros, maximo: int) -> tuple[list[str], np.ndarray, np.ndarray]:
    if not isinstance(parametros, dict) or not 1 <= len(parametros) <= maximo:
        raise ToolError(f"parametros: de 1 a {maximo} itens {{nome: [mínimo, máximo]}}")
    nomes, lo, hi = [], [], []
    for nome, faixa in parametros.items():
        if (not isinstance(faixa, (list, tuple)) or len(faixa) != 2
                or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in faixa) or not faixa[0] < faixa[1]):
            raise ToolError(f"parametros['{nome}']: use [mínimo, máximo] com mínimo < máximo")
        nomes.append(nome)
        lo.append(float(faixa[0]))
        hi.append(float(faixa[1]))
    return nomes, np.array(lo), np.array(hi)


def _no_limite(nomes, valores, lo, hi) -> list[str]:
    return [f"'{n}' terminou no limite da faixa ({v:g}): o ótimo pode estar fora; amplie a faixa"
            for n, v, a, b in zip(nomes, valores, lo, hi) if min(v - a, b - v) <= 1e-3 * (b - a)]


@mcp.tool()
def calibrar_motor(arquivo_ric: str, referencia: str, parametros: dict[str, list[float]],
                   coluna_tempo: str | None = None, coluna_empuxo: str | None = None,
                   max_avaliacoes: int = 150, salvar_como: str | None = None, sobrescrever: bool = False) -> dict:
    """Ajusta até 4 parâmetros do motor (.ric) para a curva simulada reproduzir uma curva medida (ensaio
    estático em CSV ou .eng, ex.: curva reconstruída do voo), minimizando o erro RMS de empuxo.
    parametros: {caminho de editar_motor: [mínimo, máximo]}. Os mais úteis:
      'nozzle.efficiency' (nível de empuxo), 'propellant.a_factor' (velocidade de queima: multiplica o 'a'
      de r = a·Pⁿ), 'nozzle.throat_mm', 'nozzle.erosionCoeff_m_sPa' (erosão da garganta),
      'propellant.tabs[0].n', 'propellant.density_kg_m3'.
    Devolve os valores ajustados e a comparação antes/depois. Limite do modelo: o openMotor não simula queima
    erosiva, então um pico inicial causado por ela não será reproduzido (o erro restante mostra isso).
    salvar_como grava o motor calibrado em .ric NOVO. ~0,05-0,5 s por avaliação."""
    from scipy.optimize import minimize

    nomes, lo, hi = _faixas(parametros, 4)
    if not 10 <= max_avaliacoes <= 500:
        raise ToolError("max_avaliacoes: de 10 a 500")
    destino = _destino_novo(salvar_como, ".ric", sobrescrever) if salvar_como else None
    ref = _curva(referencia, {"coluna_tempo": coluna_tempo, "coluna_empuxo": coluna_empuxo, "coluna_pressao": None})
    with orb.LOCK:
        original = om.carregar_ric(_ric_existente(arquivo_ric))
        base = original.getDict()
        iniciais = np.array([om.ler_caminho(original, n) for n in nomes])
        om.editar(om.motor_de_dict(base), dict(zip(nomes, ((lo + hi) / 2).tolist())))  # valida os caminhos
    contagem = [0]

    def motor_com(xn):
        motor = om.motor_de_dict(base)
        om.editar(motor, dict(zip(nomes, (lo + np.clip(xn, 0, 1) * (hi - lo)).tolist())))
        return motor

    def objetivo(xn):
        contagem[0] += 1
        try:
            with orb.LOCK:
                motor = motor_com(xn)
                om.validar(motor)
                r = om.simular(motor)
        except ToolError:
            return 1e12
        if not om.resumo(motor, r)["sucesso"]:
            return 1e12
        return an.erro_rms(ref, om.curva_motor(r))

    x0 = np.clip((iniciais - lo) / (hi - lo), 0, 1)
    res = minimize(objetivo, x0, method="Powell", bounds=[(0, 1)] * len(nomes),
                   options={"maxfev": max_avaliacoes, "xtol": 1e-3, "ftol": 1e-4})
    finais = lo + np.clip(res.x, 0, 1) * (hi - lo)
    with orb.LOCK:
        antes = om.curva_motor(om.simular(om.motor_de_dict(base)))
        calibrado = motor_com(res.x)
        r_final = om.simular(calibrado)
        depois = om.curva_motor(r_final)
        if destino:
            om.salvar_ric(calibrado, destino)
    out = {"parametros_ajustados": {n: _sig(v) for n, v in zip(nomes, finais)},
           "parametros_iniciais": {n: _sig(v) for n, v in zip(nomes, iniciais)},
           "antes": an.comparar_curvas(ref, antes), "depois": an.comparar_curvas(ref, depois),
           "avaliacoes": contagem[0], "avisos": _no_limite(nomes, finais, lo, hi),
           "simulacao_calibrada": om.resumo(calibrado, r_final)}
    if destino:
        out["arquivo_salvo"] = _rel(destino)
    return out


def _sig(x: float) -> float:
    return float(f"{x:.6g}")


@mcp.tool()
@_erros_java
def calibrar_voo(arquivo_ork: str, arquivo_log: str, coluna_tempo: str | None = None,
                 coluna_altitude: str | None = None, unidade_altitude: str | None = None,
                 ajustar: list[str] | None = None, faixa: list[float] | None = None, indice_simulacao: int = 0,
                 motor_eng: str | None = None, atraso_ejecao_s: float | None = None, montagem: str | None = None,
                 opcoes_voo: dict[str, Any] | None = None, max_avaliacoes: int = 40) -> dict:
    """Ajusta fatores do modelo para o voo simulado reproduzir a altitude medida por um altímetro (CSV com
    tempo e altitude; colunas detectadas pelo nome ou informadas; altitude em m ou ft, pelo cabeçalho ou por
    unidade_altitude). Compara a SUBIDA: do instante em que cada série passa de 10 m até o apogeu do log
    (o relógio do altímetro não precisa começar na ignição; a altitude é relativa ao 1º ponto do log).
    ajustar: fatores entre 'arrasto' (padrão), 'impulso' e 'massa'; faixa: [mín, máx] dos fatores (padrão
    [0,5; 2,0]). Use as mesmas condições do voo real (motor_eng com a curva do motor usado, vento, trilho...).
    Devolve os fatores (ex.: fator_arrasto 1,18 = arrasto real 18% acima do modelo), o erro antes/depois e
    os apogeus. Para usar o resultado: fator_arrasto=... em simular_voo, ou em opcoes_voo nas outras tools."""
    from scipy.optimize import minimize

    ajustar = ajustar or ["arrasto"]
    if not set(ajustar) <= {"arrasto", "impulso", "massa"} or len(set(ajustar)) != len(ajustar):
        raise ToolError("ajustar: lista sem repetição com 'arrasto', 'impulso' e/ou 'massa'")
    lo_f, hi_f = faixa or [0.5, 2.0]
    if not 0.1 <= lo_f < hi_f <= 10:
        raise ToolError("faixa: [mín, máx] com 0,1 <= mín < máx <= 10")
    if not 5 <= max_avaliacoes <= 150:
        raise ToolError("max_avaliacoes: de 5 a 150")
    log_path = _caminho(arquivo_log)
    if not log_path.exists() or log_path.suffix.lower() != ".csv":
        raise ToolError(f"arquivo_log precisa ser um .csv existente no workspace: {arquivo_log}")
    log = an.ler_log_altitude(log_path, coluna_tempo, coluna_altitude, unidade_altitude)
    opcoes = _opcoes_voo(opcoes_voo)
    contagem = [0]

    def voo(fatores: dict):
        contagem[0] += 1
        doc, _ = _carregar(arquivo_ork)
        sim = _simulacao(doc, indice_simulacao)
        res = _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, opcoes, [], None, fatores)
        colunas, linhas, _ = orb.series_voo(sim)
        it, ia = colunas.index("time_s"), colunas.index("altitude_m")
        return res, an.erro_altitude(log, [l[it] for l in linhas], [l[ia] for l in linhas])

    def objetivo(x):
        return voo(dict(zip(ajustar, np.atleast_1d(x).tolist())))[1]["erro_rms_m"]

    _, inicial = voo({})
    if len(ajustar) == 1:
        r = an.buscar_extremo(objetivo, lo_f, hi_f, maximizar=False, max_avaliacoes=max_avaliacoes)
        valores = [r["valor"]]
    else:
        r = minimize(objetivo, np.ones(len(ajustar)), method="Powell", bounds=[(lo_f, hi_f)] * len(ajustar),
                     options={"maxfev": max_avaliacoes, "xtol": 1e-3, "ftol": 1e-4})
        valores = np.clip(r.x, lo_f, hi_f).tolist()
    res_final, final = voo(dict(zip(ajustar, valores)))
    ajustados = {f"fator_{k}": round(v, 4) for k, v in zip(ajustar, valores)}
    return {
        "fatores_ajustados": ajustados,
        "erro_rms_altitude_m": {"antes": round(inicial["erro_rms_m"], 2), "depois": round(final["erro_rms_m"], 2)},
        "apogeu_m": {"log": round(final["apogeu_log_m"], 1), "simulado_antes": round(inicial["apogeu_sim_m"], 1),
                     "simulado_depois": round(final["apogeu_sim_m"], 1)},
        "pontos_comparados": final["pontos"], "avaliacoes": contagem[0],
        "avisos": _no_limite(list(ajustados), valores, [lo_f] * len(valores), [hi_f] * len(valores)),
        "como_usar": f"simular_voo(..., {', '.join(f'{k}={v}' for k, v in ajustados.items())}) "
                     f"ou opcoes_voo={ajustados}",
    }


# -------------------------------------------------------------------------------------- relatório

@mcp.tool()
async def gerar_relatorio(arquivo_ork: str, arquivo_html: str, ctx: Context, indice_simulacao: int = 0,
                          motor_eng: str | None = None, arquivo_ric: str | None = None,
                          atraso_ejecao_s: float | None = None, montagem: str | None = None,
                          opcoes_voo: dict[str, Any] | None = None, material_aletas: str | None = None,
                          modulo_cisalhamento_aletas_gpa: float | None = None,
                          dispersao: dict[str, Any] | None = None, titulo: str | None = None,
                          sobrescrever: bool = False) -> dict:
    """Gera um relatório de voo em HTML autocontido (abre no navegador, imprime em PDF, envia por e-mail):
    resumo, alertas, gráficos (altitude e velocidade, estabilidade e Mach na subida, empuxo e arrasto,
    vista de cima com o ponto de pouso), estabilidade e massa, segurança e estrutura (q máximo, acelerações,
    descida, impacto, flutter), condições de lançamento e, opcionalmente:
      arquivo_ric: seção do motor (curva de empuxo e pressão, resumo do openMotor);
      dispersao: {"n_voos": 50, "incertezas": {...}, "semente": 0} acrescenta a análise de dispersão
        (estatísticas, probabilidades, pousos e elipse de 95%), como em analisar_dispersao.
    As condições do voo seguem simular_voo (motor_eng, atraso, opcoes_voo, material das aletas).
    Grava em arquivo NOVO no workspace (sobrescrever=True só se pedido). Devolve o caminho e o resumo."""
    destino = _destino_novo(arquivo_html, ".html", sobrescrever)
    opcoes = _opcoes_voo(opcoes_voo)
    g_aletas = _g_aletas(material_aletas, modulo_cisalhamento_aletas_gpa)
    prep = None
    if dispersao is not None:
        if not isinstance(dispersao, dict) or set(dispersao) - {"n_voos", "incertezas", "semente"}:
            raise ToolError("dispersao: objeto com n_voos, incertezas e/ou semente")
        sem = dispersao.get("semente", 0)
        prep = (_preparar_dispersao(dispersao.get("n_voos", 50), dispersao.get("incertezas"), opcoes_voo, sem), sem)
    ric = _ric_existente(arquivo_ric) if arquivo_ric else None

    def trabalho():
        doc, avisos = _carregar(arquivo_ork)  # espera a JVM terminar de subir ANTES de importar o matplotlib:
        # ARMADILHA (Windows): carregar as DLLs do matplotlib enquanto a JVM inicializa em outra thread travou o
        # processo (loader lock): jvm-boot parada em OpenRocketCore.initialize, import parado em create_module.
        from . import relatorio as rel  # import tardio: o servidor sobe sem pagar o custo do matplotlib

        sim = _simulacao(doc, indice_simulacao)
        voo = _voar(doc, sim, motor_eng, atraso_ejecao_s, montagem, opcoes, avisos, g_aletas)
        colunas, linhas, eventos = orb.series_voo(sim)
        motor_voo = ", ".join(m["motor_atual"] for m in orb.montagens(doc, sim)) or "—"
        s = rel.series_dict(colunas, linhas)
        graficos = {"trajetoria": rel.grafico_trajetoria(s, eventos), "subida": rel.grafico_subida(s, eventos),
                    "empuxo": rel.grafico_empuxo_voo(s, eventos)}
        motor = None
        if ric:
            with orb.LOCK:
                m = om.carregar_ric(ric)
                r = om.simular(m)
            motor = {"arquivo": arquivo_ric, "resumo": om.resumo(m, r)}
            if motor["resumo"]["sucesso"]:
                motor["grafico"] = rel.grafico_motor(om.curva_motor(r))
        disp = None
        if prep:
            (sig, op_d, sorteios), sem = prep
            n = len(sorteios)
            voos, nominal, av, dur = _rodar_dispersao(arquivo_ork, indice_simulacao, motor_eng, atraso_ejecao_s,
                                                      montagem, op_d, sorteios, sem, g_aletas,
                                                      lambda i: anyio.from_thread.run(ctx.report_progress, i, n))
            disp = {"resumo": _resumir_dispersao(voos, nominal, av, dur, sem, sig)}
            disp["grafico"] = rel.grafico_pouso(s, voos, disp["resumo"]["pouso_95"])
        else:
            graficos["pouso"] = rel.grafico_pouso(s)
        contexto = {"foguete": str(doc.getRocket().getName()), "simulacao": str(sim.getName()),
                    "motor": motor_voo, "arquivo_ork": arquivo_ork}
        texto = rel.montar_html(titulo or f"Relatório de voo: {contexto['foguete']}", contexto, voo, graficos,
                                motor, disp)
        destino.write_text(texto, encoding="utf-8")
        return voo, disp, len(texto)

    voo, disp, tamanho = await anyio.to_thread.run_sync(_erros_java(trabalho))
    out = {"arquivo_html": _rel(destino), "tamanho_kb": round(tamanho / 1024, 1),
           "resumo": {k: voo[k] for k in ("apogeu_m", "velocidade_max_ms", "mach_max", "velocidade_saida_trilho_ms")},
           "alertas_projeto": voo["alertas_projeto"]}
    if disp:
        out["dispersao"] = {"n_voos": disp["resumo"]["n_voos"], "probabilidades": disp["resumo"]["probabilidades"]}
    return out


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
