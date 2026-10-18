# Servidor MCP unificado para openMotor + OpenRocket

**Autor da análise:** Claude (conversa com João, 16/10/2026)
**Status:** esqueleto funcional, testado de ponta a ponta em Linux (Python 3.12, Java 21). Não testado em Windows (ver §9).
**Objetivo:** um único servidor MCP que expõe a balística interna do **openMotor** e a simulação de trajetória do **OpenRocket**, permitindo fluxos encadeados como *projetar o grão → gerar a curva de empuxo → simular o voo*.

---

## 0. Instruções para o Claude Code

Este documento é a especificação e o ponto de partida do projeto. Ao implementar:

1. **Crie os arquivos da §7 exatamente como estão.** O código foi executado e validado. Não reescreva as partes marcadas como "armadilha" (§5) sem entender o motivo.
2. **Rode `teste_cliente.py`** (§8) antes de qualquer mudança, para ter uma linha de base.
3. **Siga o roadmap da §10** na ordem. Cada item tem um critério de aceitação.
4. **Mantenha estas invariantes:**
   - Nada pode ser escrito no **stdout** além do JSON-RPC (logs vão para stderr).
   - Todo acesso ao OpenRocket e ao `motorlib` passa pelo `LOCK` global.
   - Unidades SI nas APIs internas. Nas tools, o sufixo do parâmetro indica a unidade (`_mm`, `_kg`, `_graus`...).
   - Caminhos recebidos do LLM são sempre validados por `_caminho()` (restritos ao workspace).
   - Erros que o LLM precisa ler são lançados como `ToolError`. Outras exceções chegam como uma mensagem genérica.
5. Se mudar a versão do OpenRocket ou do openMotor, revalide as armadilhas da §5, porque várias dependem da versão.

---

## 1. Contexto: o que é um servidor MCP (resumo)

- O **MCP (Model Context Protocol)** padroniza como um LLM acessa ferramentas externas. Há três papéis: o **host** (Claude Desktop ou Claude Code), o **client** (uma conexão por servidor) e o **server** (este projeto).
- A comunicação usa **JSON-RPC 2.0**. O ciclo é `initialize` (negociação de capacidades) → `notifications/initialized` → chamadas (`tools/list`, `tools/call`, `resources/read`...).
- **Transporte:** usamos **stdio**. O host executa o servidor como subprocesso e conversa por stdin/stdout. Consequência crítica: **o stdout é exclusivo do protocolo**.
- **Primitivas usadas:**
  - **Tools**: ações que o modelo decide chamar. O nome, a docstring e os type hints viram a "interface" que o LLM lê, então descreva bem as unidades e o que cada parâmetro significa.
  - **Resources**: dados que o cliente pode anexar como contexto. Aqui, a biblioteca de propelentes.
  - **Progress notifications**: usadas na simulação do motor.
- **SDK:** pacote oficial `mcp` para Python, **versão 2.x**. Atenção: no 2.x, `FastMCP` foi renomeado para `MCPServer` (`from mcp.server.mcpserver import MCPServer`). Exemplos antigos da internet com `from mcp.server.fastmcp import FastMCP` **não funcionam** no 2.x.
- **Comportamento do SDK que importa aqui:** tools síncronas (`def`) são executadas em threads de um pool (`anyio.to_thread`). Tools `async def` rodam no event loop.

---

## 2. Análise do openMotor

**Versão analisada:** 0.6.2, commit `dcc7fd62045d55036451b833f60e16a786f4685d` (26/09/2026). Repositório: https://github.com/reilleya/openMotor. Licença GPL-3.0.

### 2.1 Organização do código

| Pacote | Conteúdo | Usamos? |
|---|---|---|
| `motorlib/` | Toda a física: grãos, tubeira, propelente, simulação. **Não importa PyQt.** Depende de numpy, scipy, scikit-fmm e scikit-image. | **Sim** |
| `mathlib/` | Rotina de perímetro por *marching squares* em **Cython** (`_find_perimeter_cy.pyx`). Precisa ser compilada. | Substituída (§2.4) |
| `uilib/` | GUI Qt, leitura e escrita de arquivos, exportadores, biblioteca de propelentes. **Acoplado ao PyQt6 e ao matplotlib.** | Só `defaults.py`, lido direto do arquivo |

### 2.2 Fluxo de dados

1. **Arquivo `.ric`** = YAML com as chaves `version`, `type` e `data`. A chave `data` é um dicionário com `nozzle`, `propellant`, `grains` (uma lista de `{type, properties}`) e `config`.
2. `motorlib.motor.Motor(data)` monta o motor. O inverso é `motor.getDict()`.
3. `motor.runSimulation(callback)` executa um laço temporal com passo `config.timestep`:
   - Kn = A_queima / A_garganta;
   - a pressão de câmara vem do equilíbrio de Kn do propelente;
   - o empuxo vem do coeficiente de empuxo ajustado × A_garganta × P_c;
   - o grão regride segundo a lei de Saint-Robert, **r = a·Pⁿ**;
   - o modelo inclui erosão e deposição de escória na garganta.
4. O resultado é um `SimulationResult` com **canais** de séries temporais (`time`, `force`, `pressure`, `kn`, `mass`, `massFlow`, `massFlux`, `machNumber`, `exitPressure`, `dThroat`...) e getters como `getImpulse()`, `getISP()`, `getBurnTime()`, `getMaxPressure()`, `getDesignation()`, `getPropellantMass()` e `getPeakMassFlux()`. Ele também traz uma lista de **alertas** (`ERROR` ou `WARNING`).
5. O `callback(frac)` recebe o progresso de 0 a 1. Se retornar `True`, a simulação é cancelada.

**Unidades internas: tudo em SI** (m, Pa, kg, N, s). Para conversões, existe `motorlib/units.py`.

