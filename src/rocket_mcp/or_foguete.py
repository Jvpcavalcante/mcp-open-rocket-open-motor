"""Leitura e edição do foguete (.ork) e estabilidade estática (ler_foguete, editar_foguete, varrer_parametro).

Os parâmetros usam nomes em inglês + sufixo de unidade (span_mm, deployAltitude_m...), como no motor.
ARMADILHAS (vistas no Hemera, da equipe Minerva):
- Massa e CG sobrescritos em quase todo componente: mudar uma dimensão NÃO muda a massa. A edição avisa e
  permite editar a massa sobrescrita (overrideMass_kg).
- Nomes repetidos ('Centering Ring' em dois módulos): componentes são endereçados por caminho, e basta o
  final do caminho que seja único ('Lower Propulsion Module/Centering Ring').
- A abertura de paraquedas é POR configuração de voo (cada simulação do Hemera tem a sua): os parâmetros
  deploy* leem e editam a configuração da simulação escolhida.
Tudo aqui deve ser chamado com or_bridge.LOCK.
"""
from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

import jpype
from mcp.server.mcpserver.exceptions import ToolError

from . import aletas

_PACOTE = "info.openrocket.core."
_FATORES = {"mm": 1e-3, "deg": math.pi / 180, "kg": 1.0, "m": 1.0, "s": 1.0, "": 1.0}


def _cls(nome: str):
    return jpype.JClass(_PACOTE + nome)


def _rc(nome: str):
    return _cls("rocketcomponent." + nome)


def _sig(x: float) -> float:
    return float(f"{x:.6g}")


@dataclass(frozen=True)
class _Param:
    nome: str                           # nome externo, com sufixo de unidade
    aplica: Callable[[Any], bool]       # o componente tem este parâmetro?
    ler: Callable[[Any, Any], Any]      # (componente, simulação) -> valor SI
    escrever: Callable[[Any, Any, Any], None]
    tipo: str = "float"                 # float | int | enum | bool
    opcoes: Callable[[], list[str]] | None = None
    minimo: float | None = 0.0
    anulavel: bool = False              # null desliga (overrides)
    afeta_massa: bool = False           # avisar se a massa do componente estiver sobrescrita

    @property
    def fator(self) -> float:
        suf = self.nome.rsplit("_", 1)[-1] if "_" in self.nome else ""
        return _FATORES.get(suf, 1.0)


def _e(*classes: str) -> Callable[[Any], bool]:
    return lambda c: any(isinstance(c, _rc(k)) for k in classes)


def _nao(*classes: str) -> Callable[[Any], bool]:
    return lambda c: not any(isinstance(c, _rc(k)) for k in classes)


def _num(nome, aplica, getter, setter, escala=1.0, antes=None, tipo="float", minimo=0.0, afeta_massa=False):
    """Parâmetro numérico ligado a um getter/setter Java. escala: valor_SI = getter() * escala
    (ex.: 2 para expor diâmetro de um getRadius). antes: setter booleano desligado antes (automáticos)."""
    def ler(c, sim):
        return float(getattr(c, getter)()) * escala

    def escrever(c, sim, v):
        if antes:
            getattr(c, antes)(False)
        getattr(c, setter)(int(v) if tipo == "int" else float(v) / escala)

    return _Param(nome, aplica, ler, escrever, tipo=tipo, minimo=minimo, afeta_massa=afeta_massa)


def _enum(nome, aplica, getter, setter, classe_enum):
    def escrever(c, sim, v):
        getattr(c, setter)(_cls(classe_enum).valueOf(v))

    return _Param(nome, aplica, lambda c, sim: str(getattr(c, getter)().name()), escrever, tipo="enum",
                  opcoes=lambda: [str(x.name()) for x in _cls(classe_enum).values()])


def _deploy(c, sim, escrita: bool = False):
    """Configuração de abertura da simulação. Se ela usa a padrão (compartilhada), a escrita cria uma
    cópia própria, para não alterar as outras simulações.
    ARMADILHA: isDefault(fcid) dá False mesmo quando a configuração não tem entrada própria e get(fcid)
    devolve o objeto padrão compartilhado (foguete de exemplo). O teste certo é containsId(fcid)."""
    dcs = c.getDeploymentConfigurations()
    fcid = sim.getFlightConfigurationId()
    if escrita and not dcs.containsId(fcid):
        dcs.set(fcid, dcs.getDefault().copy(fcid))
    return dcs.get(fcid)


