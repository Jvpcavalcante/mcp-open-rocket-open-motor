"""Ponte com o openMotor: usa apenas motorlib (sem PyQt)."""
from __future__ import annotations

import copy
import math
import re
import sys
from pathlib import Path

import yaml
from mcp.server.mcpserver.exceptions import ToolError  # mensagem chega ao LLM

OPENMOTOR_DIR = Path(__file__).resolve().parents[2] / "vendor" / "openMotor"
sys.path.insert(0, str(OPENMOTOR_DIR))

# A extensão Cython do openMotor exige compilador C. Se ela não estiver compilada,
# injeta o substituto em NumPy (resultado idêntico, ~1,7x mais lento em grãos FMM).
try:
    import mathlib._find_perimeter_cy  # noqa: F401
    PERIMETRO = "cython"
except ImportError:
    from . import _perimetro_numpy

    sys.modules["mathlib._find_perimeter_cy"] = _perimetro_numpy
    PERIMETRO = "numpy"

import motorlib.motor  # noqa: E402
import motorlib.nozzle  # noqa: E402
from motorlib.grains import EndBurningGrain, grainTypes  # noqa: E402
from motorlib.propellant import Propellant, PropellantTab  # noqa: E402
from motorlib.properties import BooleanProperty, EnumProperty, FloatProperty, IntProperty  # noqa: E402
from motorlib.simResult import SimAlert, SimAlertLevel, SimAlertType  # noqa: E402