**Grãos disponíveis** (`motorlib/grains/__init__.py`, dicionário `grainTypes`):
- **Analíticos (rápidos):** BATES, Rod and Tube, Conical, End Burner.
- **Fast Marching Method sobre uma malha `mapDim`² (mais lentos):** Finocyl, Star, X Core, C Grain, D Grain, Moon Burner, Custom (DXF).

**Funções úteis sem GUI:**
- `motor.getQuickResults()`: Kn inicial, carregamento volumétrico, razão porta/garganta e massa, sem simular.
- `simResult.getCSV()`: exportação em CSV.

### 2.3 Medições

| Motor de teste | Grão | Resultado | Tempo de simulação |
|---|---|---|---|
| `regression/simple` | 2× BATES | L1065, 4922 N·s | 0,05 s |
| `regression/x` | X Core | N2991, 19651 N·s | 0,8 s (Cython) / 1,5 s (NumPy) |
| `real/o3800` | 4× BATES | O4204, 7,89 s de queima | 0,1 s |

O motor `real/o3800` foi comparado com o ensaio estático real que acompanha o repositório: **3800 N médios e 7,25 s**. A simulação ficou cerca de 10% acima.

### 2.4 Armadilhas do openMotor (todas resolvidas no código)

1. **Carregar um `.ric` pela função original puxa a GUI.** `uilib/fileIO.py` importa PyQt6 no topo, e `uilib/__init__.py` importa matplotlib. **Solução:** reimplementamos o carregamento e lemos `uilib/defaults.py` direto do arquivo, sem importar o pacote `uilib`.
2. **O carregamento original é inseguro.** Ele usa `yaml.Loader`, que instancia objetos Python arbitrários a partir do arquivo. Os `.ric` contêm as tags `!!python/tuple` e `!!python/object/apply:uilib.fileIO.fileTypes`, então `safe_load` puro falha. **Solução:** um `SafeLoader` com dois construtores específicos (`_RicLoader`).
3. **Migrações de versão.** Arquivos antigos (os de teste são v0.4.0) não têm algumas chaves de config. Sem migrar, a simulação roda, mas gera **alertas falsos**: o limite de Mach ausente vira 0, e todo motor "excede" esse limite. **Solução mínima:** preencher as chaves ausentes com `DEFAULT_PREFERENCES['general']`. **Pendente:** portar as migrações completas de `uilib/fileIO.py` (roadmap).
4. **A extensão Cython exige compilador C.** No Windows, isso significa MSVC Build Tools. Além disso, o `setup.py` do openMotor falha com setuptools antigos por causa do campo `license` do `pyproject.toml`. **Solução:** `_perimetro_numpy.py`, uma reimplementação vetorizada do modo usado na simulação (só o perímetro). Ela é injetada em `sys.modules` quando a extensão não está compilada.
   - **Validação:** em 25 níveis de regressão de 3 grãos FMM, o **erro relativo máximo foi 7×10⁻¹⁵** (precisão de máquina). Os resultados da simulação foram idênticos em todas as casas exibidas.
   - **Custo:** cerca de 1,7× mais lento em grãos FMM. Em BATES, a diferença é desprezível.
   - O modo com contornos (`including_contours=True`) só é usado nas prévias gráficas da GUI (`getRegressionData`) e lança `NotImplementedError`.
   - Se a extensão compilada existir, ela é usada automaticamente (`om_bridge.PERIMETRO` informa qual implementação está ativa).
5. **O exportador `.eng` está acoplado a um QDialog** (`uilib/converters/engExporter.py`). **Solução:** reimplementado em `exportar_eng()`. O exportador exige dados que o `.ric` não tem: diâmetro e comprimento do envelope do motor, massa de hardware e designação.
6. **Os getters retornam `np.float64`**, que o JSON do MCP não serializa. **Solução:** a função `_f()` converte para `float` nativo.
7. **`motorlib` não é thread-safe** e o SDK executa tools em threads. **Solução:** o `LOCK` global.

---

## 3. Análise do OpenRocket

**Versão usada:** release **24.12** (27/07/2025), que é a última release estável. O branch principal está em `26.xx-SNAPSHOT` (commit analisado `591c5e5f`, 07/10/2026). Repositório: https://github.com/openrocket/openrocket. Licença GPL-3.0.

### 3.1 Organização do código

- O repositório tem dois módulos: **`core`** (física, arquivos, motores, simulação) e **`swing`** (GUI).
- Os pacotes foram **renomeados de `net.sf.openrocket` para `info.openrocket`** na 24.12. Por isso, bibliotecas antigas como o `orhelper` não funcionam como estão.
- O **JAR da release (83 MB) já contém tudo**. Não é preciso compilar nada, basta colocá-lo no classpath do JPype.
- Existe uma classe oficial para usar o core sem GUI: **`info.openrocket.core.startup.OpenRocketCore`**.
- O repositório tem um `AGENTS.md` com regras para contribuições feitas com IA. Elas só se aplicam se você for abrir PRs no OpenRocket.

### 3.2 Fluxo de dados e API usada

