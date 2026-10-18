"""Ponte com o OpenRocket 24.12 via JPype. Uma JVM por processo; todo acesso serializado por LOCK."""
from __future__ import annotations

import logging
import math
import threading
from pathlib import Path

import jpype
import jpype.imports
from mcp.server.mcpserver.exceptions import ToolError  # mensagem chega ao LLM (outras exceções viram erro genérico)

RAIZ = Path(__file__).resolve().parents[2]
JAR = RAIZ / "vendor" / "OpenRocket-24.12.jar"
LOGBACK = RAIZ / "config" / "logback-mcp.xml"
log = logging.getLogger("rocket-mcp")

# O SDK MCP roda tools síncronas em threads de um pool (anyio.to_thread).
# O OpenRocket tem estado global (Application.injector) -> serializar tudo.
LOCK = threading.RLock()
_pronto = threading.Event()
_erro_inicio: BaseException | None = None


def iniciar_em_segundo_plano(dir_motores: Path) -> None:
    """Sobe a JVM numa thread para o handshake MCP (initialize) responder na hora.
    As tools do openMotor funcionam imediatamente; as do OpenRocket esperam em garantir().
    ARMADILHA (Windows): NÃO importe módulos com extensões nativas novas (ex.: matplotlib) enquanto a JVM
    sobe; isso travou o processo (loader lock). Importações tardias desse tipo devem vir depois de garantir()."""

    def alvo():
        global _erro_inicio
        try:
            iniciar(dir_motores)
        except BaseException as e:  # guardado e relançado na primeira tool que precisar
            _erro_inicio = e
        finally:
            # A thread que chama startJVM vira a "main" (não-daemon) da JVM. Se ela terminar sem se
            # desanexar, o DestroyJavaVM do atexit espera por ela para sempre e o processo não sai
            # após o EOF no stdin. O launcher do Java faz o mesmo antes do DestroyJavaVM.
            if jpype.isJVMStarted():
                jpype.java.lang.Thread.detach()
            _pronto.set()

    threading.Thread(target=alvo, name="jvm-boot", daemon=True).start()


def garantir(timeout: float = 120) -> None:
    if not _pronto.wait(timeout):
        raise ToolError("OpenRocket ainda inicializando (JVM). Tente de novo em alguns segundos.")
    if _erro_inicio is not None:
        raise ToolError(f"Falha ao iniciar o OpenRocket: {_erro_inicio!r}")


def iniciar(dir_motores: Path) -> None:
    """Sobe a JVM e o core (≈8 s). Uma JVM por processo: o JPype não reinicia a JVM."""
    if jpype.isJVMStarted():
        return
    jpype.startJVM(
        "-Djava.awt.headless=true",
        f"-Dlogback.configurationFile={LOGBACK}",  # logs no stderr: stdout é o canal JSON-RPC!
        # Avisos de simulação traduzidos e formatados pelo locale da JVM: fixa em inglês para a saída
        # ser a mesma em qualquer máquina (num Windows pt-BR saía "83,9 m/s" e tradução ruim).
        "-Duser.language=en",
        "-Duser.country=US",
        classpath=[str(JAR)],
        convertStrings=True,
    )
    from com.google.inject import Module
    from info.openrocket.core.plugin import PluginModule
    from info.openrocket.core.startup import OpenRocketCore

    # BUG da 24.12: initialize() sem argumentos não registra o PluginModule e o Guice falha com
    # "No implementation for Set<RocketSubstitutor>". Já corrigido no master (26.xx).
    OpenRocketCore.initialize(jpype.JArray(Module)([PluginModule()]))
    dir_motores.mkdir(parents=True, exist_ok=True)
    sincronizar_motores(dir_motores)


_motores_registrados: dict[str, float] = {}  # caminho do .eng -> mtime já registrado


def registrar_motor(eng: Path):
    """Carrega um .eng e o adiciona ao banco de motores. Sem isso, um .ork que referencia o motor
    perde o motor ao ser recarregado ('No motor with designation ... found')."""
    if threading.current_thread().name != "jvm-boot":
        garantir()
    from info.openrocket.core.file.motor import GeneralMotorLoader
    from info.openrocket.core.startup import Application
    from java.io import FileInputStream

    with LOCK:
        stream = FileInputStream(str(eng))
        try:
            motor = GeneralMotorLoader().load(stream, eng.name).get(0).build()
        finally:
            stream.close()
        Application.getMotorSetDatabase().addMotor(motor)
        _motores_registrados[str(eng.resolve())] = eng.stat().st_mtime
        return motor