def _override_massa():
    def ler(c, sim):
        return float(c.getOverrideMass()) if c.isMassOverridden() else None

    def escrever(c, sim, v):
        if v is None:
            c.setMassOverridden(False)
        else:
            c.setOverrideMass(float(v))
            c.setMassOverridden(True)

    return _Param("overrideMass_kg", _nao("ComponentAssembly"), ler, escrever, anulavel=True)


def _override_cg():
    def ler(c, sim):
        return float(c.getOverrideCGX()) if c.isCGOverridden() else None

    def escrever(c, sim, v):
        if v is None:
            c.setCGOverridden(False)
        else:
            c.setOverrideCGX(float(v))
            c.setCGOverridden(True)

    return _Param("overrideCG_mm", _nao("ComponentAssembly"), ler, escrever, anulavel=True, minimo=None)


def _materiais() -> dict[str, Any]:
    """Materiais volumétricos (BULK) do banco do OpenRocket, por nome."""
    from info.openrocket.core.database import Databases

    return {str(m.getName()): m for m in Databases.BULK_MATERIAL}


def _material():
    """Material do banco do OpenRocket pelo nome ('Carbon fiber', 'Aluminum', 'Fiberglass'...).
    Só muda a massa: o OpenRocket não usa rigidez em lugar nenhum (o flutter usa o módulo de cisalhamento
    informado à parte)."""
    return _Param("material", _e("ExternalComponent", "StructuralComponent"),
                  lambda c, sim: str(c.getMaterial().getName()),
                  lambda c, sim, v: c.setMaterial(_materiais()[v]),
                  tipo="material", opcoes=lambda: sorted(_materiais()), afeta_massa=True)


def _densidade_material():
    """Densidade do material atual: cria um material próprio com o mesmo nome (gravado no .ork)."""
    def escrever(c, sim, v):
        m = c.getMaterial()
        c.setMaterial(_cls("material.Material").newMaterial(m.getType(), m.getName(), float(v), True))

    return _Param("materialDensity_kg_m3", _e("ExternalComponent", "StructuralComponent"),
                  lambda c, sim: float(c.getMaterial().getDensity()), escrever, afeta_massa=True)