| Etapa | API Java |
|---|---|
| Inicializar | `OpenRocketCore.initialize(PluginModule())`: Guice + carregamento assíncrono dos bancos de motores e componentes |
| Abrir `.ork` | `GeneralRocketLoader(File).load()` → `OpenRocketDocument` (com `getWarnings()`) |
| Árvore do foguete | `doc.getRocket()` é iterável sobre os `RocketComponent` |
| Simulações | `doc.getSimulations()` → `Simulation`; `sim.getFlightConfigurationId()`, `sim.getOptions()` |
| Simular | `sim.simulate()` → `sim.getSimulatedData()` → `FlightData` |
| Resultados | `getMaxAltitude`, `getMaxVelocity`, `getMaxMachNumber`, `getMaxAcceleration`, `getTimeToApogee`, `getLaunchRodVelocity`, `getDeploymentVelocity`, `getOptimumDelay`, `getGroundHitVelocity`, `getFlightTime`, `getWarningSet`; séries em `getBranches()` → `FlightDataBranch` |
| Motor de arquivo | `GeneralMotorLoader().load(InputStream, nome)` → `List<ThrustCurveMotor.Builder>` → `.build()` |
| Banco de motores | `Application.getMotorSetDatabase().addMotor(motor)` |
| Trocar motor | `MotorMount` (por exemplo `InnerTube`): `MotorConfiguration(mount, fcid)`, `.setMotor()`, `.setEjectionDelay()`, `mount.setMotorConfig(mc, fcid)` |
| Opções | `SimulationOptions`: `setLaunchRodLength`, `setLaunchRodAngle` (**radianos**), `setWindSpeedAverage`, `setLaunchAltitude`... |
| Salvar | `GeneralRocketSaver().save(File, doc)` |

**Unidades internas: SI e radianos.** `getMotorMountDiameter()` retorna o **diâmetro interno** da montagem (`getInnerRadius()*2`).

**Conceito importante:** o `.ork` **não guarda a curva de empuxo**. Ele guarda uma referência (fabricante, designação e digest) que é resolvida no banco de motores ao carregar, via `DatabaseMotorFinder`.

### 3.3 Medições

- **JVM + `initialize`:** cerca de 8 s.
- **Abrir o `.ork` de exemplo:** cerca de 1,7 s.
- **Cada simulação:** 0,5 a 1,4 s. As primeiras são mais lentas porque o JIT ainda está aquecendo.
- **Exemplo "A simple model rocket"** (montagem de 18 mm, motor A8-3): apogeu de 51 m.
- **O mesmo foguete com o motor F46 gerado pelo openMotor:** apogeu de cerca de 1016 m, Mach 0,87.

### 3.4 Armadilhas do OpenRocket (todas resolvidas no código)

1. **Bug de inicialização na 24.12.** `OpenRocketCore.initialize()` sem argumentos não registra o `PluginModule`, e o Guice falha com `No implementation for Set<RocketSubstitutor> was bound`. O código do branch principal já corrige isso. **Solução:** passar o módulo explicitamente com `OpenRocketCore.initialize(JArray(Module)([PluginModule()]))`.
2. **Os logs saem no stdout.** O OpenRocket usa logback com saída para `System.out`. Medimos **106 linhas só na inicialização**, o que **corrompe o canal JSON-RPC**. **Solução:** `config/logback-mcp.xml` (saída em `System.err`, nível WARN), passado via `-Dlogback.configurationFile`. Validado: 0 linhas no stdout.
3. **Motores customizados somem ao recarregar o `.ork`.** O carregamento gera o aviso *"No motor with designation 'F46-OM' for manufacturer 'openMotor' found."* e o foguete fica sem motor. **Solução:** registrar o motor no banco (`addMotor`) **antes** de carregar. O servidor registra todos os `.eng` da pasta `motores/` na inicialização e a cada exportação.
4. **O OpenRocket não valida o encaixe do motor.** Um motor de 29 mm numa montagem de 18 mm simulou sem nenhum aviso. **Solução:** `trocar_motor()` compara diâmetro e comprimento e devolve `avisos_validacao`.
5. **Uma JVM por processo.** O JPype não reinicia a JVM, e o OpenRocket usa estado global (`Application.injector` estático). **Solução:** um processo persistente e o `LOCK` global.
6. **A inicialização lenta atrasaria o handshake MCP** em cerca de 8 a 11 s. **Solução:** a JVM sobe numa thread (`iniciar_em_segundo_plano`), e o `initialize` responde em cerca de 1,7 s. As tools do openMotor funcionam imediatamente. As do OpenRocket esperam em `garantir()`.
7. **Diferenças sutis no JPype:**
   - Pacotes Java que colidem com módulos Python (como `io.github...`) não podem ser importados com `import`. Use `jpype.JClass("io.github.classgraph.ClassGraph")`.
   - O `PluginModule` usa ClassGraph para achar classes `@Plugin`. Isso funcionou normalmente com o JAR no classpath.

### 3.5 Semântica do "atraso ótimo" (cuidado ao interpretar)

O `getOptimumDelay()` é calculado sobre um voo **sem abertura do sistema de recuperação** (coast balístico até o apogeu verdadeiro). Se o paraquedas abre antes do apogeu, o `apogeu_m` reportado já está **reduzido pela abertura precoce**, enquanto o `atraso_otimo_s` reflete o voo ideal.

No teste, com atraso de 5 s, o tempo até o apogeu foi de 7,55 s e o atraso ótimo, de 12,1 s. Isso indica que **o paraquedas abriu cedo** (o aviso de velocidade alta na abertura confirma) e que o apogeu real com o atraso correto seria maior.

---

## 4. Arquitetura do servidor

```
┌─────────────── Host MCP (Claude Code / Claude Desktop) ───────────────┐
│                         stdio (JSON-RPC 2.0)                           │
└───────────────────────────────┬────────────────────────────────────────┘
                                │
                ┌───────────────▼───────────────┐   processo Python único
                │ server.py (MCPServer, tools)  │   LOCK global (RLock)
                │  _caminho(): sandbox workspace│
                └───────┬───────────────┬───────┘
                        │               │
            ┌───────────▼────┐   ┌──────▼─────────────────────────┐
            │ om_bridge.py   │   │ or_bridge.py                   │
            │ motorlib (Py)  │   │ JPype → JVM → OpenRocket 24.12 │
            │ + perímetro    │   │ logback → stderr               │
            │   NumPy        │   │ banco de motores ← motores/*.eng│
            └───────┬────────┘   └──────▲─────────────────────────┘
                    │  .eng (RASP)      │
                    └───────────────────┘
```

