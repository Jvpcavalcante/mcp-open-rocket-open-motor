# rocket-mcp

Servidor **MCP** (Model Context Protocol) que dá ao Claude acesso a dois programas de foguetemodelismo
experimental:

- **[openMotor](https://github.com/reilleya/openMotor)**: balística interna de motores sólidos (grãos,
  tubeira, propelente, curva de empuxo, pressão de câmara);
- **[OpenRocket 24.12](https://github.com/openrocket/openrocket)**: trajetória do foguete (apogeu,
  estabilidade, vento, recuperação, ponto de pouso).

Com ele você pede, em linguagem natural, coisas como *"crie um motor KNSB com 5 grãos BATES e simule"*,
*"voe o Hemera com esse motor e vento de 6 m/s"* ou *"qual a chance de o foguete sair instável do
trilho?"*. O Claude chama as ferramentas, roda as simulações de verdade e devolve os números.

> Os resultados são **simulações**. Confira com ensaios estáticos e dados de voo antes de decisões de
> segurança.

---

## Sumário

1. [O que dá para fazer](#o-que-dá-para-fazer)
2. [Requisitos](#requisitos)
3. [Instalação passo a passo](#instalação-passo-a-passo)
4. [Conectar ao Claude](#conectar-ao-claude)
5. [Primeiros passos](#primeiros-passos)
6. [Fluxos de trabalho comuns](#fluxos-de-trabalho-comuns)
7. [Convenções importantes](#convenções-importantes)
8. [Solução de problemas](#solução-de-problemas)
9. [Limitações conhecidas](#limitações-conhecidas)
10. [Para desenvolvedores](#para-desenvolvedores)

---

## O que dá para fazer

### Motores (openMotor)

| Ferramenta | O que faz |
|---|---|
| `listar_tipos_grao` | Catálogo de grãos (BATES, Finocyl, Star...), tubeira, configuração e propelentes, com unidades e faixas válidas |
| `criar_motor` | Cria um motor (`.ric`) a partir de propelente, grãos e tubeira; valida a geometria e devolve uma prévia (Kn, razão porta/garganta, massa) |
| `ler_motor` / `editar_motor` | Lê e altera parâmetros de um `.ric` (ex.: `grains[0].coreDiameter_mm`, `nozzle.throat_mm`), acrescenta ou remove grãos |
| `simular_motor` | Simula o motor: impulso, empuxo médio, Isp, pressão máxima, alertas |
| `exportar_motor_eng` | Gera a curva de empuxo `.eng` (formato RASP) para usar no OpenRocket |
| `exportar_series_motor` | Grava as séries no tempo (empuxo, pressão, Kn, vazão...) em CSV |

Recurso extra: `openmotor://propelentes`, a biblioteca de 20 propelentes do openMotor (KNSB, KNDX, RCS...).

### Foguete e voo (OpenRocket)

| Ferramenta | O que faz |
|---|---|
| `abrir_foguete` | Resumo de um `.ork`: simulações, montagens de motor, margem estática |
| `ler_foguete` | Árvore completa de componentes, com os parâmetros editáveis de cada um |
| `editar_foguete` | Altera aletas, ogiva, tubos, massas, paraquedas e eventos de abertura; cria e remove lastro. **Grava sempre em arquivo novo** |
| `simular_voo` | Simula o voo, opcionalmente com outro motor, vento (médio ou **perfil por altitude**), trilho, local e atmosfera. Devolve apogeu, velocidades, Mach, estabilidade, ponto de pouso, **segurança** (pressão dinâmica, acelerações, choque de abertura, descida, impacto, flutter das aletas) e alertas de projeto |
| `projetar_e_voar` | Atalho: simula o motor, exporta o `.eng` e voa o foguete com ele |

### Análise

| Ferramenta | O que faz |
|---|---|
| `varrer_parametro` | Tabela de resultados para vários valores de um parâmetro do foguete ou de uma condição de lançamento (vento, ângulo do trilho...) |
| `otimizar_parametro` | Procura o valor que atinge uma meta: *"envergadura da aleta para margem de 1,5 cal"*, *"ângulo do trilho para deriva mínima"*, *"garganta para pressão máxima de 6 MPa"* |
| `analisar_dispersao` | Monte Carlo: sorteia vento, trilho, massa, arrasto e impulso e devolve estatísticas, a **elipse de pouso de 95%** e a probabilidade de cada problema |
| `exportar_series_voo` | Grava as séries do voo (altitude, velocidade, Mach, estabilidade, posição...) em CSV |

### Dados reais

| Ferramenta | O que faz |
|---|---|
| `comparar_curvas_empuxo` | Compara curvas de empuxo (`.eng`, `.ric` simulado ou CSV de ensaio estático) |
| `calibrar_motor` | Ajusta o motor (eficiência, velocidade de queima...) para reproduzir um ensaio estático |
| `calibrar_voo` | Ajusta o fator de arrasto (e opcionalmente impulso e massa) para reproduzir o log de altitude do altímetro; o fator encontrado pode ser usado em todas as outras ferramentas |

### Relatório

| Ferramenta | O que faz |
|---|---|
| `gerar_relatorio` | HTML autocontido com gráficos, tabelas e alertas, opcionalmente com a seção do motor e a dispersão. Abre no navegador e imprime em PDF |

---

## Requisitos

| Item | Versão | Observação |
|---|---|---|
| Sistema | Windows 10/11, Linux ou macOS | Testado em Windows 11. A versão inicial também foi testada em Linux; as funções novas ainda não |
| Python | **3.12 ou 3.13** | O 3.14 ainda não funciona (falta o `scikit-fmm`) |
| Java (JDK) | **17 ou mais recente** | Testado com 21 e 25. O Java 11 **não** funciona |
| Git | qualquer | Para clonar o repositório |
| Espaço | ~400 MB | Ambiente Python + JAR do OpenRocket (83 MB) |
| Claude | Claude Code (CLI, VS Code ou JetBrains) ou Claude Desktop | |

Não é preciso compilador C nem a interface gráfica do OpenRocket ou do openMotor.

---

## Instalação passo a passo

### 1. Instale o Java

Instale o **Eclipse Temurin JDK 21** (ou mais recente) em <https://adoptium.net>. No Windows, marque a opção
**"Set JAVA_HOME variable"** no instalador.

Confira num terminal **novo** (terminais abertos antes da instalação não enxergam o `JAVA_HOME` novo):

```powershell
java -version        # deve mostrar 17 ou mais
echo $env:JAVA_HOME  # Windows (PowerShell)
```

```bash
java -version
echo $JAVA_HOME      # Linux/macOS
```

### 2. Instale o Python

Instale o **Python 3.12** em <https://www.python.org/downloads/>. No Windows, marque **"Add python.exe to
PATH"**.

### 3. Baixe o projeto

```bash
git clone <URL-DO-REPOSITÓRIO> mcp-open-rocket-motor
cd mcp-open-rocket-motor
```

### 4. Crie o ambiente e instale

**Windows (PowerShell):**

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python scripts\preparar_vendor.py
```

**Linux/macOS:**

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python scripts/preparar_vendor.py
```

O `preparar_vendor.py` baixa para a pasta `vendor/` o código do openMotor, numa versão fixa, e o JAR do
OpenRocket 24.12. Isso leva de 1 a 2 minutos.

> Instale em **modo editável** (`-e`), como acima. O servidor encontra `vendor/` e `config/` a partir da
> pasta do projeto.

### 5. Confira se funciona

```powershell
.venv\Scripts\python teste_cliente.py      # Windows
```

```bash
.venv/bin/python teste_cliente.py          # Linux/macOS
```

A saída esperada é parecida com esta (a última linha com `erro=True` é proposital: é o teste de segurança
que bloqueia caminhos fora do workspace):

```
initialize: 2.2s
tools: ['simular_motor', 'exportar_motor_eng', 'abrir_foguete', ...]
== simular_motor (0.5s) erro=False
== abrir_foguete (2.3s) erro=False
== projetar_e_voar (0.8s) erro=False
== simular_voo (0.0s) erro=True
notificações de progresso recebidas: 18
```

A primeira execução pode levar até ~10 s, enquanto o Python compila os módulos e o antivírus os examina.

### 6. Prepare o seu workspace

O **workspace** é a pasta onde ficam os seus arquivos de projeto. O servidor só lê e grava dentro dela.
Uma organização sugerida:

```
C:\foguetes\              (ou ~/foguetes)
├── hemera.ork            projetos do OpenRocket
├── motores\              motores .ric e curvas .eng
│   ├── notus.ric
│   └── Notus.eng
├── ensaios\              CSV de ensaios estáticos e logs de altímetro
└── relatorios\           relatórios gerados
```

- **Coloque os `.eng` dos seus motores em `motores/`.** O servidor os registra automaticamente no
  OpenRocket. Sem isso, um `.ork` que usa um motor próprio abre "sem motor".
- A pasta `workspace_teste/` do repositório tem exemplos prontos (`foguete.ork`, `motor.ric`) para os
  primeiros testes.

---

## Conectar ao Claude

Em todas as opções abaixo, troque os caminhos pelos da sua máquina:

- `<PROJETO>`: a pasta onde você clonou o repositório;
- `<WORKSPACE>`: a sua pasta de trabalho do passo 6;
- `<JAVA_HOME>`: a pasta do JDK (ex.: `C:\Program Files\Eclipse Adoptium\jdk-21.0.5.11-hotspot`).

O `JAVA_HOME` informado aqui tem prioridade sobre o do sistema. Isso é útil se a máquina tiver mais de um
Java instalado.

### Opção A: Claude Code, só neste projeto (arquivo `.mcp.json`)

O repositório já traz um `.mcp.json` na raiz, **com os caminhos da máquina de quem o criou**. Edite-o:

```json
{
  "mcpServers": {
    "rocket-mcp": {
      "type": "stdio",
      "command": "<PROJETO>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "rocket_mcp.server"],
      "env": {
        "ROCKET_MCP_WORKSPACE": "<WORKSPACE>",
        "JAVA_HOME": "<JAVA_HOME>"
      }
    }
  }
}
```

No JSON, cada `\` do Windows vira `\\`. No Linux/macOS, use `<PROJETO>/.venv/bin/python`.

Depois:
1. abra a pasta do projeto no Claude Code (no VS Code: *Developer: Reload Window*);
2. aprove o servidor `rocket-mcp` quando o Claude Code perguntar;
3. digite `/mcp`: o `rocket-mcp` deve aparecer como **connected**.

O `.mcp.json` precisa estar na **raiz** do projeto. Dentro de `docs/` ou de outra pasta, ele não é lido.

### Opção B: Claude Code, em todos os projetos (linha de comando)

```powershell
claude mcp add --scope user --transport stdio `
  --env ROCKET_MCP_WORKSPACE=<WORKSPACE> --env "JAVA_HOME=<JAVA_HOME>" `
  rocket-mcp -- <PROJETO>\.venv\Scripts\python.exe -m rocket_mcp.server
```

```bash
claude mcp add --scope user --transport stdio \
  --env ROCKET_MCP_WORKSPACE=<WORKSPACE> --env JAVA_HOME=<JAVA_HOME> \
  rocket-mcp -- <PROJETO>/.venv/bin/python -m rocket_mcp.server
```

### Opção C: Claude Desktop

Edite o arquivo de configuração (Windows: `%APPDATA%\Claude\claude_desktop_config.json`; macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`) e reinicie o Claude Desktop:

```json
{
  "mcpServers": {
    "rocket-mcp": {
      "command": "<PROJETO>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "rocket_mcp.server"],
      "env": { "ROCKET_MCP_WORKSPACE": "<WORKSPACE>", "JAVA_HOME": "<JAVA_HOME>" }
    }
  }
}
```

---

## Primeiros passos

Com o servidor conectado, converse normalmente. Alguns pedidos para começar, usando os arquivos de
`workspace_teste/`:

1. *"Abra o foguete.ork e me diga a margem estática."*
2. *"Simule o motor.ric."*
3. *"Voe o foguete.ork com o motor.ric num case de 29 × 150 mm com 120 g de hardware e atraso de 5 s."*
4. *"Gere um relatório do voo do foguete.ork em relatorios/teste.html."*

Na primeira ferramenta do OpenRocket depois de abrir o Claude, a resposta pode demorar alguns segundos a
mais: o Java está iniciando em segundo plano.

---

## Fluxos de trabalho comuns

### Projetar um motor

> *"Quais parâmetros um grão Finocyl aceita?"*
> *"Crie em motores/m1.ric um motor KNSB com 3 grãos BATES de 54 × 100 mm, núcleo de 20 mm, garganta de
> 12 mm e saída de 30 mm, e simule."*
> *"A pressão máxima passou de 6 MPa. Ache a garganta que deixa a pressão em 5,5 MPa e salve como
> motores/m1_v2.ric."*
> *"Exporte o .eng do m1_v2 para um case de 64 × 400 mm com 600 g de hardware."*

### Simular o voo com o seu motor

> *"Voe o hemera.ork com motores/m1_v2.eng, trilho de 6 m a 5°, vento de 4 m/s vindo do norte."*
> *"Repita com vento de 10 m/s a 1000 m de altitude."* (perfil de vento por altitude)

### Ajustar o foguete

> *"Mostre os componentes do hemera.ork."*
> *"Aumente a envergadura das aletas para 200 mm e salve como hemera_aletas200.ork. Como fica a margem?"*
> *"Qual envergadura deixa a margem estática em 1,8 cal?"*

### Avaliar o risco

> *"Rode 100 voos de dispersão do hemera.ork e me diga a chance de a estabilidade na saída do trilho
> ficar abaixo de 1 calibre."*
> *"Varra o vento de 0 a 12 m/s e mostre a estabilidade na saída do trilho e a distância de pouso."*
> *"Qual ângulo do trilho minimiza a deriva com vento de 6 m/s?"*
> *"As aletas são de alumínio: qual a margem de flutter?"*

### Usar dados reais

> *"Compare o ensaio estático ensaios/estatico_1.csv com o motores/m1_v2.ric."*
> *"Calibre o m1_v2.ric para reproduzir o ensaio, ajustando eficiência e velocidade de queima."*
> *"Calibre o arrasto do hemera.ork com o log do altímetro ensaios/voo_1.csv."*

O CSV de ensaio estático precisa de uma coluna de tempo e uma de empuxo (ex.: `time (s),force (n)`).
Unidades como ms, lbf, kgf, psi e MPa são reconhecidas pelo cabeçalho. O log do altímetro precisa de tempo
e altitude (m ou ft).

### Documentar

> *"Gere um relatório do hemera.ork com o motor m1_v2, incluindo 50 voos de dispersão, em
> relatorios/hemera_m1.html."*

---

## Convenções importantes

- **Caminhos** são relativos ao workspace (ex.: `motores/m1.ric`). Nada fora dele é lido ou gravado.
- **Arquivos originais não são sobrescritos.** Edições e resultados vão para arquivos novos; para gravar
  por cima, peça explicitamente ("pode sobrescrever"). A exceção é o `editar_motor`, que edita o `.ric` no
  próprio arquivo, a menos que você indique outro.
- **Unidades** estão no nome do parâmetro: `_mm`, `_m`, `_kg`, `_deg`/`_graus`, `_MPa`, `_ms` (m/s), `_s`.
- **Direções:** o vento é informado **de onde vem** (90° = vento de leste, e o foguete deriva para oeste);
  o trilho, **para onde aponta**. 0° = norte, 90° = leste.
- **"Lançar contra o vento":** muitos `.ork` vêm com essa opção ligada, o que faz o OpenRocket apontar o
  trilho contra o vento e ignorar a direção do trilho. Informar a direção do trilho desliga a opção, e a
  resposta avisa.
- **Resultados repetíveis:** as simulações usam uma semente fixa por padrão. Para variar as rajadas como
  na interface do OpenRocket, peça uma semente aleatória.
- **Massas sobrescritas:** se o `.ork` sobrescreve a massa de um componente ou módulo, mudar suas dimensões
  não muda a massa do foguete. A resposta avisa, e a massa sobrescrita também pode ser editada.

---

## Solução de problemas

| Sintoma | Causa provável e solução |
|---|---|
| `incorrect Java version` ou `Falha ao iniciar o OpenRocket` | O Java é anterior ao 17. Instale o 21 e informe `JAVA_HOME` na configuração do MCP |
| O `rocket-mcp` não aparece no `/mcp` | O `.mcp.json` não está na raiz do projeto, ou a janela não foi recarregada. Recarregue o VS Code e aprove o servidor |
| `failed` no `/mcp` | Caminho errado do `python.exe` (precisa ser o da `.venv`) ou do `JAVA_HOME`. Rode o `teste_cliente.py` para ver o erro completo |
| `OpenRocket ainda inicializando` | O Java ainda está subindo (normal nos primeiros ~10 s). Peça de novo |
| O `.ork` abre "sem motor" (`No motor with designation ...`) | O `.eng` do motor não está em `<WORKSPACE>/motores/`. Copie-o para lá |
| `Caminho fora do workspace` | O arquivo está fora de `ROCKET_MCP_WORKSPACE`. Mova-o para dentro ou mude o workspace |
| As alterações no código não aparecem | O servidor conectado roda a versão antiga. Use `/mcp` para reconectar ou recarregue a janela |
| Primeira execução muito lenta | Compilação dos módulos Python e varredura do antivírus. Melhora nas próximas |

---

## Limitações conhecidas

- **O openMotor não simula queima erosiva.** Motores com fluxo de massa alto no núcleo podem ter, na
  realidade, um pico inicial que a simulação não mostra. O alerta de fluxo de massa indica o risco.
- O grão **Custom (DXF)** não é suportado na criação de motores.
- O **flutter** só é calculado para aletas trapezoidais. Na tabela de materiais há só metais; para
  compósitos e madeira, informe o módulo de cisalhamento medido. A fórmula usada é a da NACA TN-4197,
  ~30% mais conservadora que a planilha do *Peak of Flight* #291 da Apogee.
- A incerteza de massa na dispersão só funciona em foguetes de **um estágio**.
- Ainda não se verificou se os `.ric` gravados pelo MCP abrem na interface gráfica do openMotor.
- O OpenRocket fixado é a versão **24.12**. Outras versões exigem revalidação.

---

## Para desenvolvedores

```
mcp-open-rocket-motor/
├── src/rocket_mcp/
│   ├── server.py          ferramentas MCP (ponto de entrada)
│   ├── om_bridge.py       ponte com o openMotor (motorlib)
│   ├── or_bridge.py       ponte com o OpenRocket (JPype): JVM, voo, vento, segurança
│   ├── or_foguete.py      leitura e edição de componentes do foguete, estabilidade estática
│   ├── analise.py         otimização, estatística, curvas e logs (sem JVM)
│   ├── relatorio.py       relatório HTML e gráficos
│   └── _perimetro_numpy.py  substituto da extensão Cython do openMotor
├── tests/                 pytest (~160 testes)
├── teste_cliente.py       teste de ponta a ponta pelo protocolo MCP
├── scripts/preparar_vendor.py
├── config/logback-mcp.xml
├── docs/
│   ├── rocket-mcp-analise-e-codigo.md  especificação original e armadilhas
│   ├── DIARIO.md                       histórico de decisões e problemas encontrados
│   └── AIRBRAKE.md                     plano de simulação de airbrake (não implementado)
└── workspace_teste/       arquivos de exemplo
```

Rodar os testes (cerca de 90 s; precisam do Java 17+):

```bash
.venv/bin/python -m pytest          # Linux/macOS
.venv\Scripts\python -m pytest      # Windows
```

Antes de cada commit, rode `pytest` e `teste_cliente.py`. As regras do projeto, como "nada no stdout além
do JSON-RPC" e "todo acesso ao OpenRocket passa pelo LOCK", estão no `CLAUDE.md`. As armadilhas do
OpenRocket e do openMotor encontradas até aqui estão no `docs/DIARIO.md`.

---

## Licença

O openMotor e o OpenRocket são distribuídos sob a **GPL-3.0**, e este projeto os usa diretamente, por isso
o `pyproject.toml` declara `GPL-3.0-only`. Se for distribuir o servidor, siga os termos da GPL-3.0.