def _parametros() -> list[_Param]:
    comprimento = _e("SymmetricComponent", "MassObject", "RingComponent", "LaunchLug")
    transicao = lambda c: isinstance(c, _rc("Transition")) and not isinstance(c, _rc("NoseCone"))  # noqa: E731
    posicionavel = _nao("SymmetricComponent", "ComponentAssembly")  # corpos são posicionados em sequência
    return [
        # comuns
        _override_massa(),
        _Param("overrideMassIncludesChildren", _nao("ComponentAssembly"),
               lambda c, sim: bool(c.isSubcomponentsOverriddenMass()),
               lambda c, sim, v: c.setSubcomponentsOverriddenMass(bool(v)), tipo="bool"),
        _override_cg(),
        _enum("positionReference", posicionavel, "getAxialMethod", "setAxialMethod",
              "rocketcomponent.position.AxialMethod"),
        _num("position_mm", posicionavel, "getAxialOffset", "setAxialOffset", minimo=None),
        _num("length_mm", comprimento, "getLength", "setLength", afeta_massa=True),
        # corpos
        _num("outerDiameter_mm", _e("BodyTube"), "getOuterRadius", "setOuterRadius", 2, "setOuterRadiusAutomatic",
             afeta_massa=True),
        _num("thickness_mm", _e("BodyTube", "Transition", "FinSet"), "getThickness", "setThickness",
             afeta_massa=True),
        _material(),
        _densidade_material(),
        _enum("finish", _e("ExternalComponent"), "getFinish", "setFinish", "rocketcomponent.ExternalComponent$Finish"),
        _num("baseDiameter_mm", _e("NoseCone"), "getBaseRadius", "setBaseRadius", 2, "setBaseRadiusAutomatic",
             afeta_massa=True),
        _num("foreDiameter_mm", transicao, "getForeRadius", "setForeRadius", 2, "setForeRadiusAutomatic",
             afeta_massa=True),
        _num("aftDiameter_mm", transicao, "getAftRadius", "setAftRadius", 2, "setAftRadiusAutomatic",
             afeta_massa=True),
        _enum("shape", _e("Transition"), "getShapeType", "setShapeType", "rocketcomponent.Transition$Shape"),
        _num("shapeParameter", _e("Transition"), "getShapeParameter", "setShapeParameter"),
        # aletas
        _num("count", _e("FinSet"), "getFinCount", "setFinCount", tipo="int", minimo=1, afeta_massa=True),
        _num("cantAngle_deg", _e("FinSet"), "getCantAngle", "setCantAngle", minimo=None),
        _enum("crossSection", _e("FinSet"), "getCrossSection", "setCrossSection", "rocketcomponent.FinSet$CrossSection"),
        _num("rootChord_mm", _e("TrapezoidFinSet"), "getRootChord", "setRootChord", afeta_massa=True),
        _num("tipChord_mm", _e("TrapezoidFinSet"), "getTipChord", "setTipChord", afeta_massa=True),
        _num("span_mm", _e("TrapezoidFinSet", "EllipticalFinSet"), "getHeight", "setHeight", afeta_massa=True),
        _num("sweepLength_mm", _e("TrapezoidFinSet"), "getSweep", "setSweep", minimo=None),
        # mudar span_mm mantém sweepLength_mm (o ângulo muda): sweepAngle_deg é aplicado por último
        _num("sweepAngle_deg", _e("TrapezoidFinSet"), "getSweepAngle", "setSweepAngle", minimo=None),
        _num("rootChord_mm", _e("EllipticalFinSet"), "getLength", "setLength", afeta_massa=True),
        _num("filletRadius_mm", _e("FinSet"), "getFilletRadius", "setFilletRadius", afeta_massa=True),
        _num("tabHeight_mm", _e("FinSet"), "getTabHeight", "setTabHeight", afeta_massa=True),
        _num("tabLength_mm", _e("FinSet"), "getTabLength", "setTabLength", afeta_massa=True),
        # massas
        _num("mass_kg", _e("MassComponent"), "getComponentMass", "setComponentMass", afeta_massa=True),
        # recuperação
        _num("diameter_mm", _e("Parachute"), "getDiameter", "setDiameter", afeta_massa=True),
        _num("cd", _e("Parachute"), "getCD", "setCD", antes="setCDAutomatic"),
        _num("lineLength_mm", _e("Parachute"), "getLineLength", "setLineLength", afeta_massa=True),
        _num("lineCount", _e("Parachute"), "getLineCount", "setLineCount", tipo="int", afeta_massa=True),
        _Param("deployEvent", _e("RecoveryDevice"), lambda c, sim: str(_deploy(c, sim).getDeployEvent().name()),
               lambda c, sim, v: _deploy(c, sim, True).setDeployEvent(
                   _rc("DeploymentConfiguration$DeployEvent").valueOf(v)),
               tipo="enum", opcoes=lambda: [str(x.name()) for x in _rc("DeploymentConfiguration$DeployEvent").values()]),
        _Param("deployAltitude_m", _e("RecoveryDevice"), lambda c, sim: float(_deploy(c, sim).getDeployAltitude()),
               lambda c, sim, v: _deploy(c, sim, True).setDeployAltitude(float(v))),
        _Param("deployDelay_s", _e("RecoveryDevice"), lambda c, sim: float(_deploy(c, sim).getDeployDelay()),
               lambda c, sim, v: _deploy(c, sim, True).setDeployDelay(float(v))),
    ]


_CACHE: list[_Param] = []


def _todos() -> list[_Param]:
    if not _CACHE:
        _CACHE.extend(_parametros())  # JClass só existe com a JVM no ar
    return _CACHE


def _params_de(c) -> dict[str, _Param]:
    return {p.nome: p for p in _todos() if p.aplica(c)}


def parametros(c) -> list[str]:
    return sorted(_params_de(c))


# ----------------------------------------------------------------------------------------------- árvore

def _nome(c) -> str:
    return str(c.getName()).strip()


def componentes(doc) -> list[tuple[str, Any]]:
    """(caminho, componente) em ordem de árvore, a partir dos estágios ('Sustainer/Nosecone/...').
    Irmãos com o mesmo nome (dois 'Centering ring' no mesmo tubo, no foguete de exemplo) recebem
    índice: 'Centering ring[0]', 'Centering ring[1]'."""
    out = []

    def visitar(c, prefixo):
        filhos = list(c.getChildren())
        nomes = [_nome(f) for f in filhos]
        vistos: dict[str, int] = {}
        for filho, nome in zip(filhos, nomes):
            if nomes.count(nome) > 1:
                vistos[nome] = vistos.get(nome, -1) + 1
                nome = f"{nome}[{vistos[nome]}]"
            caminho = f"{prefixo}/{nome}" if prefixo else nome
            out.append((caminho, filho))
            visitar(filho, caminho)

    visitar(doc.getRocket(), "")
    return out