def sincronizar_motores(dir_motores: Path) -> None:
    """Registra os .eng novos ou alterados da pasta. Chamado na inicialização e antes de cada carga de
    .ork: um .eng copiado para motores/ com o servidor no ar passa a valer sem reiniciar."""
    if not dir_motores.is_dir():
        return
    for eng in sorted(dir_motores.glob("*.eng")):
        if _motores_registrados.get(str(eng.resolve())) == eng.stat().st_mtime:
            continue
        try:
            registrar_motor(eng)
        except Exception as e:  # um .eng quebrado não pode derrubar a inicialização do OpenRocket
            log.warning("Motor ignorado (%s): %r", eng.name, e)


def carregar_ork(path: str | Path):
    garantir()
    from info.openrocket.core.file import GeneralRocketLoader
    from java.io import File

    with LOCK:
        loader = GeneralRocketLoader(File(str(path)))
        doc = loader.load()
        return doc, [str(w) for w in loader.getWarnings()]


def salvar_ork(doc, path: str | Path) -> None:
    from info.openrocket.core.file import GeneralRocketSaver
    from java.io import File

    with LOCK:
        GeneralRocketSaver().save(File(str(path)), doc)


def montagens(doc, sim):
    """Componentes que estão ativos como montagem de motor."""
    from info.openrocket.core.rocketcomponent import MotorMount

    fcid = sim.getFlightConfigurationId()
    out = []
    for c in doc.getRocket():
        if isinstance(c, MotorMount) and c.isMotorMount():
            out.append({
                "componente": c,
                "nome": str(c.getName()),
                "diametro_interno_mm": round(float(c.getMotorMountDiameter()) * 1000, 2),
                "comprimento_mm": round(float(c.getLength()) * 1000, 1),
                "motor_atual": str(c.getMotorConfig(fcid).toMotorName()),
            })
    return out


def trocar_motor(doc, sim, motor, atraso_s: float, nome_montagem: str | None = None) -> list[str]:
    """Coloca `motor` na(s) montagem(ns) da configuração de voo da simulação.
    Retorna avisos de validação: o OpenRocket NÃO checa se o motor cabe na montagem."""
    from info.openrocket.core.motor import MotorConfiguration

    avisos = []
    with LOCK:
        fcid = sim.getFlightConfigurationId()
        alvos = [m for m in montagens(doc, sim) if nome_montagem in (None, m["nome"])]
        if not alvos:
            raise ToolError(f"Montagem '{nome_montagem}' não encontrada")
        d_motor = float(motor.getDiameter()) * 1000
        l_motor = float(motor.getLength()) * 1000
        for m in alvos:
            if m["comprimento_mm"] == 0:
                # Montagem "fantasma" (ex.: Ghost end Sustainer do Hemera): só marca onde o motor fica.
                avisos.append(f"Montagem '{m['nome']}' tem comprimento 0 (componente fantasma): "
                              "encaixe do motor não verificado")
            elif d_motor > m["diametro_interno_mm"] + 0.1:
                avisos.append(f"Motor de {d_motor:.1f} mm NÃO cabe na montagem '{m['nome']}' "
                              f"({m['diametro_interno_mm']:.1f} mm)")
            if 0 < m["comprimento_mm"] < l_motor:
                avisos.append(f"Motor ({l_motor:.0f} mm) mais longo que a montagem '{m['nome']}' "
                              f"({m['comprimento_mm']:.0f} mm): haverá overhang")
            mc = MotorConfiguration(m["componente"], fcid)
            mc.setMotor(motor)
            mc.setEjectionDelay(float(atraso_s))
            m["componente"].setMotorConfig(mc, fcid)
    return avisos


# Opções de voo aceitas por configurar() (os nomes são os argumentos de simular_voo e as chaves de opcoes_voo)
OPCOES_VOO = ("trilho_m", "angulo_trilho_graus", "direcao_trilho_graus", "lancar_contra_vento", "vento_medio_ms",
              "vento_direcao_graus", "turbulencia_pct", "perfil_vento", "perfil_vento_referencia",
              "altitude_lancamento_m", "latitude_graus", "longitude_graus", "temperatura_c", "pressao_hpa", "semente")


def _nivel_vento(n, i: int) -> tuple[float, float, float, float | None]:
    """[altitude_m, velocidade_ms, direcao_graus(, turbulencia_pct)] ou o dict equivalente."""
    onde = f"perfil_vento[{i}]"
    if isinstance(n, dict):
        desconhecidas = set(n) - {"altitude_m", "velocidade_ms", "direcao_graus", "turbulencia_pct"}
        if desconhecidas:
            raise ToolError(f"{onde}: chave(s) desconhecida(s) {sorted(desconhecidas)}")
        n = [n.get("altitude_m"), n.get("velocidade_ms"), n.get("direcao_graus"), n.get("turbulencia_pct")]
    if not isinstance(n, (list, tuple)) or len(n) not in (3, 4):
        raise ToolError(f"{onde}: use [altitude_m, velocidade_ms, direcao_graus] (+ turbulencia_pct opcional)")
    n = list(n) + [None] * (4 - len(n))
    for valor, nome in zip(n[:3], ("altitude_m", "velocidade_ms", "direcao_graus")):
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise ToolError(f"{onde}.{nome}: esperado um número, recebido {valor!r}")
    if n[3] is not None and (isinstance(n[3], bool) or not isinstance(n[3], (int, float)) or n[3] < 0):
        raise ToolError(f"{onde}.turbulencia_pct: esperado um número >= 0, recebido {n[3]!r}")
    if n[1] < 0:
        raise ToolError(f"{onde}.velocidade_ms: não pode ser negativa")
    return float(n[0]), float(n[1]), float(n[2]), None if n[3] is None else float(n[3])


