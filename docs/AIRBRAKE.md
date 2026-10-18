# Airbrake no rocket-mcp: plano de implementação

Objetivo: simular voos com airbrake (freio aerodinâmico) atuando na fase de coast e controlar isso pelo MCP:
abertura fixa, programada no tempo ou em malha fechada para atingir um apogeu-alvo.

Status: **proposta, nada implementado ainda.** As assinaturas de API citadas foram conferidas com `javap` no
`vendor/OpenRocket-24.12.jar`. Os pontos marcados com **[verificar]** são hipóteses a confirmar no primeiro protótipo.

---

## 1. Por que não usar o plugin da Waterloo Rocketry

O [or-airbrake-plugin](https://github.com/waterloo-rocketry/or-airbrake-plugin) (último commit em ago/2024)
serviu de referência de design, mas não dá para usá-lo como dependência:

| Problema | Detalhe |
|---|---|
| Versão do OpenRocket | Feito para a **23.09** (pacotes `net.sf.openrocket.*`). A 24.12 renomeou tudo para `info.openrocket.core.*`, então não carrega sem port. |
| Código nativo | O PID e a previsão de apogeu chamam C++ via JNI, vindo de um submódulo com o firmware de bordo deles (`cansw_processor_stm`), compilado com `cmake` + `make`. Isso é inviável no nosso Windows sem toolchain C. |
| Modelo de arrasto | São 11 polinômios cúbicos de CFD (`SimulatedDragForceInterpolator`), com normalização fixa (`(v-273.9)/148.7`, `(h-5000)/3172`). Eles dão a força de arrasto **do foguete inteiro deles** e substituem o `CDaxial` do OpenRocket, então não servem para outro foguete. |
| Limites fixos no código | Freio só depois de `t > 9 s` e com `vz > 34 m/s`. |
| Licença | O repositório **não tem licença**: não copiamos código, só a ideia. |

O que vale aproveitar é o **mecanismo**, que é o mesmo na 24.12:

1. uma `SimulationExtension` que, no `initialize()`, adiciona um `SimulationListener`;
2. o listener decide a abertura do freio a cada passo;
3. no `postAerodynamicCalculation`, o listener altera o `CDaxial` das forças aerodinâmicas.

---

## 2. Visão geral da arquitetura

```
┌───────────── MCP (Python) ─────────────┐        ┌──────────── JVM (JPype) ─────────────┐
│ server.py                              │        │ OpenRocket-24.12.jar                 │
│   simular_voo(..., airbrake={...})     │        │                                      │
│   projetar_e_voar(..., airbrake={...}) │        │ rocket-mcp-airbrake.jar  (novo)      │
│   ajustar_airbrake(...)        (novo)  │        │   AirbrakeProvider   @Plugin         │
│        │                               │        │   AirbrakeExtension  (config no .ork)│
│ or_bridge.py                           │        │   AirbrakeListener   (por simulação) │
│   configurar_airbrake(sim, spec) ──────┼─LOCK──▶│   ModeloArrasto, Controladores,      │
│   series_airbrake(ramo)        ◀───────┼────────│   PrevisorApogeu                     │
└────────────────────────────────────────┘        └──────────────────────────────────────┘
```

Decisões:

- **A extensão é em Java, não um listener Python via JPype.** O Java roda em velocidade nativa, a configuração é
  salva **dentro do `.ork`** (o arquivo gravado pelo MCP abre na GUI com o freio, se o plugin estiver instalado
  nela) e o `clone()` de listener que o OpenRocket exige fica simples. Um listener em Python chamaria o Python
  várias vezes por passo de RK4 e seria lento em varreduras.
- **O JAR é próprio e pequeno**, compilado com `javac` puro contra o JAR do OpenRocket, sem Gradle. O JDK 25 já está
  instalado. Compilamos com `--release 17`, o mínimo do OpenRocket 24.12, para o mesmo JAR também funcionar na GUI.
- **O arrasto do freio é incremental:** somamos ao `CDaxial` que o OpenRocket já calculou, sem substituí-lo
  (detalhes na §4). Assim o resto do modelo aerodinâmico do OpenRocket continua valendo.
- Nenhuma lógica de controle fica em Python. O Python só monta a configuração, dispara a simulação e lê as séries.

---

## 3. Módulo Java

### 3.1 Estrutura

```
java/airbrake/
  src/rocketmcp/airbrake/
    AirbrakeProvider.java      @Plugin, registra a extensão no Guice
    AirbrakeExtension.java     getters e setters sobre o Config (persistido no .ork) + initialize()
    AirbrakeListener.java      hooks da simulação
    ModeloArrasto.java         abertura e Mach -> incremento de CD·A
    Atuador.java               limite de taxa de abertura (%/s)
    PrevisorApogeu.java        apogeu previsto com arrasto quadrático
    controle/
      Controlador.java         interface: double abertura(EstadoVoo e)
      Fixo.java                abertura constante
      Programado.java          tabela tempo -> abertura (relativa ao burnout)
      Alvo.java                bisseção: a abertura constante que leva ao apogeu-alvo
      Pid.java                 PID sobre o erro de apogeu previsto, com anti-windup
scripts/compilar_airbrake.py   javac + jar -> vendor/rocket-mcp-airbrake.jar
```

O nome do pacote, `rocketmcp.airbrake`, também vira o ID da extensão: `getId()` devolve o nome da classe, e é
esse ID que fica gravado no `.ork`. **Não renomear a classe depois de existir `.ork` salvo com ela.**

### 3.2 API do OpenRocket 24.12 (conferida)

| Uso | Classe e método |
|---|---|
| Registro | `@info.openrocket.core.plugin.Plugin`. O `PluginModule` acha as classes anotadas com **ClassGraph varrendo o classpath inteiro**, então basta o JAR estar no `classpath` do `startJVM`. |
| Provider | `AbstractSimulationExtensionProvider(Class<? extends SimulationExtension>, String... caminhoNoMenu)` |
| Extensão | `AbstractSimulationExtension`: `getName()`, `initialize(SimulationConditions)`, `getFlightDataTypes()`, campo `protected Config config` |
| Config | `config.getDouble(k, padrao)`, `getBoolean`, `getString`, `put(k, Number/Boolean/String/List)` |
| Listener | `AbstractSimulationListener`, que já implementa `SimulationComputationListener` e `SimulationEventListener` |
| Hooks usados | `startSimulation`, `handleFlightEvent(status, FlightEvent)`, `preStep`, `postAerodynamicCalculation(status, AerodynamicForces)`, `postStep`, `clone()` |
| Arrasto | `AerodynamicForces.getCDaxial()` e `setCDaxial(double)`. O `RK4SimulationStepper` usa **`getCDaxial()`** para a força axial. |
| Estado | `status.getSimulationTime()`, `getRocketVelocity()`, `getRocketPosition()`, `isApogeeReached()`, `getFlightDataBranch()`, `getSimulationConditions().getLaunchSite()` |
| Condições | `FlightConditions.getMach()`, `getRefArea()`, `getAtmosphericConditions().getDensity()` (obtido em `postFlightConditions`) |
| Séries novas | `FlightDataType.getType(nome, simbolo, UnitGroup)`, gravadas com `branch.setValue(tipo, v)` |
| Eventos | `FlightEvent.Type.BURNOUT`, `APOGEE` |

### 3.3 Esboço do código (não compilado)

```java
@Plugin
public class AirbrakeProvider extends AbstractSimulationExtensionProvider {
    public AirbrakeProvider() { super(AirbrakeExtension.class, "Rocket MCP", "Airbrake"); }
}
```

```java
public class AirbrakeExtension extends AbstractSimulationExtension {
    static final FlightDataType EXT  = FlightDataType.getType("Airbrake extension", "ab_ext", UnitGroup.UNITS_RELATIVE);
    static final FlightDataType APOG = FlightDataType.getType("Airbrake predicted apogee", "ab_apo", UnitGroup.UNITS_DISTANCE);
    static final FlightDataType DCD  = FlightDataType.getType("Airbrake delta CD", "ab_dcd", UnitGroup.UNITS_COEFFICIENT);

    @Override public String getName() { return "Airbrake (" + getMode() + ")"; }
    @Override public List<FlightDataType> getFlightDataTypes() { return List.of(EXT, APOG, DCD); }

    @Override public void initialize(SimulationConditions c) throws SimulationException {
        c.getSimulationListenerList().add(new AirbrakeListener(
            ModeloArrasto.de(this), Controladores.de(this), new Atuador(getActuatorRatePctS() / 100), limites()));
    }

    // Um par getter/setter por chave, todos em SI, como no plugin original:
    public String getMode()             { return config.getString("mode", "off"); }
    public void   setMode(String v)     { config.put("mode", v); fireChangeEvent(); }
    public double getTargetApogeeM()    { return config.getDouble("target_apogee_m", 0.0); }
    // ... cd, area_max_m2, fixed_extension_pct, schedule, kp, ki, kd,
    //     enable_delay_s, min_speed_ms, max_mach, actuator_rate_pct_s
}
```

```java
class AirbrakeListener extends AbstractSimulationListener {
    private double tBurnout = Double.NaN, ext = 0, dCD = 0, apogeuPrev = Double.NaN;
    private FlightConditions cond;

    @Override public boolean handleFlightEvent(SimulationStatus s, FlightEvent e) {
        if (e.getType() == FlightEvent.Type.BURNOUT && Double.isNaN(tBurnout)) tBurnout = e.getTime();
        return true;
    }

    // Uma vez por passo: o controlador decide e o atuador limita a taxa.
    @Override public boolean preStep(SimulationStatus s) {
        double alvo = habilitado(s) ? controlador.abertura(EstadoVoo.de(s, cond, modelo)) : 0.0;
        ext = atuador.mover(ext, alvo, s.getSimulationConditions().getTimeStep());
        return true;
    }

    @Override public FlightConditions postFlightConditions(SimulationStatus s, FlightConditions fc) {
        cond = fc;
        return fc;
    }

    // O RK4 chama isto 4x por passo: só aplica o ext já decidido, sem rodar o controlador.
    @Override public AerodynamicForces postAerodynamicCalculation(SimulationStatus s, AerodynamicForces f) {
        if (ext > 0 && cond != null) {
            dCD = modelo.cdA(ext, cond.getMach()) / cond.getRefArea();
            f.setCDaxial(f.getCDaxial() + dCD);
        } else dCD = 0;
        return f;
    }

    // Grava as séries depois do passo, quando o ponto do instante atual já existe.
    @Override public void postStep(SimulationStatus s) {
        FlightDataBranch b = s.getFlightDataBranch();
        b.setValue(AirbrakeExtension.EXT, ext);
        b.setValue(AirbrakeExtension.DCD, dCD);
        b.setValue(AirbrakeExtension.APOG, apogeuPrev);
    }

    private boolean habilitado(SimulationStatus s) {
        return !Double.isNaN(tBurnout) && s.getSimulationTime() >= tBurnout + lim.enableDelayS
            && !s.isApogeeReached() && s.getRocketVelocity().z > lim.minSpeedMs
            && (cond == null || cond.getMach() < lim.maxMach);
    }

    @Override public AirbrakeListener clone() { /* cópia profunda do estado do controlador (§6) */ }
}
```

### 3.4 Compilação (`scripts/compilar_airbrake.py`)

O script faz o equivalente a:

```
javac --release 17 -cp vendor/OpenRocket-24.12.jar -d build/airbrake  (todos os .java de java/airbrake/src)
jar --create --file vendor/rocket-mcp-airbrake.jar -C build/airbrake .
```

Ele é escrito em Python, como o `preparar_vendor.py`, para funcionar igual no Windows e no Linux. Ele falha com
mensagem clara se não houver `javac` no PATH ou no `JAVA_HOME`. O JAR gerado vai para o `.gitignore`, igual
ao do OpenRocket.

Para usar na GUI, copie o JAR para `%APPDATA%\OpenRocket\Plugins` (Windows) ou `~/.openrocket/Plugins` e reinicie.
A extensão aparece em *Simulation options > Add extension > Rocket MCP > Airbrake*. Sem um painel Swing próprio,
a GUI mostra a extensão mas não deixa editar os parâmetros. Um configurador Swing fica para a fase 5.

---

## 4. Modelo físico

### 4.1 Arrasto do freio

```
CDaxial_total = CDaxial_OpenRocket + ΔCD
ΔCD           = Cd_freio(Mach) · A_exposta(ext) / A_ref
```

- `A_ref` é a área de referência do OpenRocket (`FlightConditions.getRefArea()`, a seção do maior diâmetro).
- `A_exposta(ext)` é a área frontal das aletas do freio em função da abertura `ext ∈ [0, 1]`. O padrão é linear
  (`ext · area_max_m2`). Pode ser trocada por uma tabela `extension_curve` com pares `[ext, fração_da_área]`,
  para mecanismos com came ou bielas, em que a área não cresce linearmente com o curso.
- `Cd_freio` tem padrão **1,17** (placa plana normal ao escoamento, subsônico) e é configurável. Também aceita uma
  tabela `cd_by_mach` com pares `[Mach, Cd]` (interpolação linear e saturação nas pontas) para quem tiver CFD ou
  dados de túnel. Perto de Mach 1 a placa plana subestima o arrasto: por isso o `max_mach` padrão é 0,8.
- O modelo é incremental e ignora a interferência entre o freio e a esteira do corpo. Esse é o principal erro da
  aproximação, e o Cd efetivo medido costuma ficar abaixo do da placa isolada. Se houver CFD do foguete inteiro com
  o freio (como no plugin da Waterloo), dá para criar um modo `cd_override` que substitui o CDaxial. Isso não fica
  no padrão.

### 4.2 Atuador

A abertura não salta: `|Δext| ≤ actuator_rate_pct_s/100 · dt`. O padrão é 100 %/s, de fechado a aberto em 1 s.
Isso evita que o controlador "teletransporte" o freio e dá resultados mais realistas para a sintonia.

### 4.3 Previsão de apogeu (usada por `target` e `pid`)

Usa a forma fechada do voo vertical com arrasto quadrático e densidade constante:

```
Δh = m / (ρ·CdA) · ln(1 + ρ·CdA·vz² / (2·m·g))
apogeu_previsto = z + Δh
CdA = CDaxial_OR·A_ref + Cd_freio·A_exposta(ext)
```

O custo por chamada é desprezível. Ela superestima um pouco o apogeu, porque a densidade cai com a altitude, e
ignora a componente horizontal. Isso é aceitável para o controle, porque o erro diminui à medida que o apogeu se
aproxima. Se o erro sistemático incomodar, a alternativa é integrar numericamente com densidade ISA (Euler,
dt = 0,05 s), o que custa algumas centenas de operações por passo.

---

## 5. Modos de controle

| `mode` | Comportamento | Parâmetros |
|---|---|---|
| `off` | Sem freio. Igual a não ter a extensão. | - |
| `fixed` | Abre até `fixed_extension_pct` assim que for permitido. | `fixed_extension_pct` |
| `schedule` | Tabela `[[t_s, ext_pct], ...]`, com tempo contado **a partir do burnout**. Interpolação linear. | `schedule` |
| `target` | A cada passo, faz bisseção (cerca de 12 iterações) da abertura constante que leva o apogeu previsto ao alvo. Não precisa de ganhos, e é o **modo recomendado** para começar. | `target_apogee_m` |
| `pid` | `ext = Kp·e + Ki·∫e + Kd·ė`, com `e = apogeu_previsto − alvo`. Integrador saturado e congelado quando a saída satura (anti-windup). | `target_apogee_m`, `kp`, `ki`, `kd` |

Limites comuns a todos os modos:
- `enable_delay_s`: segundos depois do burnout (padrão 0,5);
- `min_speed_ms`: velocidade vertical mínima (padrão 20);
- `max_mach`: Mach máximo (padrão 0,8);
- `actuator_rate_pct_s`: taxa do atuador (padrão 100).

Fora da janela permitida o freio **fecha** (sem cortar abruptamente, respeitando a taxa do atuador).

Ruído de sensor e atraso de malha, que o plugin da Waterloo tinha, ficam para a fase 4. Quando entrarem, vão
usar a semente da simulação (`getRandomSeed()`) para manter a repetibilidade que o projeto já garante com `semente`.

---

## 6. Armadilhas previstas

1. **O RK4 chama `postAerodynamicCalculation` 4 vezes por passo.** O controlador roda só no `preStep`, e o hook
   aerodinâmico só aplica o `ext` já decidido. Se o controlador rodasse ali, o PID integraria 4 vezes por passo.
2. **Gravar séries no `preStep` grava no ponto anterior** **[verificar]**. O plugin da Waterloo fazia isso. Nós
   gravamos no `postStep`.
3. **`clone()` do listener.** O OpenRocket clona listeners ao criar ramos (separação de estágio). A cópia tem de
   ser profunda: o estado do integrador do PID não pode ficar compartilhado entre ramos.
4. **Registro pelo ClassGraph.** O `@Plugin` só é encontrado se o JAR estiver no classpath **antes** do
   `OpenRocketCore.initialize(...)`. Se o JAR não existir, o `or_bridge` sobe sem ele, e qualquer pedido de
   airbrake vira `ToolError` com a instrução "rode `python scripts/compilar_airbrake.py`".
5. **`.ork` com a extensão aberto sem o JAR** (na GUI sem o plugin ou num servidor antigo). O OpenRocket avisa
   que a extensão é desconhecida e a ignora **[verificar o comportamento exato]**. O `abrir_foguete` deve mostrar
   esse aviso.
6. **`sim.getSimulationExtensions()` é a lista viva** **[verificar]**. Para trocar a configuração, removemos a
   extensão antiga e adicionamos a nova. Nunca duas `AirbrakeExtension` na mesma simulação.
7. **Mutação do documento.** Aplicar `airbrake` numa simulação altera o `doc` em memória. Como hoje, isso só vai
   para o disco com `salvar_como`, em arquivo novo, sem sobrescrever.
8. **Faixas de valor.** Validamos tudo no Python antes de chegar ao Java (`fixed_extension_pct ∈ [0, 100]`,
   `area_max_m2 > 0`, tabelas ordenadas etc.). O Java também valida e lança `SimulationException`, que o
   `_erros_java` já transforma em `ToolError`.
9. **`LOCK`.** `configurar_airbrake` e a leitura das séries rodam dentro do `LOCK` global, como todo acesso ao
   OpenRocket.

---

## 7. Integração no Python

### 7.1 `or_bridge.py`

```python
JAR_AIRBRAKE = RAIZ / "vendor" / "rocket-mcp-airbrake.jar"

# em iniciar(): o JAR entra no classpath só se existir
classpath=[str(JAR)] + ([str(JAR_AIRBRAKE)] if JAR_AIRBRAKE.exists() else []),

def configurar_airbrake(sim, spec: dict | None) -> list[str]:
    """Cria, troca ou remove a AirbrakeExtension da simulação. spec=None ou {"mode": "off"} remove.
    Unidades SI; a validação de faixas fica aqui, antes do Java."""

def series_airbrake(ramo) -> dict:
    """Abertura (%), ΔCD e apogeu previsto ao longo do tempo, mais o resumo: abertura máxima, instante de
    abertura e fechamento, Mach na abertura e erro em relação ao alvo."""
```

As séries novas são lidas com `FlightDataType.getType("Airbrake extension", "ab_ext", UnitGroup.UNITS_RELATIVE)`.
O `getType` devolve a instância já registrada quando o nome bate.

### 7.2 Tools do MCP

Os parâmetros seguem as convenções do projeto: nomes em inglês com sufixo de unidade, e nenhuma gravação sobre
arquivo existente sem pedido explícito.

**`simular_voo` e `projetar_e_voar`** ganham um argumento opcional `airbrake: dict | None`:

```json
{
  "mode": "target",
  "target_apogee_m": 3000,
  "cd": 1.17,
  "area_max_m2": 0.0045,
  "actuator_rate_pct_s": 100,
  "enable_delay_s": 0.5,
  "max_mach": 0.8,
  "compare_without": true
}
```

A resposta ganha um bloco `airbrake`:

```json
{
  "modo": "target",
  "apogeu_m": 3001.8,
  "apogeu_sem_freio_m": 3412.5,
  "reducao_m": 410.7,
  "erro_alvo_m": 1.8,
  "extensao_max_pct": 64.0,
  "abertura_s": 6.1, "fechamento_s": 21.7,
  "mach_na_abertura": 0.71,
  "saturou": false
}
```

`compare_without` roda uma segunda simulação sem o freio, com a mesma semente, e por isso dobra o tempo da
chamada. O padrão é `true` porque essa é a pergunta mais comum.

**`ajustar_airbrake`** (nova) avalia a robustez do controle. Ela roda N voos variando vento médio, turbulência,
massa (±%) e Cd do freio (±%), e devolve a distribuição do erro de apogeu (média, desvio e pior caso) e a fração
de voos em que o freio saturou. Com `mode="pid"`, aceita uma grade de `kp`/`ki`/`kd` e devolve a melhor
combinação. Tem `ctx.report_progress`, como `simular_motor`.

**Séries temporais:** quando o projeto tiver exportação de séries de voo, as colunas `ab_ext`, `ab_dcd` e
`ab_apo` entram automaticamente, porque fazem parte do `FlightDataBranch`.

---

## 8. Testes

Em `tests/test_airbrake.py`, com o `conftest` atual: workspace temporário, JVM única e cliente MCP em memória.
Os testes são pulados (`pytest.skip`) se o JAR não tiver sido compilado.

| Teste | Critério |
|---|---|
| Registro | `AirbrakeProvider` aparece entre os providers do Guice depois do `initialize` |
| Neutro | `mode="fixed", fixed_extension_pct=0` dá o mesmo apogeu do voo sem extensão (diferença < 0,1 m, mesma semente) |
| Monotônico | Apogeu decresce estritamente para abertura fixa de 0, 25, 50, 75 e 100 % |
| Ordem de grandeza | Para `fixed` 100 %, a redução de apogeu fica dentro de ±15 % de uma estimativa analítica feita no teste |
| Janela | `ab_ext` é 0 antes do burnout + `enable_delay_s` e depois do apogeu |
| Atuador | Nenhuma diferença entre passos consecutivos excede `rate·dt` |
| Alvo | `mode="target"` com alvo 10 % abaixo do apogeu sem freio: \|erro\| < 1 % do alvo |
| Alvo inatingível | Alvo acima do apogeu sem freio: freio fica em 0 e a resposta traz um aviso |
| Ida e volta do `.ork` | `salvar_como` grava, `abrir_foguete` relê, e a config da extensão volta idêntica |
| Sem JAR | Com o JAR ausente, pedir `airbrake` gera `ToolError` com a instrução de compilação |
| Validação | `fixed_extension_pct=150`, `area_max_m2=-1` e `mode` desconhecido geram `ToolError` antes de simular |

Além disso: `python teste_cliente.py` antes de cada commit (CLAUDE.md), com uma chamada de `simular_voo`
usando `airbrake`.

---

## 9. Fases

| Fase | Entrega | Pronto quando |
|---|---|---|
| 1 | Módulo Java com `fixed`, script de compilação, `configurar_airbrake`, argumento `airbrake` em `simular_voo` | Testes Registro, Neutro, Monotônico, Janela e Sem JAR passam |
| 2 | Modo `target` + previsor de apogeu + atuador + `compare_without` | Testes Alvo e Atuador passam |
| 3 | `pid` e `schedule`, ida e volta no `.ork`, `ajustar_airbrake` | Todos os testes da §8 passam |
| 4 | Ruído de sensor e atraso de malha com semente; `cd_by_mach` e `extension_curve` | Monte Carlo repetível com a mesma semente |
| 5 (opcional) | Painel Swing para editar a extensão na GUI do OpenRocket | Parâmetros editáveis na GUI, e o `.ork` abre igual nos dois lados |

Ao concluir cada fase: registrar no `docs/DIARIO.md` e atualizar a seção de arquitetura do `docs/ANALISE.md`.

---

## 10. Dados necessários do projeto real

Os padrões permitem simular já, mas o resultado só tem valor quantitativo com dados do freio de verdade:

- área frontal exposta máxima das aletas (m²) e como ela varia com o curso do atuador;
- velocidade do atuador (tempo de fechado a aberto);
- Cd do freio: CFD, túnel ou, no mínimo, comparação com um voo real;
- restrições operacionais: Mach máximo para abrir (carga estrutural) e atraso mínimo depois do burnout;
- apogeu-alvo e tolerância exigida (regulamento da competição, se for o caso).