def _casa(pedido: str, real: str) -> bool:
    """Segmento do caminho: igual, ou sem o índice de irmão ('centering ring' casa 'centering ring[1]')."""
    return real == pedido or re.sub(r"\[\d+\]$", "", real) == pedido


def _resolver(comps, caminho: str, obrigatorio: bool = True):
    alvo = [s.strip().casefold() for s in caminho.strip().strip("/").split("/") if s.strip()]
    if not alvo:
        raise ToolError("Caminho de componente vazio")
    achados = []
    for p, c in comps:
        segmentos = [s.casefold() for s in p.split("/")]
        if len(segmentos) >= len(alvo) and all(_casa(a, r) for a, r in zip(alvo, segmentos[-len(alvo):])):
            achados.append((p, c))
    if len(achados) > 1:  # pedido exato vence o casamento sem índice ('Centering ring[1]')
        exatos = [(p, c) for p, c in achados if [s.casefold() for s in p.split("/")][-len(alvo):] == alvo]
        achados = exatos or achados
    if len(achados) == 1:
        return achados[0]
    if len(achados) > 1:
        raise ToolError(f"'{caminho}' é ambíguo; use mais do caminho: {[p for p, _ in achados]}")
    if not obrigatorio:
        return None
    nomes = sorted({p.split("/")[-1] for p, _ in comps})
    parecidos = difflib.get_close_matches(caminho.strip().split("/")[-1], nomes, n=5, cutoff=0.5)
    raise ToolError(f"Componente '{caminho}' não encontrado." + (f" Parecidos: {parecidos}" if parecidos else
                                                                  " Use ler_foguete para ver os caminhos."))


def _valor_externo(p: _Param, v):
    if v is None or p.tipo in ("enum", "bool", "material"):
        return v
    if p.tipo == "int":
        return int(v)
    return _sig(v / p.fator)


def descrever_componente(caminho: str, c, sim) -> dict:
    params = {}
    for nome, p in _params_de(c).items():
        try:
            params[nome] = _valor_externo(p, p.ler(c, sim))
        except Exception:  # noqa: BLE001 (getter ausente numa subclasse: só não lista)
            continue
    out = {"caminho": caminho, "tipo": str(c.getClass().getSimpleName()), "params": params}
    if c.isMassive():
        out["massa_material_kg"] = round(float(c.getComponentMass()), 4)
    if isinstance(c, _rc("FinSet")):
        out["geometria"] = geometria_aletas(c)
    return out


def geometria_aletas(c) -> dict:
    """Grandezas derivadas de UMA aleta (área, alongamento, afilamento, enflechamentos, CMA). Só as
    trapezoidais têm o conjunto completo; nas outras, área e alongamento vêm do contorno do OpenRocket."""
    if isinstance(c, _rc("TrapezoidFinSet")):
        return aletas.geometria_trapezio(float(c.getRootChord()), float(c.getTipChord()), float(c.getHeight()),
                                         float(c.getSweep()), float(c.getThickness()))
    area, s = float(c.getPlanformArea()), float(c.getSpan())
    return {"area_mm2": round(area * 1e6, 1), "alongamento": round(s * s / area, 4) if area > 0 else None}


def opcoes_enum() -> dict[str, list[str]]:
    return {p.nome: p.opcoes() for p in _todos() if p.opcoes is not None}


# ----------------------------------------------------------------------------------------------- edição

def _converter(p: _Param, valor, onde: str):
    if valor is None:
        if p.anulavel:
            return None
        raise ToolError(f"{onde}: não aceita null")
    if p.tipo == "enum":
        opcoes = p.opcoes()
        alvo = str(valor).strip().upper().replace(" ", "_")
        if alvo not in opcoes:
            raise ToolError(f"{onde}: valor {valor!r} inválido. Opções: {opcoes}")
        return alvo
    if p.tipo == "material":
        opcoes = {o.casefold(): o for o in p.opcoes()}
        alvo = str(valor).strip().casefold()
        if alvo not in opcoes:
            parecidos = difflib.get_close_matches(str(valor), list(opcoes.values()), n=5, cutoff=0.4)
            raise ToolError(f"{onde}: material {valor!r} não está no banco do OpenRocket. "
                            + (f"Parecidos: {parecidos}. " if parecidos else "")
                            + "Opções em ler_foguete (opcoes.material); para outra densidade, escolha o mais "
                              "parecido e altere materialDensity_kg_m3.")
        return opcoes[alvo]
    if p.tipo == "bool":
        if not isinstance(valor, bool):
            raise ToolError(f"{onde}: esperado true/false, recebido {valor!r}")
        return valor
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor):
        raise ToolError(f"{onde}: esperado um número, recebido {valor!r}")
    if p.tipo == "int" and valor != int(valor):
        raise ToolError(f"{onde}: esperado um inteiro, recebido {valor!r}")
    if p.minimo is not None and valor < p.minimo:
        raise ToolError(f"{onde} = {valor} abaixo do mínimo {p.minimo}")
    return valor * p.fator