def _aplicar_perfil(o, perfil, referencia, turbulencia_pct) -> None:
    from info.openrocket.core.models.wind import WindModel, WindModelType

    if not isinstance(perfil, list) or not 1 <= len(perfil) <= 50:
        raise ToolError("perfil_vento: informe de 1 a 50 níveis")
    niveis = sorted(_nivel_vento(n, i) for i, n in enumerate(perfil))
    if len({n[0] for n in niveis}) != len(niveis):
        raise ToolError("perfil_vento: altitudes repetidas")
    ref = str(referencia or "AGL").upper()
    if ref not in ("AGL", "MSL"):
        raise ToolError("perfil_vento_referencia: use 'AGL' (acima do solo) ou 'MSL' (acima do nível do mar)")
    m = o.getMultiLevelWindModel()
    m.clearLevels()
    for alt, vel, dire, turb in niveis:
        turb = turb if turb is not None else turbulencia_pct
        if turb is None:
            m.addWindLevel(alt, vel, math.radians(dire))  # turbulência padrão do OpenRocket
        else:  # o 4º argumento é o desvio-padrão em m/s
            m.addWindLevel(alt, vel, math.radians(dire), jpype.java.lang.Double(turb / 100 * vel))
    m.setAltitudeReference(getattr(WindModel.AltitudeReference, ref))
    o.setWindModelType(WindModelType.MULTI_LEVEL)


def configurar(sim, *, trilho_m=None, angulo_trilho_graus=None, direcao_trilho_graus=None, lancar_contra_vento=None,
               vento_medio_ms=None, vento_direcao_graus=None, turbulencia_pct=None, perfil_vento=None,
               perfil_vento_referencia=None, altitude_lancamento_m=None, latitude_graus=None, longitude_graus=None,
               temperatura_c=None, pressao_hpa=None, semente=None) -> list[str]:
    """Ajusta as opções da simulação. Unidades do OpenRocket: SI e RADIANOS. Devolve notas para o LLM.
    ARMADILHA: com 'Launch into wind' ligado (padrão nos .ork testados, inclusive o Hemera), o OpenRocket
    aponta o trilho contra o vento e IGNORA a direção do trilho. Informar direcao_trilho_graus desliga isso.
    ARMADILHA: os getters E setters de vento das SimulationOptions (getWindDirection, getWindSpeedAverage...)
    trocam o modelo para o médio. Para ler sem efeito colateral, use o.getAverageWindModel()."""
    from info.openrocket.core.models.wind import WindModelType

    notas = []
    o = sim.getOptions()
    if trilho_m is not None:
        o.setLaunchRodLength(float(trilho_m))
    if angulo_trilho_graus is not None:
        o.setLaunchRodAngle(math.radians(angulo_trilho_graus))
    if direcao_trilho_graus is not None:
        o.setLaunchRodDirection(math.radians(direcao_trilho_graus))
        if lancar_contra_vento is None and o.getLaunchIntoWind():
            lancar_contra_vento = False
            notas.append("'Lançar contra o vento' desligado para valer a direção do trilho informada")
    if lancar_contra_vento is not None:
        o.setLaunchIntoWind(bool(lancar_contra_vento))
    if perfil_vento is not None:
        if vento_medio_ms is not None or vento_direcao_graus is not None:
            raise ToolError("Use perfil_vento OU vento_medio_ms/vento_direcao_graus, não os dois. "
                            "turbulencia_pct, se informada, vale para os níveis sem turbulência própria.")
        _aplicar_perfil(o, perfil_vento, perfil_vento_referencia, turbulencia_pct)
    elif perfil_vento_referencia is not None:
        raise ToolError("perfil_vento_referencia só vale junto com perfil_vento")
    elif any(v is not None for v in (vento_medio_ms, vento_direcao_graus, turbulencia_pct)):
        if o.getWindModelType() != WindModelType.AVERAGE:
            # Os setters abaixo só valem no modelo de vento médio; no multinível seriam ignorados.
            o.setWindModelType(WindModelType.AVERAGE)
            notas.append("Modelo de vento trocado de multinível para médio para aplicar o vento informado")
        if vento_medio_ms is not None:
            o.setWindSpeedAverage(float(vento_medio_ms))
        if vento_direcao_graus is not None:
            o.setWindDirection(math.radians(vento_direcao_graus))
        if turbulencia_pct is not None:
            o.setWindTurbulenceIntensity(float(turbulencia_pct) / 100)
    if altitude_lancamento_m is not None:
        o.setLaunchAltitude(float(altitude_lancamento_m))
    if latitude_graus is not None:
        o.setLaunchLatitude(float(latitude_graus))
    if longitude_graus is not None:
        o.setLaunchLongitude(float(longitude_graus))
    if temperatura_c is not None or pressao_hpa is not None:
        o.setISAAtmosphere(False)
        if temperatura_c is not None:
            o.setLaunchTemperature(float(temperatura_c) + 273.15)
        if pressao_hpa is not None:
            o.setLaunchPressure(float(pressao_hpa) * 100)
    if semente is not None:
        _fixar_semente(o, int(semente))
    return notas