def _carregar_defaults():
    """Lê uilib/defaults.py direto do arquivo: importar o pacote `uilib` puxa matplotlib e PyQt6."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("om_defaults", OPENMOTOR_DIR / "uilib" / "defaults.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_DEFAULTS = _carregar_defaults()
DEFAULT_PREFERENCES = _DEFAULTS.DEFAULT_PREFERENCES
PROPELENTES_PADRAO = _DEFAULTS.DEFAULT_PROPELLANTS  # biblioteca de propelentes (KNSB, KNDX, ...)


class _RicLoader(yaml.SafeLoader):
    """SafeLoader que só aceita as duas tags Python que aparecem nos .ric.
    NÃO usar yaml.Loader (o openMotor usa): ele instancia objetos arbitrários."""


_RicLoader.add_constructor(
    "tag:yaml.org,2002:python/tuple", lambda l, n: tuple(l.construct_sequence(n))
)
_RicLoader.add_multi_constructor(  # type: uilib.fileIO.fileTypes [3] -> [3]
    "tag:yaml.org,2002:python/object/apply:", lambda l, s, n: l.construct_sequence(n)
)

_CONFIG_DEFAULTS = DEFAULT_PREFERENCES["general"]


def _migrar(data: dict) -> dict:
    """Migração MÍNIMA: preenche chaves de config ausentes com os defaults.
    TODO: portar as migrações completas de uilib/fileIO.py (ex.: v0.3 -> tabs de propelente,
    v0.5 -> invertedFins do Finocyl)."""
    cfg = data.setdefault("config", {})
    for k, v in _CONFIG_DEFAULTS.items():
        cfg.setdefault(k, v)
    for g in data.get("grains", []):
        if g["type"] == "Finocyl":
            g["properties"].setdefault("invertedFins", False)
    return data


def carregar_ric(path: str | Path) -> motorlib.motor.Motor:
    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_RicLoader)
    if not isinstance(raw, dict) or not {"data", "type", "version"} <= raw.keys():
        raise ToolError(f"Arquivo .ric inválido (faltam data/type/version): {Path(path).name}")
    return motorlib.motor.Motor(_migrar(raw["data"]))


def salvar_ric(motor: motorlib.motor.Motor, path: str | Path) -> None:
    """Grava no formato que o openMotor 0.6.2 abre (com as tags Python que ele espera)."""
    txt = yaml.dump({"data": motor.getDict()}, sort_keys=True)
    txt += "type: !!python/object/apply:uilib.fileIO.fileTypes [3]\nversion: !!python/tuple [0, 6, 2]\n"
    Path(path).write_text(txt, encoding="utf-8")


def simular(motor, progresso=None):
    """Roda a simulação. `progresso(frac)` recebe 0..1; retornar True cancela."""
    return motor.runSimulation(callback=progresso)


def _f(x, n: int) -> float:
    """numpy -> float nativo (o JSON do MCP não serializa np.float64)."""
    return round(float(x), n)


def _alerta(a) -> dict:
    return {"nivel": a.level.name, "descricao": a.description, "local": a.location}


def resumo(motor, r) -> dict:
    sucesso = r.success and not r.getAlertsByLevel(SimAlertLevel.ERROR)
    out = {
        "sucesso": sucesso,
        "alertas": [_alerta(a) for a in r.alerts],
    }
    if sucesso:
        out |= {
            "designacao": r.getDesignation(),
            "impulso_total_Ns": _f(r.getImpulse(), 2),
            "empuxo_medio_N": _f(r.getAverageForce(), 2),
            "tempo_queima_s": _f(r.getBurnTime(), 3),
            "isp_s": _f(r.getISP(), 1),
            "pressao_max_MPa": _f(r.getMaxPressure() / 1e6, 3),
            "kn_inicial": _f(r.getInitialKN(), 1),
            "kn_pico": _f(r.getPeakKN(), 1),
            "massa_propelente_kg": _f(r.getPropellantMass(), 4),
            "fluxo_massico_pico_kg_m2s": _f(r.getPeakMassFlux(), 1),
            "diametro_grao_max_mm": _f(r.getMaxPropellantDiameter() * 1000, 2),
            "comprimento_propelente_mm": _f(r.getPropellantLength() * 1000, 2),
        }
    return out


def exportar_eng(r, path, diametro_mm, comprimento_mm, massa_hardware_kg,
                 designacao=None, fabricante="openMotor") -> str:
    """Reimplementa uilib/converters/engExporter.py (que é acoplado a um QDialog).
    Formato RASP: 'nome diam(mm) comp(mm) atrasos massa_prop(kg) massa_total(kg) fabricante'."""
    mp = r.getPropellantMass()
    nome = (designacao or r.getDesignation() + "-OM").replace(" ", "_")
    linhas = [f"{nome} {diametro_mm:g} {comprimento_mm:g} P {mp:.5f} {mp + massa_hardware_kg:.5f} {fabricante}"]
    t = r.channels["time"].getData()
    f = r.channels["force"].getData()
    for ti, fi in zip(t, f):
        linhas.append(f"{ti:.4f} {fi + (0.01 if ti == 0 else 0):.4f}")  # 1º ponto não pode ser 0
    if f[-1] != 0:  # ponto de empuxo zero após a queima (exigido por RASAero/OpenRocket)
        linhas.append(f"{r.getBurnTime() + 0.01:.4f} 0.0")
    Path(path).write_text("\n".join(linhas) + "\n;\n", encoding="utf-8")
    return nome


def series_motor(r) -> tuple[list[str], list[list[float]]]:
    """Séries da simulação para CSV. Canais por grão: massa somada; vazão acumulada em direção à tubeira
    (o último grão é a vazão total); fluxo de massa e Mach do núcleo pelo máximo entre os grãos."""
    ch = {k: c.getData() for k, c in r.channels.items()}
    colunas = ["time_s", "thrust_N", "chamber_pressure_MPa", "kn", "propellant_mass_kg", "mass_flow_kg_s",
               "mass_flux_max_kg_m2s", "core_mach_max", "exit_pressure_MPa", "throat_change_mm"]
    linhas = []
    for i, t in enumerate(ch["time"]):
        linhas.append([float(t), float(ch["force"][i]), float(ch["pressure"][i]) / 1e6, float(ch["kn"][i]),
                       float(sum(ch["mass"][i])), float(ch["massFlow"][i][-1]), float(max(ch["massFlux"][i])),
                       float(max(ch["machNumber"][i])), float(ch["exitPressure"][i]) / 1e6,
                       float(ch["dThroat"][i]) * 1000])
    return colunas, linhas


# ---------------------------------------------------------------------------------------------------
# Criação e edição de motores (listar_tipos_grao, criar_motor, ler_motor, editar_motor)
#
# Os parâmetros usam os nomes do motorlib (os mesmos do .ric) + sufixo de unidade: coreDiameter_mm,
# divAngle_deg, maxPressure_MPa... A lista sai das propriedades de cada classe, então grãos novos do
# openMotor aparecem sem manter tabela à mão.
# ARMADILHA: FloatProperty.setValue IGNORA EM SILÊNCIO valores fora da faixa (o valor anterior fica).
# Por isso a faixa é checada aqui, e o erro volta ao LLM.
# ---------------------------------------------------------------------------------------------------

# unidade do motorlib -> (sufixo no nome do parâmetro, fator: valor_SI = valor * fator)
_UNIDADES = {
    "": ("", 1.0),
    "m": ("mm", 1e-3),
    "Pa": ("MPa", 1e6),
    "deg": ("deg", 1.0),
    "s": ("s", 1.0),
    "%": ("pct", 1.0),
    "kg/(m^2*s)": ("kg_m2s", 1.0),
    "(m*Pa)/s": ("mPa_s", 1.0),
    "m/(s*Pa)": ("m_sPa", 1.0),
    "kg/m^3": ("kg_m3", 1.0),
    "K": ("K", 1.0),
    "g/mol": ("g_mol", 1.0),
    "m/(s*Pa^n)": ("SI", 1.0),  # coeficiente a de r = a·Pⁿ com P em Pa e r em m/s
}
_TIPOS_GRAO = {nome: cls for nome, cls in grainTypes.items() if nome != "Custom Grain"}  # Custom exige DXF
_TUBEIRA_OPCIONAIS = {"throatLength", "slagCoeff", "erosionCoeff"}  # 0 = sem comprimento/escória/erosão
_CAMINHO = re.compile(r"^(?:(?P<secao>nozzle|config)\.(?P<param>\w+)"
                      r"|grains\[(?P<i>\d+)\](?:\.(?P<gparam>\w+))?"
                      r"|propellant\.tabs\[(?P<ti>\d+)\]\.(?P<tparam>\w+)"
                      r"|propellant\.(?P<pparam>\w+)"
                      r"|(?P<prop>propellant))$")


def _unidade(p) -> tuple[str, float]:
    if p.unit in _UNIDADES:
        return _UNIDADES[p.unit]
    return re.sub(r"\W+", "_", p.unit).strip("_"), 1.0  # unidade desconhecida: SI, sufixo literal


def _sig(x: float) -> float:
    return float(f"{x:.6g}")


def _editaveis(colecao) -> dict[str, str]:
    """Nome externo (com sufixo de unidade) -> nome da propriedade no motorlib."""
    out = {}
    for nome, p in colecao.props.items():
        if isinstance(p, (FloatProperty, IntProperty)):
            suf = _unidade(p)[0]
            out[f"{nome}_{suf}" if suf else nome] = nome
        elif isinstance(p, (EnumProperty, BooleanProperty)):
            out[nome] = nome
    return out


def _obrigatorios_grao(grao) -> set[str]:
    # Todo número é obrigatório: o padrão do motorlib é o mínimo da faixa (0 ou negativo), não um valor útil.
    return {n for n, p in grao.props.items() if isinstance(p, (FloatProperty, IntProperty))}


def _converter(p, valor, onde: str):
    if isinstance(p, (FloatProperty, IntProperty)):
        if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor):
            raise ToolError(f"{onde}: esperado um número, recebido {valor!r}")
        if isinstance(p, IntProperty) and valor != int(valor):
            raise ToolError(f"{onde}: esperado um inteiro, recebido {valor!r}")
        suf, fator = _unidade(p)
        si = int(valor) if isinstance(p, IntProperty) else valor * fator
        tol = 1e-9 * max(abs(p.min), abs(p.max))  # arredondamento da conversão de unidade
        if not p.min - tol <= si <= p.max + tol:
            faixa = f"[{_sig(p.min / fator)}, {_sig(p.max / fator)}]" + (f" {suf}" if suf else "")
            raise ToolError(f"{onde} = {valor} fora da faixa {faixa}")
        return min(max(si, p.min), p.max)
    if isinstance(p, EnumProperty):
        if valor not in p.values:
            raise ToolError(f"{onde}: valor {valor!r} inválido. Opções: {p.values}")
        return valor
    if not isinstance(valor, bool):
        raise ToolError(f"{onde}: esperado true/false, recebido {valor!r}")
    return valor


def _aplicar(colecao, valores, onde: str, obrigatorios: set[str] = frozenset()) -> None:
    if not isinstance(valores, dict):
        raise ToolError(f"{onde}: esperado um objeto {{parâmetro: valor}}, recebido {valores!r}")
    mapa = _editaveis(colecao)
    desconhecidos = sorted(set(valores) - set(mapa))
    if desconhecidos:
        raise ToolError(f"{onde}: parâmetro(s) desconhecido(s) {desconhecidos}. Aceitos: {sorted(mapa)}")
    faltando = sorted(ext for ext, nome in mapa.items() if nome in obrigatorios and ext not in valores)
    if faltando:
        raise ToolError(f"{onde}: faltam parâmetros obrigatórios {faltando}")
    for ext, valor in valores.items():
        nome = mapa[ext]
        colecao.setProperty(nome, _converter(colecao.props[nome], valor, f"{onde}.{ext}"))


def _grao(spec, onde: str):
    if not isinstance(spec, dict):
        raise ToolError(f"{onde}: esperado um objeto com 'type' e os parâmetros do grão")
    spec = dict(spec)
    tipo = spec.pop("type", None)
    if tipo not in _TIPOS_GRAO:
        raise ToolError(f"{onde}.type inválido: {tipo!r}. Tipos aceitos: {sorted(_TIPOS_GRAO)}")
    grao = _TIPOS_GRAO[tipo]()
    for p in grao.props.values():
        if isinstance(p, BooleanProperty) and p.getValue() is None:  # o motorlib deixa None
            p.setValue(False)
    _aplicar(grao, spec, onde, _obrigatorios_grao(grao))
    return grao


def _propelente(valor) -> Propellant:
    """Nome da biblioteca (string) ou propelente próprio (objeto com name, density_kg_m3 e tabs)."""
    if isinstance(valor, dict):
        return _propelente_proprio(valor)
    if not isinstance(valor, str) or not valor.strip():
        raise ToolError("propellant: informe o nome de um propelente da biblioteca ou um objeto "
                        "{name, density_kg_m3, tabs} (ver listar_tipos_grao)")
    return _propelente_biblioteca(valor)


def _propelente_proprio(spec: dict) -> Propellant:
    spec = dict(spec)
    nome, tabs = spec.pop("name", None), spec.pop("tabs", None)
    if not isinstance(nome, str) or not nome.strip():
        raise ToolError("propellant.name: obrigatório num propelente próprio")
    if not isinstance(tabs, list) or not tabs:
        raise ToolError("propellant.tabs: informe ao menos uma faixa de pressão")
    prop = Propellant()
    prop.setProperty("name", nome.strip())
    _aplicar(prop, spec, "propellant", {"density"})
    faixas = []
    for i, t in enumerate(tabs):
        faixa = PropellantTab()
        _aplicar(faixa, t, f"propellant.tabs[{i}]", set(faixa.props))
        faixas.append(faixa)
    prop.props["tabs"].tabs = faixas
    return prop


def _propelente_biblioteca(nome: str) -> Propellant:
    """Busca: nome completo, nome sem o autor ('KNSB' -> 'Nakka - KNSB') ou trecho único."""
    alvo = nome.strip().casefold()
    nomes = [p["name"] for p in PROPELENTES_PADRAO]
    for criterio in (lambda n: n.casefold() == alvo,
                     lambda n: n.split(" - ", 1)[-1].casefold() == alvo,
                     lambda n: alvo in n.casefold()):
        achados = [i for i, n in enumerate(nomes) if criterio(n)]
        if len(achados) == 1:
            return Propellant(copy.deepcopy(PROPELENTES_PADRAO[achados[0]]))
        if achados:
            raise ToolError(f"Propelente '{nome}' ambíguo: {[nomes[i] for i in achados]}")
    raise ToolError(f"Propelente '{nome}' não encontrado. Disponíveis: {nomes}")


def _esquema_colecao(colecao, obrigatorios: set[str], padroes: dict | None = None) -> dict:
    out = {}
    for ext, nome in _editaveis(colecao).items():
        p = colecao.props[nome]
        d = {"descricao": p.dispName, "obrigatorio": nome in obrigatorios}
        if isinstance(p, (FloatProperty, IntProperty)):
            fator = _unidade(p)[1]
            d |= {"tipo": "int" if isinstance(p, IntProperty) else "float",
                  "min": _sig(p.min / fator), "max": _sig(p.max / fator)}
            if nome not in obrigatorios:
                d["padrao"] = _sig((padroes or {}).get(nome, p.getValue()) / fator)
        elif isinstance(p, EnumProperty):
            d |= {"tipo": "enum", "valores": list(p.values), "padrao": p.values[0]}
        else:
            d |= {"tipo": "bool", "padrao": False}
        out[ext] = d
    return out


def esquema() -> dict:
    """Catálogo de parâmetros para criar_motor/editar_motor."""
    tubeira = motorlib.nozzle.Nozzle()
    return {
        "unidades": "sufixo do nome: _mm, _deg, _MPa, _s, _pct; sem sufixo = adimensional",
        "grains": {tipo: _esquema_colecao(cls(), _obrigatorios_grao(cls())) for tipo, cls in _TIPOS_GRAO.items()},
        "nozzle": _esquema_colecao(tubeira, set(tubeira.props) - _TUBEIRA_OPCIONAIS),
        "config": _esquema_colecao(motorlib.motor.MotorConfig(), set(), _CONFIG_DEFAULTS),
        "propellants": [p["name"] for p in PROPELENTES_PADRAO],
        "propellant_proprio": {
            "uso": "propellant pode ser um nome da lista acima ou um objeto {name, density_kg_m3, tabs: [...]}",
            "name": {"tipo": "string", "obrigatorio": True},
            **_esquema_colecao(Propellant(), {"density"}),
            "tabs": _esquema_colecao(PropellantTab(), set(PropellantTab().props)),
        },
    }


def montar_motor(propellant, grains, nozzle, config=None) -> motorlib.motor.Motor:
    if not isinstance(grains, list) or not grains:
        raise ToolError("grains: informe uma lista com ao menos um grão")
    motor = motorlib.motor.Motor()
    motor.config.setProperties(_CONFIG_DEFAULTS)  # sem isso o limite de Mach fica 0 (armadilha 9 da §5)
    _aplicar(motor.config, config or {}, "config")
    _aplicar(motor.nozzle, nozzle, "nozzle", set(motor.nozzle.props) - _TUBEIRA_OPCIONAIS)
    motor.propellant = _propelente(propellant)
    motor.grains = [_grao(g, f"grains[{i}]") for i, g in enumerate(grains)]
    return motor


def editar(motor, alteracoes) -> None:
    """Aplica {caminho: valor} em ordem. Caminhos: 'propellant', 'nozzle.<param>', 'config.<param>',
    'grains[i].<param>' e 'grains[i]' (objeto substitui; i == nº de grãos acrescenta; null remove)."""
    if not isinstance(alteracoes, dict) or not alteracoes:
        raise ToolError("alteracoes: informe ao menos uma alteração {caminho: valor}")
    for chave, valor in alteracoes.items():
        m = _CAMINHO.match(chave.strip())
        if not m:
            raise ToolError(f"Caminho inválido: {chave!r}. Use 'propellant', 'propellant.a_factor', "
                            "'propellant.density_kg_m3', 'propellant.tabs[i].<param>', 'nozzle.<param>', "
                            "'config.<param>', 'grains[i].<param>' ou 'grains[i]'")
        if m["prop"]:
            motor.propellant = _propelente(valor)
        elif m["tparam"] or m["pparam"]:
            _editar_propelente(motor, m, valor, chave)
        elif m["secao"]:
            _aplicar(getattr(motor, m["secao"]), {m["param"]: valor}, m["secao"])
        else:
            i, n = int(m["i"]), len(motor.grains)
            if (m["gparam"] or valor is None) and i >= n:
                raise ToolError(f"{chave}: o motor tem {n} grão(s), índices 0 a {n - 1}")
            if m["gparam"]:
                _aplicar(motor.grains[i], {m["gparam"]: valor}, f"grains[{i}]")
            elif valor is None:
                del motor.grains[i]
            elif i < n:
                motor.grains[i] = _grao(valor, f"grains[{i}]")
            elif i == n:
                motor.grains.append(_grao(valor, f"grains[{i}]"))
            else:
                raise ToolError(f"{chave}: para acrescentar um grão use grains[{n}]")


def _editar_propelente(motor, m, valor, chave: str) -> None:
    """propellant.tabs[i].<param>, propellant.density_kg_m3 e propellant.a_factor (multiplica o 'a' da lei
    r = a·Pⁿ em todas as faixas: o parâmetro natural para calibrar a velocidade de queima)."""
    if motor.propellant is None:
        raise ToolError(f"{chave}: o motor não tem propelente")
    faixas = motor.propellant.props["tabs"].tabs
    if m["tparam"]:
        i = int(m["ti"])
        if i >= len(faixas):
            raise ToolError(f"{chave}: o propelente tem {len(faixas)} faixa(s), índices 0 a {len(faixas) - 1}")
        _aplicar(faixas[i], {m["tparam"]: valor}, f"propellant.tabs[{i}]")
    elif m["pparam"] == "a_factor":
        if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not valor > 0:
            raise ToolError(f"{chave}: esperado um número > 0, recebido {valor!r}")
        for i, faixa in enumerate(faixas):
            p = faixa.props["a"]
            novo = p.getValue() * valor
            if not p.min <= novo <= p.max:
                raise ToolError(f"{chave}: 'a' da faixa {i} ficaria {novo:g}, fora de [{p.min:g}, {p.max:g}]")
            p.setValue(novo)
    else:
        _aplicar(motor.propellant, {m["pparam"]: valor}, "propellant")


def alertas_geometria(motor) -> list[dict]:
    """As mesmas checagens que Motor.runSimulation() faz antes de simular (motorlib/motor.py),
    com o local no formato dos caminhos de editar_motor."""
    alertas = []
    if not motor.grains:
        alertas.append(SimAlert(SimAlertLevel.ERROR, SimAlertType.CONSTRAINT,
                                "Motor must have at least one propellant grain", "grains"))
    for gid, grao in enumerate(motor.grains):
        local = f"grains[{gid}]"
        if isinstance(grao, EndBurningGrain) and gid != 0:
            alertas.append(SimAlert(SimAlertLevel.ERROR, SimAlertType.CONSTRAINT,
                                    "End burning grains must be the forward-most grain in the motor", local))
        for a in grao.getGeometryErrors():
            a.location = local
            alertas.append(a)
    for a in motor.nozzle.getGeometryErrors():
        a.location = "nozzle"
        alertas.append(a)
    if motor.propellant is None:
        alertas.append(SimAlert(SimAlertLevel.ERROR, SimAlertType.CONSTRAINT,
                                "Motor must have a propellant set", "propellant"))
    else:
        for a in motor.propellant.getErrors():
            a.location = "propellant"
            alertas.append(a)
    return [_alerta(a) for a in alertas]


def validar(motor) -> list[dict]:
    """ToolError se houver erro de geometria; senão devolve os avisos (que não impedem gravar)."""
    alertas = alertas_geometria(motor)
    erros = [a for a in alertas if a["nivel"] == "ERROR"]
    if erros:
        raise ToolError("Motor inválido, nada foi gravado:\n"
                        + "\n".join(f"- {a['local']}: {a['descricao']}" for a in erros))
    return alertas


def previa(motor) -> dict:
    """Números sem simular (Motor.getQuickResults). Só para motor sem erros de geometria."""
    q = motor.getQuickResults()
    return {
        "kn_inicial": _f(q["initialKn"], 1),
        "razao_porta_garganta": None if q["portRatio"] is None else _f(q["portRatio"], 2),
        "razao_expansao": _f(motor.nozzle.calcExpansion(), 2),
        "carregamento_volumetrico_pct": _f(q["volumeLoading"], 1),
        "massa_propelente_kg": _f(q["propellantMass"], 4),
        "comprimento_propelente_mm": _f(q["length"] * 1000, 2),
        "diametro_grao_max_mm": _f(q["diameter"] * 1000, 2),
    }


def _valores(colecao) -> dict:
    out = {}
    for ext, nome in _editaveis(colecao).items():
        p = colecao.props[nome]
        out[ext] = _sig(p.getValue() / _unidade(p)[1]) if isinstance(p, FloatProperty) else p.getValue()
    return out


def _igual(a, b) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_igual(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_igual(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9)
    return a == b


def _descrever_propelente(prop):
    """Só o nome se for idêntico ao da biblioteca; senão o objeto completo (ida e volta exata)."""
    if prop is None:
        return None
    dados = prop.getProperties()
    if any(_igual(dados, p) for p in PROPELENTES_PADRAO):
        return dados["name"]
    return {"name": dados["name"], **_valores(prop), "tabs": [_valores(t) for t in prop.props["tabs"].tabs]}


def descrever(motor) -> dict:
    """O motor no formato de entrada de criar_motor (e com os nomes dos caminhos de editar_motor)."""
    return {
        "propellant": _descrever_propelente(motor.propellant),
        "grains": [{"type": g.geomName, **_valores(g)} for g in motor.grains],
        "nozzle": _valores(motor.nozzle),
        "config": _valores(motor.config),
    }


# ------------------------------------------------------------------------------------- calibração

def ler_caminho(motor, chave: str) -> float:
    """Valor atual de um caminho de editar_motor (nas unidades externas). propellant.a_factor = 1."""
    m = _CAMINHO.match(chave.strip())
    if not m or m["prop"]:
        raise ToolError(f"Caminho numérico inválido: {chave!r} (ex.: 'nozzle.efficiency', 'propellant.a_factor')")
    try:
        if m["tparam"]:
            return _valores(motor.propellant.props["tabs"].tabs[int(m["ti"])])[m["tparam"]]
        if m["pparam"] == "a_factor":
            return 1.0
        if m["pparam"]:
            return _valores(motor.propellant)[m["pparam"]]
        if m["secao"]:
            return _valores(getattr(motor, m["secao"]))[m["param"]]
        if m["gparam"]:
            return _valores(motor.grains[int(m["i"])])[m["gparam"]]
    except (KeyError, IndexError) as e:
        raise ToolError(f"{chave}: parâmetro ou índice inexistente ({e})") from e
    raise ToolError(f"{chave}: use um parâmetro numérico (ex.: 'grains[0].coreDiameter_mm')")


def motor_de_dict(dados: dict) -> motorlib.motor.Motor:
    return motorlib.motor.Motor(copy.deepcopy(dados))


def curva_motor(r) -> dict:
    """Curva simulada no formato de analise.comparar_curvas."""
    import numpy as np

    return {"t": np.array(r.channels["time"].getData(), float), "f": np.array(r.channels["force"].getData(), float),
            "p": np.array(r.channels["pressure"].getData(), float),
            "massa_propelente_kg": float(r.getPropellantMass()), "nome": r.getDesignation()}