def _massa_sobrescrita_por(c):
    """O próprio componente, ou o ancestral cujo override de massa inclui os filhos."""
    if c.isMassOverridden():
        return c
    pai = c.getParent()
    while pai is not None:
        if pai.isMassOverridden() and pai.isSubcomponentsOverriddenMass():
            return pai
        pai = pai.getParent()
    return None


def _separar(chave: str) -> tuple[str, str | None]:
    """'Caminho/Do/Componente.param' -> (caminho, param). Nomes de componente podem ter '.', então só
    separa se o sufixo for um parâmetro conhecido."""
    if "." in chave:
        caminho, param = chave.rsplit(".", 1)
        if param in {p.nome for p in _todos()}:
            return caminho, param
    return chave, None


def _aplicar(caminho: str, c, sim, nome: str, valor, aplicadas: list, efeitos: list) -> None:
    params = _params_de(c)
    if nome not in params:
        raise ToolError(f"'{caminho}' ({c.getClass().getSimpleName()}) não tem o parâmetro '{nome}'. "
                        f"Aceitos: {sorted(params)}")
    p = params[nome]
    onde = f"{caminho}.{nome}"
    antes = _valor_externo(p, p.ler(c, sim))
    p.escrever(c, sim, _converter(p, valor, onde))
    depois = _valor_externo(p, p.ler(c, sim))
    aplicadas.append({"parametro": onde, "antes": antes, "depois": depois})
    if isinstance(valor, (int, float)) and not isinstance(valor, bool) and isinstance(depois, (int, float)) \
            and not math.isclose(depois, valor, rel_tol=1e-6, abs_tol=1e-9):
        efeitos.append(f"O OpenRocket ajustou {onde} para {depois} (pedido: {valor})")
    dono = _massa_sobrescrita_por(c) if p.afeta_massa else None
    if dono is not None:
        efeitos.append(f"A massa de '{_nome(dono)}' está sobrescrita ({float(dono.getOverrideMass()):.3f} kg): "
                       f"{onde} não altera a massa do foguete. Edite '{_nome(dono)}.overrideMass_kg' se necessário.")


def _novo_lastro(comps, caminho: str, valores: dict, sim, aplicadas, efeitos):
    if "/" not in caminho.strip("/"):
        raise ToolError(f"'{caminho}' não existe. Para criar uma massa, informe o pai: 'Payload Module/{caminho}'")
    pai_caminho, nome = caminho.strip("/").rsplit("/", 1)
    pai_caminho, pai = _resolver(comps, pai_caminho)
    if "mass_kg" not in valores:
        raise ToolError(f"Nova massa '{caminho}': informe ao menos mass_kg (e position_mm a partir do topo do pai)")
    mc = _rc("MassComponent")(0.05, 0.02, 0.0)
    mc.setName(nome)
    mc.setMassComponentType(_rc("MassComponent$MassComponentType").MASSCOMPONENT)
    mc.setAxialMethod(_cls("rocketcomponent.position.AxialMethod").TOP)
    pai.addChild(mc)
    novo = f"{pai_caminho}/{nome}"
    aplicadas.append({"parametro": novo, "antes": None, "depois": "criado (MassComponent)"})
    for param, v in valores.items():
        _aplicar(novo, mc, sim, param, v, aplicadas, efeitos)


# Ordem de aplicação dentro de uma edição: trocar a referência recalcula o deslocamento (position_mm);
# trocar o material recria a densidade; o ângulo de enflechamento depende do span (aplicado por último).
_PRIMEIRO, _ULTIMO = ("positionReference", "material"), ("sweepAngle_deg",)


def _ordem(param: str | None) -> int:
    return 0 if param in _PRIMEIRO else 2 if param in _ULTIMO else 1