def condicoes_lancamento(sim) -> dict:
    """Condições efetivas da simulação (o que veio do .ork + o que foi alterado)."""
    from info.openrocket.core.models.wind import WindModelType

    o = sim.getOptions()
    tipo = o.getWindModelType()  # ler ANTES de qualquer getter de vento das options (ver configurar)
    out = {
        "trilho_m": round(float(o.getLaunchRodLength()), 2),
        "angulo_trilho_graus": round(math.degrees(float(o.getLaunchRodAngle())), 1),
        "lancar_contra_vento": bool(o.getLaunchIntoWind()),
        # getLaunchRodDirection() já resolve o 'contra o vento': direção do vento na altitude de lançamento
        "direcao_trilho_efetiva_graus": round(math.degrees(float(o.getLaunchRodDirection())) % 360, 1),
        "modelo_vento": str(tipo.name()),
        "altitude_lancamento_m": round(float(o.getLaunchAltitude()), 1),
        "atmosfera_isa": bool(o.isISAAtmosphere()),
    }
    if tipo == WindModelType.AVERAGE:
        m = o.getAverageWindModel()  # getters do modelo, sem o efeito colateral dos das options
        out |= {"vento_medio_ms": round(float(m.getAverage()), 2),
                "vento_direcao_graus": round(math.degrees(float(m.getDirection())) % 360, 1),
                "turbulencia_pct": round(float(m.getTurbulenceIntensity()) * 100, 1)}
    else:
        m = o.getMultiLevelWindModel()
        out["perfil_vento_referencia"] = str(m.getAltitudeReference().name())
        out["perfil_vento"] = [{"altitude_m": round(float(lv.getAltitude()), 1),
                                "velocidade_ms": round(float(lv.getSpeed()), 2),
                                "direcao_graus": round(math.degrees(float(lv.getDirection())) % 360, 1),
                                # intensidade = desvio/velocidade: NaN num nível parado
                                "turbulencia_pct": round(float(lv.getTurbulenceIntensity()) * 100, 1)
                                if float(lv.getSpeed()) > 0 else 0.0}
                               for lv in m.getLevels()]
    if tipo != o.getWindModelType():
        raise RuntimeError("condicoes_lancamento alterou o modelo de vento")  # proteção contra regressão
    return out


def _fixar_semente(o, semente: int) -> None:
    """Torna a simulação repetível. ARMADILHA da 24.12: setRandomSeed() NÃO alcança a turbulência do vento.
    Cada PinkNoiseWindModel sorteia a própria semente no construtor (campo final, sem setter), então duas
    simulações do mesmo arquivo diferiam ~0,2%. A semente é gravada por reflexão (permitido até o Java 25;
    o JEP 500 prevê restringir escrita em campos final em versões futuras)."""
    o.setRandomSeed(semente)
    pink = jpype.JClass("info.openrocket.core.models.wind.PinkNoiseWindModel").class_
    campo_semente, campo_ruido = pink.getDeclaredField("seed"), pink.getDeclaredField("randomSource")
    campo_semente.setAccessible(True)
    campo_ruido.setAccessible(True)
    modelos = [o.getAverageWindModel()]
    nivel = jpype.JClass("info.openrocket.core.models.wind.MultiLevelPinkNoiseWindModel$LevelWindModel").class_
    campo_modelo = nivel.getDeclaredField("model")
    campo_modelo.setAccessible(True)
    modelos += [campo_modelo.get(lv) for lv in o.getMultiLevelWindModel().getLevels()]
    for i, m in enumerate(modelos):
        campo_semente.setInt(m, semente + i)
        campo_ruido.set(m, None)  # recriado a partir da semente no próximo uso


