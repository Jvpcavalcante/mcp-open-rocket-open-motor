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
    As tools do openMotor funcionam imediatamente; as do OpenRocket esperam em garantir()."""

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
              "vento_direcao_graus", "turbulencia_pct", "altitude_lancamento_m", "latitude_graus", "longitude_graus",
              "temperatura_c", "pressao_hpa", "semente")


def configurar(sim, *, trilho_m=None, angulo_trilho_graus=None, direcao_trilho_graus=None, lancar_contra_vento=None,
               vento_medio_ms=None, vento_direcao_graus=None, turbulencia_pct=None, altitude_lancamento_m=None,
               latitude_graus=None, longitude_graus=None, temperatura_c=None, pressao_hpa=None,
               semente=None) -> list[str]:
    """Ajusta as opções da simulação. Unidades do OpenRocket: SI e RADIANOS. Devolve notas para o LLM.
    ARMADILHA: com 'Launch into wind' ligado (padrão nos .ork testados, inclusive o Hemera), o OpenRocket
    aponta o trilho contra o vento e IGNORA a direção do trilho. Informar direcao_trilho_graus desliga isso."""
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
    if any(v is not None for v in (vento_medio_ms, vento_direcao_graus, turbulencia_pct)):
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
    contra = bool(o.getLaunchIntoWind())
    vento_dir = math.degrees(float(o.getWindDirection())) % 360
    out = {
        "trilho_m": round(float(o.getLaunchRodLength()), 2),
        "angulo_trilho_graus": round(math.degrees(float(o.getLaunchRodAngle())), 1),
        "lancar_contra_vento": contra,
        # contra o vento, o trilho aponta para de onde o vento vem
        "direcao_trilho_efetiva_graus": round(vento_dir if contra else
                                              math.degrees(float(o.getLaunchRodDirection())) % 360, 1),
        "modelo_vento": str(o.getWindModelType().name()),
        "altitude_lancamento_m": round(float(o.getLaunchAltitude()), 1),
        "atmosfera_isa": bool(o.isISAAtmosphere()),
    }
    if o.getWindModelType() == WindModelType.AVERAGE:
        out |= {"vento_medio_ms": round(float(o.getWindSpeedAverage()), 2), "vento_direcao_graus": round(vento_dir, 1),
                "turbulencia_pct": round(float(o.getWindTurbulenceIntensity()) * 100, 1)}
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


def simular(sim) -> dict:
    with LOCK:
        sim.simulate()
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
    otimo = res.get("atraso_otimo_s")
    if atraso_ejecao_s is not None and otimo and abs(atraso_ejecao_s - otimo) > 1.5:
        quando = "antes" if atraso_ejecao_s < otimo else "depois"
        a.append(f"Atraso de ejeção {atraso_ejecao_s} s abre a recuperação ~{abs(atraso_ejecao_s - otimo):.1f} s "
                 f"{quando} do apogeu (ótimo {otimo} s), se ela usa a carga do motor")
    return a


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