def _sem_conflito(pares) -> None:
    """sweepLength_mm e sweepAngle_deg do mesmo componente na mesma edição: um anula o outro."""
    params: dict[str, tuple[str, set]] = {}
    for caminho, param in pares:
        params.setdefault(caminho.strip().casefold(), (caminho.strip(), set()))[1].add(param)
    for caminho, ps in params.values():
        if {"sweepLength_mm", "sweepAngle_deg"} <= ps:
            raise ToolError(f"'{caminho}': informe sweepLength_mm OU sweepAngle_deg (um define o outro)")


def editar(doc, sim, alteracoes: dict) -> dict:
    """Aplica {caminho.param: valor} e operações de componente:
    'Pai/Nome': {...} cria uma massa (MassComponent) se não existir, ou altera vários parâmetros;
    'Caminho': null remove uma massa (só MassComponent)."""
    if not isinstance(alteracoes, dict) or not alteracoes:
        raise ToolError("alteracoes: informe ao menos uma alteração {'Componente.param': valor}")
    aplicadas, efeitos = [], []
    _sem_conflito([_separar(k) for k in alteracoes]
                  + [(k, p) for k, v in alteracoes.items() if isinstance(v, dict) for p in v])
    ordem = sorted(alteracoes.items(), key=lambda kv: _ordem(_separar(kv[0])[1]))
    for chave, valor in ordem:
        comps = componentes(doc)
        caminho, param = _separar(chave)
        if param is not None:
            achado_caminho, c = _resolver(comps, caminho)
            _aplicar(achado_caminho, c, sim, param, valor, aplicadas, efeitos)
            continue
        achado = _resolver(comps, caminho, obrigatorio=False)
        if valor is None:
            if achado is None:
                raise ToolError(f"Componente '{caminho}' não encontrado para remover")
            achado_caminho, c = achado
            if not isinstance(c, _rc("MassComponent")):
                raise ToolError(f"Só massas (MassComponent) podem ser removidas; '{achado_caminho}' é "
                                f"{c.getClass().getSimpleName()}")
            c.getParent().removeChild(c)
            aplicadas.append({"parametro": achado_caminho, "antes": "MassComponent", "depois": "removido"})
        elif isinstance(valor, dict):
            if achado is None:
                _novo_lastro(comps, caminho, valor, sim, aplicadas, efeitos)
            else:
                for param, v in sorted(valor.items(), key=lambda kv: _ordem(kv[0])):
                    _aplicar(achado[0], achado[1], sim, param, v, aplicadas, efeitos)
        else:
            raise ToolError(f"'{chave}': use 'Componente.param': valor, 'Pai/Nova massa': {{...}} ou "
                            "'Massa': null")
    return {"aplicadas": aplicadas, "efeitos": list(dict.fromkeys(efeitos))}


# ----------------------------------------------------------------------------------- estabilidade estática

def estabilidade_estatica(doc, sim, mach: float = 0.3) -> dict:
    """Margem estática como a GUI do OpenRocket mostra: CP pelo Barrowman (Mach 0,3, AoA 0) e CG da
    configuração de voo da simulação, com o motor que estiver nela."""
    from info.openrocket.core.aerodynamics import BarrowmanCalculator, FlightConditions
    from info.openrocket.core.logging import WarningSet
    from info.openrocket.core.masscalc import MassCalculator

    conf = doc.getRocket().getFlightConfiguration(sim.getFlightConfigurationId())
    cond = FlightConditions(conf)
    cond.setMach(mach)
    cond.setAOA(0.0)
    cp = float(BarrowmanCalculator().getCP(conf, cond, WarningSet()).x)
    lanc, queima = MassCalculator.calculateLaunch(conf), MassCalculator.calculateBurnout(conf)
    ref = float(cond.getRefLength())
    cg, cgq = float(lanc.getCM().x), float(queima.getCM().x)
    ok = ref > 0 and cp == cp
    return {
        "margem_estatica_cal": round((cp - cg) / ref, 2) if ok else None,
        "margem_estatica_burnout_cal": round((cp - cgq) / ref, 2) if ok else None,
        "cp_mm": round(cp * 1000, 1) if cp == cp else None,
        "cg_mm": round(cg * 1000, 1),
        "cg_burnout_mm": round(cgq * 1000, 1),
        "diametro_referencia_mm": round(ref * 1000, 1),
        "massa_decolagem_kg": round(float(lanc.getMass()), 3),
        "massa_burnout_kg": round(float(queima.getMass()), 3),
        "com_motor": bool(conf.hasMotors()),
    }