def _dados(ramo, tipo: str) -> list[float]:
    from info.openrocket.core.simulation import FlightDataType

    return [float(x) for x in ramo.get(getattr(FlightDataType, tipo))]


def _tempo_evento(ramo, nome: str) -> float | None:
    from info.openrocket.core.simulation import FlightEvent

    ev = ramo.getFirstEvent(getattr(FlightEvent.Type, nome))
    return None if ev is None else float(ev.getTime())


def _valor_em(t: list[float], v: list[float], instante: float | None) -> float | None:
    """Primeiro valor válido (não NaN) a partir do instante."""
    if instante is None:
        return None
    return next((vi for ti, vi in zip(t, v) if ti >= instante and vi == vi), None)


_CLASSE_ARRASTO = []


def listener_arrasto(fator: float):
    """Listener que multiplica o CDaxial (o que o RK4 usa na força axial) por `fator`, preservando a
    variação com o Mach. Implementado em Python via JPype: custa ~0,1-0,2 s a mais por voo no Hemera
    (~2.400 chamadas). Com fator 1,0 o apogeu é idêntico ao sem listener."""
    if not _CLASSE_ARRASTO:
        J = jpype.JOverride

        @jpype.JImplements("info.openrocket.core.simulation.listeners.SimulationComputationListener")
        class FatorArrasto:
            def __init__(self, f):
                self.f = f

            # Retornos null/NaN = "use o cálculo padrão" (como no AbstractSimulationListener)
            @J
            def startSimulation(self, s): pass
            @J
            def endSimulation(self, s, e): pass
            @J
            def preStep(self, s): return True
            @J
            def postStep(self, s): pass
            @J
            def isSystemListener(self): return False
            @J
            def clone(self): return self  # sem estado mutável
            @J
            def preAccelerationCalculation(self, s): return None
            @J
            def postAccelerationCalculation(self, s, a): return None
            @J
            def preAtmosphericModel(self, s): return None
            @J
            def postAtmosphericModel(self, s, c): return None
            @J
            def preWindModel(self, s): return None
            @J
            def postWindModel(self, s, c): return None
            @J
            def preGravityModel(self, s): return float("nan")
            @J
            def postGravityModel(self, s, g): return float("nan")
            @J
            def preFlightConditions(self, s): return None
            @J
            def postFlightConditions(self, s, c): return None
            @J
            def preAerodynamicCalculation(self, s): return None
            @J
            def postAerodynamicCalculation(self, s, forcas):
                forcas.setCDaxial(forcas.getCDaxial() * self.f)
                return forcas
            @J
            def preMassCalculation(self, s): return None
            @J
            def postMassCalculation(self, s, r): return None
            @J
            def preSimpleThrustCalculation(self, s): return float("nan")
            @J
            def postSimpleThrustCalculation(self, s, t): return float("nan")

        _CLASSE_ARRASTO.append(FatorArrasto)
    return _CLASSE_ARRASTO[0](float(fator))


def escalar_impulso(doc, sim, fator: float) -> None:
    """Troca o motor de cada montagem ativa por uma cópia com a curva de empuxo × fator (mesma queima e
    massas). A cópia não entra no banco de motores."""
    from info.openrocket.core.motor import ThrustCurveMotor

    fcid = sim.getFlightConfigurationId()
    for m in montagens(doc, sim):
        mc = m["componente"].getMotorConfig(fcid)
        motor = mc.getMotor()
        if motor is None or not isinstance(motor, ThrustCurveMotor):
            continue
        b = ThrustCurveMotor.Builder()
        b.setManufacturer(motor.getManufacturer())
        b.setDesignation(f"{motor.getDesignation()}x{fator:.4f}")
        b.setDiameter(motor.getDiameter())
        b.setLength(motor.getLength())
        b.setTimePoints(motor.getTimePoints())
        b.setThrustPoints(jpype.JArray(jpype.JDouble)([float(x) * fator for x in motor.getThrustPoints()]))
        b.setCGPoints(motor.getCGPoints())
        b.setInitialMass(motor.getInitialMass())
        b.setStandardDelays(motor.getStandardDelays())
        mc.setMotor(b.build())


def escalar_massa(doc, sim, fator: float) -> None:
    """Massa estrutural (sem motor) × fator, mantendo o CG: sobrescreve a massa e o CG do estágio
    incluindo os filhos. Só para foguete de um estágio."""
    from info.openrocket.core.masscalc import MassCalculator

    rocket = doc.getRocket()
    if rocket.getChildCount() != 1:
        raise ToolError("A incerteza de massa ainda não suporta foguetes com mais de um estágio")
    estagio = rocket.getChild(0)
    estrutura = MassCalculator.calculateStructure(rocket.getFlightConfiguration(sim.getFlightConfigurationId()))
    estagio.setOverrideMass(float(estrutura.getMass()) * fator)
    estagio.setMassOverridden(True)
    estagio.setSubcomponentsOverriddenMass(True)
    estagio.setOverrideCGX(float(estrutura.getCM().x) - float(estagio.getAxialFront()))
    estagio.setCGOverridden(True)
    estagio.setSubcomponentsOverriddenCG(True)