### 4.1 Tools implementadas

| Tool | Entrada principal | O que faz |
|---|---|---|
| `simular_motor` | `arquivo_ric` | Balística interna e resumo, com progresso (async) |
| `exportar_motor_eng` | `.ric`, diâmetro e comprimento do case (mm), massa de hardware (kg) | Gera `motores/<nome>.eng` e registra no OpenRocket |
| `abrir_foguete` | `arquivo_ork` | Nome, simulações e montagens de motor (diâmetro interno, comprimento, motor atual) |
| `simular_voo` | `.ork`, índice da simulação, `.eng` e atraso opcionais, trilho, ângulo, vento, altitude, `salvar_como` | Trajetória e avisos. Não altera o original se `salvar_como` não for informado |
| `projetar_e_voar` | `.ric` + `.ork` + dados do case + atraso | Pipeline completo |

**Resource:** `openmotor://propelentes`, com os 20 propelentes padrão (KNSB, KNDX, KNSU, RCS...).

### 4.2 Decisões de design

- **Servidor único**, porque permite tools encadeadas como `projetar_e_voar`.
- **Workspace (`ROCKET_MCP_WORKSPACE`)**: todos os caminhos são relativos a ele e não podem escapar dele.
- **Arquivos originais são somente leitura**, salvo `salvar_como` explícito.
- **Retornos em português e com unidade no nome da chave** (`apogeu_m`, `pressao_max_MPa`), para o LLM não errar unidades.

---

## 5. Checklist de armadilhas (resumo)

| # | Armadilha | Onde está tratada |
|---|---|---|
| 1 | Logs do OpenRocket no stdout corrompem o MCP | `config/logback-mcp.xml` + `-Dlogback.configurationFile` |
| 2 | `initialize()` da 24.12 sem `PluginModule` | `or_bridge.iniciar()` |
| 3 | Motor customizado some ao recarregar o `.ork` | `registrar_motor()` no boot e em cada exportação |
| 4 | OpenRocket não valida diâmetro e comprimento do motor | `or_bridge.trocar_motor()` |
| 5 | JVM única por processo e estado global | `LOCK`, processo persistente |
| 6 | Handshake lento por causa da JVM | `iniciar_em_segundo_plano()` + `garantir()` |
| 7 | `uilib` puxa PyQt6 e matplotlib | `_carregar_defaults()` lê o arquivo direto |
| 8 | `yaml.Loader` inseguro | `_RicLoader(SafeLoader)` |
| 9 | Arquivos `.ric` antigos sem chaves de config | `_migrar()` (mínima) |
| 10 | Cython exige compilador | `_perimetro_numpy.py` |
| 11 | `np.float64` não serializa | `_f()` |
| 12 | Exceções viram "Error executing tool" genérico | Lançar `ToolError` para mensagens destinadas ao LLM |
| 13 | Tools síncronas rodam em threads | `LOCK`; progresso via `anyio.from_thread.run` |
| 14 | SDK `mcp` 2.x renomeou FastMCP | `from mcp.server.mcpserver import MCPServer` |
| 15 | Ângulos em radianos no OpenRocket | `configurar()` converte de graus |
| 16 | Arquivos lidos com encoding padrão do Windows (cp1252) | `encoding="utf-8"` explícito |

---

## 6. Estrutura do projeto e instalação

```
rocket-mcp/
├── pyproject.toml
├── .gitignore
├── config/
│   └── logback-mcp.xml
├── scripts/
│   └── preparar_vendor.py       # baixa openMotor (commit fixo) e JAR do OpenRocket 24.12
├── src/rocket_mcp/
│   ├── __init__.py              # vazio
│   ├── server.py                # MCPServer + tools
│   ├── om_bridge.py             # openMotor (motorlib)
│   ├── or_bridge.py             # OpenRocket (JPype)
│   └── _perimetro_numpy.py      # substituto da extensão Cython
├── teste_cliente.py             # teste ponta a ponta via stdio
├── vendor/                      # gerado pelo script (não versionar)
│   ├── openMotor/
│   └── OpenRocket-24.12.jar
└── workspace_teste/             # foguete.ork + motor.ric para o teste
```

> **Instale em modo editável (`pip install -e .`).** Os módulos localizam `vendor/` e `config/` subindo dois níveis a partir de `src/rocket_mcp/`. Uma instalação não editável quebraria esses caminhos (está no roadmap).

### 6.1 Pré-requisitos

- **Python 3.12 ou 3.13.** O scikit-fmm ainda não publica wheel para 3.14, e as versões atuais de numpy e scipy exigem 3.12 ou mais.
- **Java 17 ou mais recente** (os testes usaram **Java 21**). Não é preciso compilador C nem git.

### 6.2 Windows (PowerShell)

```powershell
# 1) Java: instale o Eclipse Temurin JDK 21 (https://adoptium.net) e marque "Set JAVA_HOME"
#    O JPype localiza a JVM pelo JAVA_HOME.
# 2) Python 3.12 ou 3.13 (python.org), marcando "Add to PATH"
cd C:\caminho\rocket-mcp
py -3.12 -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python scripts\preparar_vendor.py

# 3) Workspace de teste
mkdir workspace_teste
copy vendor\openMotor\test\data\regression\tiny\motor.ric workspace_teste\motor.ric
#    foguete.ork: no OpenRocket, File > Open Example > "A simple model rocket" > Save As workspace_teste\foguete.ork
#    (ou extraia "datafiles/examples/A simple model rocket.ork" de dentro do JAR, que é um zip)
.venv\Scripts\python teste_cliente.py
```