# ----------------------------------------------------------------------------- aerodinâmica e aletas

def conjuntos_aletas(doc) -> list[tuple[str, Any]]:
    return [(p, c) for p, c in componentes(doc) if isinstance(c, _rc("FinSet"))]


def resolver_aletas(doc, caminho: str | None) -> tuple[str, Any]:
    """O conjunto de aletas pedido, ou o único do foguete."""
    if caminho is not None:
        achado_caminho, c = _resolver(componentes(doc), caminho)
        if not isinstance(c, _rc("FinSet")):
            raise ToolError(f"'{achado_caminho}' é {c.getClass().getSimpleName()}, não um conjunto de aletas")
        return achado_caminho, c
    todas = conjuntos_aletas(doc)
    if len(todas) != 1:
        raise ToolError("O foguete não tem aletas" if not todas else
                        f"O foguete tem {len(todas)} conjuntos de aletas; informe aletas: {[p for p, _ in todas]}")
    return todas[0]


def resumo_aletas(caminho: str, c, sim) -> dict:
    params = {k: v for k, v in descrever_componente(caminho, c, sim)["params"].items()
              if k not in ("overrideMass_kg", "overrideMassIncludesChildren", "overrideCG_mm", "positionReference")}
    dono = _massa_sobrescrita_por(c)
    return {"caminho": caminho, "tipo": str(c.getClass().getSimpleName()), "params": params,
            "geometria": geometria_aletas(c), "massa_material_kg": round(float(c.getComponentMass()), 4),
            "massa_sobrescrita_por": _nome(dono) if dono is not None else None}


def compensar_massa(c, massa_antes: float) -> str | None:
    """Com a massa das aletas sobrescrita (por elas ou por um ancestral que inclui os filhos), soma ao
    override a variação da massa calculada das aletas. Sem isso, mudar a geometria não muda a massa."""
    dono = _massa_sobrescrita_por(c)
    delta = float(c.getComponentMass()) - massa_antes
    if dono is None or abs(delta) < 1e-9:
        return None
    antes = float(dono.getOverrideMass())
    dono.setOverrideMass(antes + delta)
    return (f"Massa sobrescrita de '{_nome(dono)}' ajustada de {antes:.4f} para {antes + delta:.4f} kg "
            f"({delta:+.4f} kg, variação da massa calculada de '{_nome(c)}')")


def _forcas(f, ref_cp: bool = True) -> dict:
    cp = f.getCP()
    cna = float(cp.weight) if cp is not None else 0.0
    out = {"cna": round(cna, 4)}
    if ref_cp:
        out["cp_mm"] = round(float(cp.x) * 1000, 1) if cna != 0 and cp.x == cp.x else None
    return out | {"cd": round(float(f.getCD()), 5), "cd_atrito": round(float(f.getFrictionCD()), 5),
                  "cd_pressao": round(float(f.getPressureCD()), 5), "cd_base": round(float(f.getBaseCD()), 5)}