def simular(sim, listeners: tuple = ()) -> dict:
    with LOCK:
        sim.simulate(*listeners)
        d = sim.getSimulatedData()
        out = {
            "apogeu_m": round(float(d.getMaxAltitude()), 1),
            "velocidade_max_ms": round(float(d.getMaxVelocity()), 1),
            "mach_max": round(float(d.getMaxMachNumber()), 3),
            "aceleracao_max_ms2": round(float(d.getMaxAcceleration()), 1),
            "tempo_ate_apogeu_s": round(float(d.getTimeToApogee()), 2),
            "velocidade_saida_trilho_ms": round(float(d.getLaunchRodVelocity()), 2),
            "velocidade_abertura_recuperacao_ms": round(float(d.getDeploymentVelocity()), 2),
            "atraso_otimo_s": round(float(d.getOptimumDelay()), 2),
            "velocidade_impacto_ms": round(float(d.getGroundHitVelocity()), 2),
            "tempo_voo_s": round(float(d.getFlightTime()), 1),
            "avisos": [str(w) for w in d.getWarningSet()],
        }
        return out | _analisar_ramo(d.getBranch(0))


def _analisar_ramo(ramo) -> dict:
    """Estabilidade a partir dos dados de voo. Ela é NaN no trilho; o mínimo só considera a subida
    (saída do trilho -> apogeu): na descida o valor não tem significado físico (no Hemera chega a -9)."""
    t, est = _dados(ramo, "TYPE_TIME"), _dados(ramo, "TYPE_STABILITY")
    t_trilho, t_apogeu = _tempo_evento(ramo, "LAUNCHROD"), _tempo_evento(ramo, "APOGEE")
    subida = [(ti, e) for ti, e in zip(t, est)
              if t_trilho is not None and t_apogeu is not None and t_trilho <= ti <= t_apogeu and e == e]
    t_min, e_min = min(subida, key=lambda p: p[1]) if subida else (None, None)
    saida = _valor_em(t, est, t_trilho)

    def r(x, n):
        return None if x is None else round(x, n)

    # Ponto de pouso: posição horizontal no fim do voo (X = leste, Y = norte, em relação à base)
    leste, norte = _dados(ramo, "TYPE_POSITION_X"), _dados(ramo, "TYPE_POSITION_Y")
    pouso = None
    if leste and norte:
        x, y = leste[-1], norte[-1]
        pouso = {"distancia_m": r(math.hypot(x, y), 1), "rumo_graus": round(math.degrees(math.atan2(x, y))) % 360,
                 "leste_m": r(x, 1), "norte_m": r(y, 1)}
    return {"estabilidade_voo": {"saida_trilho_cal": r(saida, 2), "min_subida_cal": r(e_min, 2),
                                 "t_min_subida_s": r(t_min, 2)},
            "pouso": pouso}


def alertas_projeto(res: dict, atraso_ejecao_s: float | None = None) -> list[str]:
    """Validações de projeto que o OpenRocket não faz (roadmap item 9)."""
    a = []
    v = res.get("velocidade_saida_trilho_ms")
    if v is not None and v < 15:
        a.append(f"Velocidade de saída do trilho baixa ({v} m/s < 15 m/s): trilho mais longo ou mais empuxo inicial")
    est = res.get("estabilidade_voo", {})
    if est.get("saida_trilho_cal") is not None and est["saida_trilho_cal"] < 1:
        a.append(f"Estabilidade na saída do trilho {est['saida_trilho_cal']} cal (< 1 cal)")
    if est.get("min_subida_cal") is not None and est["min_subida_cal"] < 1:
        a.append(f"Estabilidade mínima na subida {est['min_subida_cal']} cal em t={est['t_min_subida_s']} s (< 1 cal)")
    margem = res.get("estabilidade", {}).get("margem_estatica_cal")
    if margem is not None and margem < 1:
        a.append(f"Margem estática {margem} cal (< 1 cal): risco de instabilidade")
    elif margem is not None and margem > 3:
        a.append(f"Margem estática {margem} cal (> 3 cal): sobre-estável, sensível a vento (weathercocking)")
    if res.get("mach_max", 0) > 0.8:
        a.append(f"Mach máximo {res['mach_max']} (> 0,8): regime transônico, arrasto e estabilidade menos precisos")
    seg = res.get("seguranca", {})
    if seg.get("empuxo_peso_medio") is not None and seg["empuxo_peso_medio"] < 5:
        a.append(f"Empuxo/peso médio {seg['empuxo_peso_medio']} (< 5): decolagem lenta, sensível a vento")
    for f in seg.get("flutter") or []:
        if isinstance(f, dict) and f.get("fator_min") is not None and f["fator_min"] < 1.5:
            a.append(f"Flutter em '{f['aletas']}': velocidade de flutter só {f['fator_min']}× a de voo em "
                     f"t={f['t_s']} s (recomendado ≥ 1,5×)")
    if seg.get("descida"):
        final = seg["descida"][-1]
        if final["velocidade_ms"] > 9:
            a.append(f"Descida final a {final['velocidade_ms']} m/s após '{final['apos_abertura']}' (> 9 m/s, "
                     "limite comum em competições)")
    otimo = res.get("atraso_otimo_s")
    if atraso_ejecao_s is not None and otimo and abs(atraso_ejecao_s - otimo) > 1.5:
        quando = "antes" if atraso_ejecao_s < otimo else "depois"
        a.append(f"Atraso de ejeção {atraso_ejecao_s} s abre a recuperação ~{abs(atraso_ejecao_s - otimo):.1f} s "
                 f"{quando} do apogeu (ótimo {otimo} s), se ela usa a carga do motor")
    return a