### 6.3 Linux / macOS

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python scripts/preparar_vendor.py
mkdir -p workspace_teste
cp vendor/openMotor/test/data/regression/tiny/motor.ric workspace_teste/
unzip -p vendor/OpenRocket-24.12.jar "datafiles/examples/A simple model rocket.ork" > workspace_teste/foguete.ork
.venv/bin/python teste_cliente.py
```

### 6.4 Registrar no Claude Code

```powershell
# Windows: o python.exe do venv é um executável de verdade (não um shim .cmd), então não precisa de "cmd /c"
claude mcp add --transport stdio --env ROCKET_MCP_WORKSPACE=C:\foguetes rocket-mcp -- C:\caminho\rocket-mcp\.venv\Scripts\python.exe -m rocket_mcp.server
```

```bash
# Linux/macOS
claude mcp add --transport stdio --env ROCKET_MCP_WORKSPACE=$HOME/foguetes rocket-mcp -- /caminho/rocket-mcp/.venv/bin/python -m rocket_mcp.server
```

Dentro do Claude Code, `/mcp` mostra o status da conexão. Para o Claude Desktop no Windows, a configuração equivalente vai em `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "rocket-mcp": {
      "command": "C:\\caminho\\rocket-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "rocket_mcp.server"],
      "env": { "ROCKET_MCP_WORKSPACE": "C:\\foguetes" }
    }
  }
}
```

---

## 7. Código inicial (testado)

### 7.1 `pyproject.toml`

```toml
[project]
name = "rocket-mcp"
version = "0.1.0"
description = "Servidor MCP unificado para openMotor (balística interna) e OpenRocket (trajetória)"
requires-python = ">=3.12,<3.14"  # scikit-fmm ainda não tem wheel para 3.14
license = "GPL-3.0-only"  # openMotor e OpenRocket são GPL-3.0
dependencies = [
    "mcp>=2.3,<3",        # 2.x: FastMCP virou MCPServer (mcp.server.mcpserver)
    "jpype1>=1.7",
    "numpy",
    "scipy",
    "scikit-fmm",
    "scikit-image",
    "pyyaml",
]

[project.scripts]
rocket-mcp = "rocket_mcp.server:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/rocket_mcp"]
```

### 7.2 `.gitignore`

```
.venv/
vendor/
__pycache__/
*.pyc
workspace_teste/motores/
```

### 7.3 `config/logback-mcp.xml`

```xml
<configuration>
  <appender name="ERR" class="ch.qos.logback.core.ConsoleAppender">
    <target>System.err</target>
    <encoder><pattern>%d{HH:mm:ss} %-5level %logger{20} - %msg%n</pattern></encoder>
  </appender>
  <root level="WARN"><appender-ref ref="ERR"/></root>
</configuration>
```

### 7.4 `scripts/preparar_vendor.py`

```python
"""Baixa as dependências externas fixadas para vendor/ (funciona em Windows, Linux e macOS).
Não precisa de git nem de compilador."""
import io
import shutil
import urllib.request
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
VENDOR = RAIZ / "vendor"

OM_COMMIT = "dcc7fd62045d55036451b833f60e16a786f4685d"  # openMotor 0.6.2 (26/09/2026), versão analisada
OM_URL = f"https://codeload.github.com/reilleya/openMotor/zip/{OM_COMMIT}"
OR_VERSION = "24.12"
OR_URL = (f"https://github.com/openrocket/openrocket/releases/download/"
          f"release-{OR_VERSION}/OpenRocket-{OR_VERSION}.jar")


def baixar(url: str) -> bytes:
    print("Baixando", url)
    with urllib.request.urlopen(url) as r:
        return r.read()


def main() -> None:
    VENDOR.mkdir(exist_ok=True)
    (RAIZ / "motores").mkdir(exist_ok=True)

    destino = VENDOR / "openMotor"
    if not destino.exists():
        with zipfile.ZipFile(io.BytesIO(baixar(OM_URL))) as z:
            z.extractall(VENDOR)
        shutil.move(str(VENDOR / f"openMotor-{OM_COMMIT}"), destino)

    jar = VENDOR / f"OpenRocket-{OR_VERSION}.jar"
    if not jar.exists():
        jar.write_bytes(baixar(OR_URL))
    print("vendor/ pronto:", *sorted(p.name for p in VENDOR.iterdir()))


if __name__ == "__main__":
    main()
```

### 7.5 `src/rocket_mcp/__init__.py`

Arquivo vazio.

### 7.6 `src/rocket_mcp/_perimetro_numpy.py`

```python
"""Substituto em NumPy puro para mathlib._find_perimeter_cy (extensão Cython do openMotor).

Motivo: compilar o Cython exige compilador C (no Windows, MSVC Build Tools). Esta versão
vetorizada reproduz o modo usado na SIMULAÇÃO (só o perímetro, sem contornos), com a mesma
tabela de casos do marching squares e a mesma exclusão de borda/raio externo do original.
Resultados idênticos ao Cython nos motores de teste do openMotor (ver doc).
O modo com contornos (including_contours=True) só é usado nas prévias gráficas da GUI.
"""
from functools import lru_cache

import numpy as np


@lru_cache(maxsize=8)
def _mascara_raio(n: int, m: int) -> np.ndarray:
    # Mesma exclusão do original: ignora quadrados a menos de 3 px do raio externo do grão
    r = np.arange(3, n - 4)[:, None]
    c = np.arange(3, m - 4)[None, :]
    return ~((n / 2) - 3 < np.hypot(r + 0.5 - n / 2, c + 0.5 - m / 2))


def _frac(a, b, level):
    d = b - a
    seguro = np.where(d == 0, 1.0, d)
    return np.where(d == 0, 0.0, (level - a) / seguro)