def analise_aerodinamica(doc, sim, machs: list[float], aoa_rad: float = 0.0, altitude_m: float = 0.0,
                         mach_componentes: float | None = None) -> dict:
    """Modelo aerodinâmico do OpenRocket (Barrowman estendido) em cada Mach: CNα, CP, margem estática e CD
    (atrito, pressão, base) do foguete e de cada conjunto de aletas; com mach_componentes, a contribuição
    de todos os componentes nesse Mach. Atmosfera padrão (ISA) na altitude dada: o CD de atrito depende
    do Reynolds. Com AoA = 0, 'cna' é a derivada linear; com AoA > 0 entra a sustentação do corpo
    (não linear) e 'cna' vira CN/α efetivo."""
    from info.openrocket.core.aerodynamics import BarrowmanCalculator, FlightConditions
    from info.openrocket.core.logging import WarningSet
    from info.openrocket.core.masscalc import MassCalculator
    from info.openrocket.core.models.atmosphere import ExtendedISAModel

    conf = doc.getRocket().getFlightConfiguration(sim.getFlightConfigurationId())
    lanc, queima = MassCalculator.calculateLaunch(conf), MassCalculator.calculateBurnout(conf)
    cg, cgq = float(lanc.getCM().x), float(queima.getCM().x)
    caminhos = {str(c.getID()): p for p, c in componentes(doc)}
    ids_aletas = {str(c.getID()) for _, c in conjuntos_aletas(doc)}
    atmosfera = ExtendedISAModel().getConditions(float(altitude_m))
    calc, avisos = BarrowmanCalculator(), WarningSet()

    def condicoes(mach):
        cond = FlightConditions(conf)
        cond.setAtmosphericConditions(atmosfera)
        cond.setMach(float(mach))
        cond.setAOA(float(aoa_rad))
        return cond

    curvas = []
    for mach in machs:
        cond = condicoes(mach)
        mapa = calc.getForceAnalysis(conf, cond, avisos)
        ref = float(cond.getRefLength())
        total = _forcas(calc.getAerodynamicForces(conf, cond, avisos))
        cp = total["cp_mm"] / 1000 if total["cp_mm"] is not None else None
        linha = {"mach": mach, "velocidade_ms": round(float(cond.getVelocity()), 1), **total,
                 "margem_estatica_cal": round((cp - cg) / ref, 3) if cp is not None and ref > 0 else None,
                 "margem_estatica_burnout_cal": round((cp - cgq) / ref, 3) if cp is not None and ref > 0 else None,
                 "aletas": {}}
        for e in mapa.entrySet():
            if str(e.getKey().getID()) in ids_aletas:
                f = _forcas(e.getValue())
                f["fracao_cna"] = round(f["cna"] / total["cna"], 4) if total["cna"] else None
                f["fracao_cd"] = round(f["cd"] / total["cd"], 4) if total["cd"] else None
                linha["aletas"][caminhos[str(e.getKey().getID())]] = f
        curvas.append(linha)
    out = {"diametro_referencia_mm": round(float(condicoes(0.3).getRefLength()) * 1000, 1),
           "area_referencia_mm2": round(float(condicoes(0.3).getRefArea()) * 1e6, 1),
           "cg_mm": round(cg * 1000, 1), "cg_burnout_mm": round(cgq * 1000, 1),
           "aoa_deg": round(math.degrees(aoa_rad), 3), "altitude_m": altitude_m, "curvas": curvas}
    if mach_componentes is not None:
        mapa = calc.getForceAnalysis(conf, condicoes(mach_componentes), avisos)
        comps = []
        for e in mapa.entrySet():
            c = e.getKey()
            if isinstance(c, _rc("ComponentAssembly")) or str(c.getID()) not in caminhos:
                continue  # foguete, estágios e boosters: somas, já no total
            f = _forcas(e.getValue())
            if any(f[k] for k in ("cna", "cd")):
                comps.append({"caminho": caminhos[str(c.getID())], "tipo": str(c.getClass().getSimpleName())} | f)
        out["componentes"] = {"mach": mach_componentes, "lista": sorted(comps, key=lambda x: -abs(x["cna"]))}
    out["avisos"] = sorted({str(w) for w in avisos})
    return out


# ------------------------------------------------------------------------------------- flutter (laminado)

def geometria_flutter(caminho: str, c) -> dict:
    """cr, ct, s e enflechamento do bordo de ataque (SI e graus) para fin_flutter.run_case."""
    if not isinstance(c, _rc("TrapezoidFinSet")):
        raise ToolError(f"'{caminho}' é {c.getClass().getSimpleName()}: o cálculo de flutter (placa trapezoidal) "
                        "só aceita aletas trapezoidais")
    return {"cr": float(c.getRootChord()), "ct": float(c.getTipChord()), "s": float(c.getHeight()),
            "sweep_le_deg": math.degrees(float(c.getSweepAngle()))}


def aplicar_laminado(caminho: str, c, nome: str, h: float, rho_eq: float) -> list[str]:
    """docs/FLUTTER.md §13 passo 2: espessura = h do laminado e material 'Laminado <nome>' com densidade
    rho_eq, para o OpenRocket recalcular massa, CG e arrasto. Devolve os avisos."""
    c.setThickness(float(h))
    c.setMaterial(_cls("material.Material").newMaterial(_cls("material.Material$Type").BULK, f"Laminado {nome}",
                                                         float(rho_eq), True))
    avisos = []
    secao = str(c.getCrossSection().name())
    if secao != "SQUARE":
        # medido no Hemera: AIRFOIL tem ~85% do volume da placa
        avisos.append(f"'{caminho}': seção {secao} no OpenRocket tem menos volume que a placa de espessura "
                      "uniforme do modelo de flutter, então a massa calculada fica menor que m_A·área")
    return avisos