# Módulo de cisalhamento (GPa) só de metais, com valores bem estabelecidos. Compósitos e madeira variam
# demais com laminação/umidade: o usuário informa o valor medido ou do fornecedor.
MATERIAIS_ALETA_GPA = {"aluminio": 26.0, "aco": 79.3, "titanio": 44.0}
G0 = 9.80665


def _flutter(cr, ct, h, esp, g_pa, a, p):
    """Velocidade de flutter (m/s) de uma aleta trapezoidal: NACA TN-4197 (Martin, 1958), forma usual
    Vf = a·√(G / (39,3·AR³/((t/c)³(AR+2)) · (λ+1)/2 · p/p0)), com G em psi e p/p0 adimensional.
    ATENÇÃO: a versão publicada no Peak of Flight #291 (Apogee) dá Vf √2 MAIOR. A constante 1,337 =
    39,3/(2·14,696) já absorve o ½ de (λ+1)/2, e a expressão ainda divide por 2·(AR+2). Usamos a da NACA,
    mais conservadora. Ver tests/test_seguranca.py."""
    area = (cr + ct) / 2 * h
    ar = h * h / area  # alongamento
    lam = ct / cr
    tc = esp / cr
    g_psi = g_pa / 6894.757
    den = 39.3 * ar ** 3 / (tc ** 3 * (ar + 2)) * (lam + 1) / 2 * (p / 101325.0)
    return a * math.sqrt(g_psi / den)