def _get_perimeter(array, level, vertex_connect_high, returning_contours):
    if returning_contours:
        raise NotImplementedError("Contornos (prévia gráfica) não suportados sem a extensão Cython")
    a = np.asarray(array, dtype=np.float64)  # MaskedArray -> dados brutos, como o memoryview do Cython
    n, m = a.shape
    acima = a > level
    case = (acima[3:n - 4, 3:m - 4] + 2 * acima[3:n - 4, 4:m - 3]
            + 4 * acima[4:n - 3, 3:m - 4] + 8 * acima[4:n - 3, 4:m - 3])
    ri, ci = np.nonzero((case != 0) & (case != 15) & _mascara_raio(n, m))
    if ri.size == 0:
        return 0.0, []
    k = case[ri, ci]
    ri, ci = ri + 3, ci + 3
    ul, ur, ll, lr = a[ri, ci], a[ri, ci + 1], a[ri + 1, ci], a[ri + 1, ci + 1]
    top, bottom = _frac(ul, ur, level), _frac(ll, lr, level)
    left, right = _frac(ll, ul, level), _frac(lr, ur, level)
    h = np.hypot
    tl, tr = h(top, 1 - left), h(1 - top, 1 - right)
    lb, br = h(left, bottom), h(1 - bottom, right)
    horiz, vert = h(right - left, 1), h(top - bottom, 1)
    seg = np.select(
        [np.isin(k, (1, 14)), np.isin(k, (2, 13)), np.isin(k, (3, 12)), np.isin(k, (4, 11)),
         np.isin(k, (5, 10)), np.isin(k, (7, 8)), k == 6, k == 9],
        [tl, tr, horiz, lb, vert, br, tr + lb, tl + br],
    )
    return float(seg.sum()), []
```

### 7.7 `src/rocket_mcp/om_bridge.py`

```python
"""Ponte com o openMotor: usa apenas motorlib (sem PyQt)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

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
from motorlib.simResult import SimAlertLevel  # noqa: E402


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
    if not {"data", "type", "version"} <= raw.keys():
        raise ValueError("Arquivo .ric inválido (faltam data/type/version)")
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


def resumo(motor, r) -> dict:
    sucesso = r.success and not r.getAlertsByLevel(SimAlertLevel.ERROR)
    out = {
        "sucesso": sucesso,
        "alertas": [
            {"nivel": a.level.name, "descricao": a.description, "local": a.location}
            for a in r.alerts
        ],
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
```

### 7.8 `src/rocket_mcp/or_bridge.py`

```python
"""Ponte com o OpenRocket 24.12 via JPype. Uma JVM por processo; todo acesso serializado por LOCK."""
from __future__ import annotations

import threading
from pathlib import Path

import jpype
import jpype.imports
from mcp.server.mcpserver.exceptions import ToolError  # mensagem chega ao LLM (outras exceções viram erro genérico)

RAIZ = Path(__file__).resolve().parents[2]
JAR = RAIZ / "vendor" / "OpenRocket-24.12.jar"
LOGBACK = RAIZ / "config" / "logback-mcp.xml"

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
    for eng in sorted(dir_motores.glob("*.eng")):
        registrar_motor(eng)


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
        return motor


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
            if d_motor > m["diametro_interno_mm"] + 0.1:
                avisos.append(f"Motor de {d_motor:.1f} mm NÃO cabe na montagem '{m['nome']}' "
                              f"({m['diametro_interno_mm']:.1f} mm)")
            if l_motor > m["comprimento_mm"]:
                avisos.append(f"Motor ({l_motor:.0f} mm) mais longo que a montagem '{m['nome']}' "
                              f"({m['comprimento_mm']:.0f} mm): haverá overhang")
            mc = MotorConfiguration(m["componente"], fcid)
            mc.setMotor(motor)
            mc.setEjectionDelay(float(atraso_s))
            m["componente"].setMotorConfig(mc, fcid)
    return avisos


def configurar(sim, *, trilho_m=None, angulo_trilho_graus=None, vento_medio_ms=None, altitude_m=None):
    """Unidades do OpenRocket: SI e RADIANOS."""
    import math

    o = sim.getOptions()
    if trilho_m is not None:
        o.setLaunchRodLength(float(trilho_m))
    if angulo_trilho_graus is not None:
        o.setLaunchRodAngle(math.radians(angulo_trilho_graus))
    if vento_medio_ms is not None:
        o.setWindSpeedAverage(float(vento_medio_ms))
    if altitude_m is not None:
        o.setLaunchAltitude(float(altitude_m))


def simular(sim) -> dict:
    with LOCK:
        sim.simulate()
        d = sim.getSimulatedData()
        return {
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
```

### 7.9 `src/rocket_mcp/server.py`

```python
"""Servidor MCP unificado: openMotor (balística interna) + OpenRocket (trajetória)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import anyio
import anyio.from_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import om_bridge as om
from . import or_bridge as orb

logging.basicConfig(stream=sys.stderr, level=logging.INFO)  # nunca stdout
log = logging.getLogger("rocket-mcp")

WORKSPACE = Path(os.environ.get("ROCKET_MCP_WORKSPACE", Path.cwd())).resolve()
MOTORES = WORKSPACE / "motores"

mcp = MCPServer(
    name="rocket-mcp",
    instructions=(
        "Ferramentas de projeto de foguetes. Unidades SI salvo indicação no nome do parâmetro. "
        "Caminhos são relativos ao workspace. Fluxo típico: simular_motor -> exportar_motor_eng -> "
        "simular_voo (ou projetar_e_voar para tudo de uma vez)."
    ),
)


def _caminho(rel: str) -> Path:
    """Impede acesso fora do workspace (o LLM escolhe os caminhos)."""
    p = (WORKSPACE / rel).resolve()
    if not p.is_relative_to(WORKSPACE):
        raise ToolError(f"Caminho fora do workspace: {rel}")
    if not p.exists() and p.suffix in (".ric", ".ork", ".eng"):
        raise ToolError(f"Arquivo não encontrado: {rel}")
    return p


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
    return res | {"arquivo_eng": str(eng.relative_to(WORKSPACE)), "nome_motor": nome}


@mcp.tool()
def abrir_foguete(arquivo_ork: str) -> dict:
    """Lê um projeto do OpenRocket (.ork): nome, simulações existentes e montagens de motor
    (diâmetro interno, comprimento e motor atual de cada uma)."""
    doc, avisos = orb.carregar_ork(_caminho(arquivo_ork))
    sims = list(doc.getSimulations())
    return {
        "foguete": str(doc.getRocket().getName()),
        "avisos_carregamento": avisos,
        "simulacoes": [{"indice": i, "nome": str(s.getName())} for i, s in enumerate(sims)],
        "montagens": [{k: v for k, v in m.items() if k != "componente"}
                      for m in (orb.montagens(doc, sims[0]) if sims else [])],
    }


@mcp.tool()
def simular_voo(arquivo_ork: str, indice_simulacao: int = 0, motor_eng: str | None = None,
                atraso_ejecao_s: float | None = None, montagem: str | None = None,
                trilho_m: float | None = None, angulo_trilho_graus: float | None = None,
                vento_medio_ms: float | None = None, altitude_lancamento_m: float | None = None,
                salvar_como: str | None = None) -> dict:
    """Simula o voo de uma simulação do .ork. Opcionalmente troca o motor por um .eng
    (caminho no workspace, ex.: 'motores/F46-OM.eng') e ajusta trilho, ângulo, vento e altitude.
    Retorna apogeu, velocidades, Mach, atraso ótimo de ejeção e avisos.
    O arquivo original só é alterado se salvar_como for informado."""
    doc, avisos = orb.carregar_ork(_caminho(arquivo_ork))
    sim = doc.getSimulations().get(indice_simulacao)
    avisos_val = []
    if motor_eng:
        motor = orb.registrar_motor(_caminho(motor_eng))
        avisos_val = orb.trocar_motor(doc, sim, motor, atraso_ejecao_s or 0.0, montagem)
    orb.configurar(sim, trilho_m=trilho_m, angulo_trilho_graus=angulo_trilho_graus,
                   vento_medio_ms=vento_medio_ms, altitude_m=altitude_lancamento_m)
    res = orb.simular(sim)
    if salvar_como:
        orb.salvar_ork(doc, _caminho(salvar_como))
    return res | {"avisos_validacao": avisos_val, "avisos_carregamento": avisos}


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
```

### 7.10 `teste_cliente.py`

```python
"""Teste ponta a ponta: sobe o servidor via stdio e chama as tools como um cliente MCP faria."""
import asyncio, os, sys, time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Workspace com um foguete.ork e um motor.ric (ex.: copie de vendor/openMotor/test/data/regression/tiny/motor.ric)
WORKSPACE = os.environ.get("ROCKET_MCP_WORKSPACE", os.path.abspath("workspace_teste"))

async def main():
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "rocket_mcp.server"],
        env={**os.environ, "ROCKET_MCP_WORKSPACE": WORKSPACE})  # herda JAVA_HOME, SYSTEMROOT (Windows)...
    t = time.time()
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize(); print(f"initialize: {time.time()-t:.1f}s")
        print("tools:", [x.name for x in (await s.list_tools()).tools])
        prog = []
        async def cb(p, total, msg): prog.append(p)
        for nome, args, kw in [
            ("simular_motor", {"arquivo_ric": "motor.ric"}, {"progress_callback": cb}),
            ("abrir_foguete", {"arquivo_ork": "foguete.ork"}, {}),
            ("projetar_e_voar", {"arquivo_ric": "motor.ric", "arquivo_ork": "foguete.ork", "diametro_mm": 29,
              "comprimento_mm": 150, "massa_hardware_kg": 0.12, "atraso_ejecao_s": 5}, {}),
            ("simular_voo", {"arquivo_ork": "../etc/passwd"}, {}),
        ]:
            t = time.time(); res = await s.call_tool(nome, args, **kw)
            txt = res.content[0].text if res.content else ""
            print(f"\n== {nome} ({time.time()-t:.1f}s) erro={res.is_error}\n{txt[:1500]}")
        print("\nnotificações de progresso recebidas:", len(prog), "última:", prog[-1] if prog else None)

asyncio.run(main())
```

---

## 8. Validação realizada

O ambiente de teste foi Linux com Python 3.12.3, OpenJDK 21.0.10, `mcp` 2.3.0, JPype 1.7.1, numpy 2.4/2.5, scipy 1.17, scikit-image 0.26 e scikit-fmm 2025.6.23. O teste partiu de um venv limpo, com `vendor/` baixado pelo script e **sem** a extensão Cython compilada.

Saída do `teste_cliente.py`:

```
initialize: 1.7s
tools: ['simular_motor', 'exportar_motor_eng', 'abrir_foguete', 'simular_voo', 'projetar_e_voar']
== simular_motor (0.9s) erro=False        → F46, 68,81 N·s, Isp 201,6 s, Pmax 5,70 MPa, 0 alertas
== abrir_foguete (10.3s) erro=False       → 5 simulações; montagem "Inner Tube" 18 mm × 75 mm, motor A8-3
                                            (10 s porque a JVM ainda estava subindo em paralelo)
== projetar_e_voar (3.8s) erro=False      → apogeu ≈1016 m, vmax 296 m/s, Mach 0,87
                                            avisos_validacao: "Motor de 29.0 mm NÃO cabe na montagem 'Inner Tube' (18.0 mm)",
                                                              "... mais longo que a montagem ... haverá overhang"
                                            avisos: "Recovery device deployment at high speed (83.9 m/s)"
== simular_voo (0.0s) erro=True           → "Caminho fora do workspace: ../etc/passwd"
notificações de progresso recebidas: 18
```

O resource `openmotor://propelentes` também foi lido com sucesso (20 propelentes).