def seguranca(doc, sim, g_aletas_pa: float | None = None) -> dict:
    """Números de segurança/estrutura a partir do voo já simulado (ramo principal)."""
    from info.openrocket.core.rocketcomponent import TrapezoidFinSet
    from info.openrocket.core.simulation import FlightEvent

    with LOCK:
        ramo = sim.getSimulatedData().getBranch(0)
        t = _dados(ramo, "TYPE_TIME")
        v, vz = _dados(ramo, "TYPE_VELOCITY_TOTAL"), _dados(ramo, "TYPE_VELOCITY_Z")
        rho, p, a = _dados(ramo, "TYPE_AIR_DENSITY"), _dados(ramo, "TYPE_AIR_PRESSURE"), _dados(ramo, "TYPE_SPEED_OF_SOUND")
        empuxo, massa, acel = _dados(ramo, "TYPE_THRUST_FORCE"), _dados(ramo, "TYPE_MASS"), _dados(ramo, "TYPE_ACCELERATION_TOTAL")
        t_trilho, t_apogeu = _tempo_evento(ramo, "LAUNCHROD"), _tempo_evento(ramo, "APOGEE")
        t_queima, t_solo = _tempo_evento(ramo, "BURNOUT"), _tempo_evento(ramo, "GROUND_HIT")
        aberturas = [(float(e.getTime()), str(e.getSource().getName()).strip() if e.getSource() else "?")
                     for e in ramo.getEvents() if e.getType() == FlightEvent.Type.RECOVERY_DEVICE_DEPLOYMENT]
        aletas = [(str(c.getName()).strip(), float(c.getRootChord()), float(c.getTipChord()), float(c.getHeight()),
                   float(c.getThickness())) for c in doc.getRocket() if isinstance(c, TrapezoidFinSet)]
        v_impacto = float(sim.getSimulatedData().getGroundHitVelocity())

    def r(x, n):
        return None if x is None or x != x else round(x, n)

    n = len(t)
    t_apo = t_apogeu if t_apogeu is not None else t[-1]
    subida = [i for i in range(n) if t[i] <= t_apo]
    q = [0.5 * rho[i] * v[i] ** 2 for i in range(n)]
    iq = max(subida, key=q.__getitem__)
    out = {"q_max_kpa": r(q[iq] / 1000, 1), "t_q_max_s": r(t[iq], 2), "mach_q_max": r(v[iq] / a[iq], 3),
           "aceleracao_max_subida_g": r(max(acel[i] for i in subida) / G0, 1)}
    recup = [i for i in range(n) if t[i] > t_apo and acel[i] == acel[i]]
    if recup:
        ir = max(recup, key=acel.__getitem__)
        out |= {"aceleracao_max_recuperacao_g": r(acel[ir] / G0, 1), "t_aceleracao_max_recuperacao_s": r(t[ir], 2)}
    # empuxo/peso: médio na queima e na saída do trilho
    if t_queima:
        queima = [i for i in range(n) if t[i] <= t_queima]
        out["empuxo_peso_medio"] = r(sum(empuxo[i] for i in queima) / len(queima) / (massa[0] * G0), 1)
    if t_trilho is not None:
        it = next(i for i in range(n) if t[i] >= t_trilho)
        out["empuxo_peso_saida_trilho"] = r(empuxo[it] / (massa[it] * G0), 1)
    # descida: velocidade "terminal" de cada fase = média de |vz| no último quarto do intervalo
    fins_fase = [ab[0] for ab in aberturas[1:]] + [t_solo if t_solo is not None else t[-1]]
    descida = []
    for (t0, nome), t1 in zip(aberturas, fins_fase):
        janela = [abs(vz[i]) for i in range(n) if t0 + 0.75 * (t1 - t0) <= t[i] <= t1]
        if janela:
            descida.append({"apos_abertura": nome, "t_s": r(t0, 1), "velocidade_ms": r(sum(janela) / len(janela), 2)})
    out["descida"] = descida
    out["velocidade_impacto_ms"] = r(v_impacto, 2)
    out["energia_impacto_j"] = r(0.5 * massa[-1] * v_impacto ** 2, 0)
    # flutter: menor razão Vflutter/V na subida, para cada conjunto de aletas trapezoidais
    if g_aletas_pa is None:
        out["flutter"] = {"nota": "informe material_aletas (aluminio, aco, titanio) ou modulo_cisalhamento_aletas_gpa"}
    else:
        out["flutter"] = []
        for nome, cr, ct, h, esp in aletas:
            pares = [(_flutter(cr, ct, h, esp, g_aletas_pa, a[i], p[i]) / v[i], i) for i in subida if v[i] > 1]
            fator, i = min(pares)
            out["flutter"].append({"aletas": nome, "fator_min": r(fator, 2), "t_s": r(t[i], 2),
                                   "velocidade_flutter_ms": r(fator * v[i], 1), "velocidade_ms": r(v[i], 1)})
    return out


# Séries exportadas por exportar_series_voo: (coluna, FlightDataType, fator)
SERIES_VOO = (
    ("time_s", "TYPE_TIME", 1), ("altitude_m", "TYPE_ALTITUDE", 1), ("velocity_z_ms", "TYPE_VELOCITY_Z", 1),
    ("velocity_total_ms", "TYPE_VELOCITY_TOTAL", 1), ("acceleration_total_ms2", "TYPE_ACCELERATION_TOTAL", 1),
    ("mach", "TYPE_MACH_NUMBER", 1), ("thrust_N", "TYPE_THRUST_FORCE", 1), ("drag_N", "TYPE_DRAG_FORCE", 1),
    ("drag_coeff", "TYPE_DRAG_COEFF", 1), ("mass_kg", "TYPE_MASS", 1), ("stability_cal", "TYPE_STABILITY", 1),
    ("cg_mm", "TYPE_CG_LOCATION", 1000), ("cp_mm", "TYPE_CP_LOCATION", 1000), ("aoa_deg", "TYPE_AOA", 180 / math.pi),
    ("position_east_m", "TYPE_POSITION_X", 1), ("position_north_m", "TYPE_POSITION_Y", 1),
    ("wind_ms", "TYPE_WIND_VELOCITY", 1),
)


def series_voo(sim) -> tuple[list[str], list[list[float]], list[dict]]:
    """Colunas, linhas e eventos do voo já simulado (ramo principal)."""
    with LOCK:
        ramo = sim.getSimulatedData().getBranch(0)
        colunas = [c for c, _, _ in SERIES_VOO]
        dados = [[x * f for x in _dados(ramo, tipo)] for _, tipo, f in SERIES_VOO]
        eventos = [{"evento": str(e.getType().name()), "t_s": round(float(e.getTime()), 3)} for e in ramo.getEvents()]
    return colunas, [list(linha) for linha in zip(*dados)], eventos