Outros testes feitos:

- **Ida e volta `.ric`:** `salvar_ric()` seguido de `carregar_ric()` reproduz o mesmo motor (O4204). **Não verificado:** se o arquivo gerado abre na GUI do openMotor.
- **Ida e volta `.ork`:** salvar e recarregar sem `addMotor` perde o motor. Com `addMotor`, o motor é preservado e a simulação dá resultado idêntico.
- **Equivalência Cython × NumPy:** erro relativo máximo de 7×10⁻¹⁵ no perímetro, e simulações idênticas (X Core, Moon Burner, Finocyl, BATES).
- **Wheels Windows no PyPI** (consultado em 16/10/2026): existem para jpype1 (cp310–cp314), scikit-fmm (cp310–cp313), scikit-image (cp311–cp314), numpy (cp312+) e scipy (cp312+).

---

## 9. Riscos e pontos não verificados

1. **Nada foi executado em Windows.** As escolhas (fallback NumPy, `encoding="utf-8"`, `pathlib`, script de download em Python puro, herança do ambiente no teste) foram feitas pensando em Windows, mas é a primeira coisa a validar. Pontos de atenção:
   - o JPype precisa achar a JVM (`JAVA_HOME`);
   - caminhos com espaços no `-Dlogback.configurationFile`;
   - antivírus atrasando o primeiro boot da JVM.
2. **Timeout de inicialização do host.** O `initialize` responde em cerca de 2 s, mas a primeira tool do OpenRocket pode esperar até cerca de 10 s pela JVM. `garantir()` espera até 120 s.
3. **A API Java pode mudar.** O branch principal (26.xx) já difere da 24.12, como mostra o `initialize`. Fixe o JAR e revalide ao atualizar.
4. **A migração de `.ric` é mínima.** Arquivos v0.2 e v0.3 (propelente sem `tabs`) vão falhar.
5. **`setWindSpeedAverage`** foi assumido a partir do código-fonte e não teve o efeito validado numericamente. A 24.12 tem vento multinível, e é preciso confirmar qual modelo de vento a simulação usa.
6. **Licença:** openMotor e OpenRocket são GPL-3.0. Se o servidor for distribuído, trate-o como GPL-3.0 (o `pyproject.toml` já declara isso).

---

## 10. Roadmap para o Claude Code

Ordem sugerida, cada item com critério de aceitação:

1. **Reproduzir a linha de base** (§6 e §8).
   *Aceite:* a saída do `teste_cliente.py` equivale à da §8, inclusive no Windows.
2. **Testes automatizados com pytest** a partir do `teste_cliente.py`: um teste por tool, mais segurança de caminho e o "motor que não cabe".
   *Aceite:* `pytest` verde.
3. **Migrações completas de `.ric`**: portar `doMigration` e as funções `migrate*` de `uilib/fileIO.py` (sem PyQt; a migração 0.2→0.3 usa `QApplication` só para ler preferências, então use os defaults).
   *Aceite:* todos os `.ric` em `vendor/openMotor/test/data/**` carregam sem alertas espúrios de Mach ou separação.
4. **`criar_motor` e `editar_motor`**: montar o motor a partir de grãos, tubeira, nome do propelente (do resource) e config, validando com `getGeometryErrors()`; salvar com `salvar_ric()`; editar um parâmetro por caminho (ex.: `grains[0].coreDiameter`).
   *Aceite:* o LLM consegue iterar "aumente o núcleo até Pmax < 5 MPa".
5. **`previa_motor`** usando `motor.getQuickResults()`: Kn inicial, carregamento volumétrico e razão porta/garganta sem simular.
6. **Margem estática e CG/CP** no OpenRocket. Investigar o `BarrowmanCalculator` e a calculadora de massa (`masscalc`), ou os canais de estabilidade do `FlightDataBranch`.
   *Aceite:* `abrir_foguete` passa a retornar a margem estática (calibres) na decolagem.
7. **Edição de componentes do foguete** com uma lista curada: aletas (corda, envergadura, número), comprimentos de tubo, massa de lastro e paraquedas.
8. **Séries temporais**: exportar CSV de `FlightDataBranch` e de `SimulationResult.getCSV()`, devolvendo o caminho do arquivo e não os dados brutos, para não estourar o contexto do LLM.
9. **Validações de projeto adicionais** em `simular_voo`:
   - velocidade de saída do trilho menor que ~15 m/s;
   - abertura de recuperação muito antes ou depois do apogeu (comparar com `atraso_otimo_s`);
   - Mach acima de 0,8;
   - motor com overhang.
10. **Robustez**:
    - localizar `vendor/` e `config/` por variável de ambiente (`ROCKET_MCP_HOME`), permitindo instalação não editável;
    - tornar configurável o timeout de `garantir()`;
    - converter exceções Java em `ToolError` legíveis.
11. *(Opcional)* **Compilar a extensão Cython** para ganhar ~1,7× em grãos FMM. Com compilador disponível, compile `vendor/openMotor/mathlib/_find_perimeter_cy.pyx` fora do diretório do openMotor (para não ler o `pyproject.toml` dele) e confirme que `om_bridge.PERIMETRO == "cython"`.

---

## 11. Referências

- openMotor: https://github.com/reilleya/openMotor (commit `dcc7fd62`), com teoria de Sutton (*Rocket Propulsion Elements*) e Richard Nakka (https://www.nakka-rocketry.net/rtheory.html)
- OpenRocket: https://github.com/openrocket/openrocket · release 24.12 · documentação: https://openrocket.readthedocs.io
- SDK MCP Python 2.x, guia de migração (FastMCP → MCPServer): https://py.sdk.modelcontextprotocol.io/v2/migration/
- JPype: https://jpype.readthedocs.io
- Claude Code + MCP: https://docs.claude.com/en/docs/claude-code/overview
