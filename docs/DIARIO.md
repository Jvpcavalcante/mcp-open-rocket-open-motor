# Diário do projeto

## 2026-10-17 — Linha de base no Windows (roadmap item 1)

**Feito:** arquivos da §7 criados exatamente como no documento (extraídos por script dos blocos de código;
contagem de linhas confere), setup da §6.2 executado e `teste_cliente.py` rodado. **Nenhuma lógica alterada.**

**Resultado:** a saída equivale à da §8. Critério de aceitação do item 1 do roadmap atendido no Windows,
com as ressalvas abaixo.

### Ambiente

| Item | Windows (este teste) | Linux (§8) |
|---|---|---|
| SO | Windows 11 Education 10.0.26200, locale pt-BR (cp1252) | Linux |
| Python | 3.12.6 (python.org, `py -3.12`) | 3.12.3 |
| Java | Temurin 21.0.12.1+1 (portátil, ver P1) | OpenJDK 21.0.10 |
| mcp / jpype1 | 2.3.0 / 1.7.1 | 2.3.0 / 1.7.1 |
| numpy / scipy | 2.5.4 / **1.18.1** | 2.4–2.5 / 1.17 |
| scikit-image / scikit-fmm | 0.26.0 / 2025.6.23 | 0.26 / 2025.6.23 |
| Perímetro openMotor | numpy (sem Cython) | numpy |

### Comparação com a §8

| Etapa | §8 (Linux) | Windows | Situação |
|---|---|---|---|
| `initialize` | 1,7 s | 2,2 s (8,4 s na 1ª execução após instalar, ver P5) | ok |
| `tools` | 5 tools | idênticas | ok |
| `simular_motor` | F46, 68,81 N·s, Isp 201,6 s, Pmax 5,70 MPa, 0 alertas | idêntico (Pmax 5,699) | ok |
| `abrir_foguete` | 5 sims; Inner Tube 18 × 75 mm, A8-3; 10,3 s | idêntico; 2,9–3,1 s | ok |
| `projetar_e_voar` | apogeu ≈1016 m, vmax 296 m/s, Mach 0,87 | 1016,1 / 1016,0 m; 295,6 m/s; Mach 0,871 | ok (ver D3) |
| `avisos_validacao` | não cabe (29 > 18 mm) + overhang | idênticos | ok |
| `avisos` | "Recovery device deployment at high speed (83.9 m/s)" | "Implantação recuperação do dispositivo a alta velocidade (83,9 m/s):  \"Parachute\"" | **diferente (D1)** |
| `arquivo_eng` | `motores/F46-OM.eng` (presumido) | `motores\\F46-OM.eng` | **diferente (D2)** |
| `simular_voo ../etc/passwd` | erro "Caminho fora do workspace" | idêntico | ok |
| Progresso | 18 notificações | 18 | ok |

Outros números do voo no Windows: tempo até apogeu 7,55 s, atraso ótimo 12,1 s, saída do trilho 19,7 m/s,
abertura a 83,96 m/s, impacto 7,18 m/s, voo de 151,7 s (consistentes com a §3.5).

### Problemas e diferenças

**P1. A máquina só tinha Java 8 e 11, e o `JAVA_HOME` global apontava para o JDK 11.**
Com Java 11, o servidor sobe e as tools do openMotor e o sandbox funcionam, mas toda tool do OpenRocket
falha com `ToolError`: `Falha ao iniciar o OpenRocket: ImportError("Unable to import
'info.openrocket.core.plugin.PluginModule' due to incorrect Java version")`. A mensagem é clara para o LLM.
*Contorno usado:* Temurin 21 em zip portátil em `%USERPROFILE%\.jdks\jdk-21.0.12.1+1` (sem admin) e `JAVA_HOME`
definido **só no processo do teste**, para não quebrar o que depende do Java 11. Ao registrar no Claude Code,
passar `--env JAVA_HOME=...` junto com `ROCKET_MCP_WORKSPACE`. O caminho do JDK tem espaço e "ã"
(`João Victor`), e o JPype funcionou mesmo assim.
*Atualização (mesmo dia):* o João instalou o **Temurin JDK 25.0.4.1** (`C:\Program Files\Eclipse Adoptium\
jdk-25.0.4.101-hotspot`), e o `JAVA_HOME` da máquina passou a apontar para ele. O `teste_cliente.py` passa com
Java 25, com resultados idênticos (apogeu 1016,1 m, Mach 0,871, 18 notificações). A diferença é que o Java 25
imprime no stderr avisos de `--enable-native-access` (JPype chama `System::load`) e de
`sun.misc.Unsafe::invokeCleaner` (ClassGraph, dentro do JAR). Os avisos são inofensivos e não vão para o stdout,
mas o segundo indica que uma versão futura do Java pode quebrar o ClassGraph do OpenRocket 24.12. Processos
abertos antes da instalação (VS Code, terminais) continuam enxergando o `JAVA_HOME` antigo (JDK 11) até
serem reiniciados.

**P2. O servidor não termina quando o stdin fecha (EOF).** Reproduzido duas vezes fora do `teste_cliente.py`:
depois do EOF, o processo fica preso. O `jstack` mostra `DestroyJavaVM` esperando, e a única thread Java
não-daemon é a `"main"`. *Hipótese (não verificada):* a JVM foi iniciada na thread Python `jvm-boot`
(`iniciar_em_segundo_plano`), que vira a "main" da JVM e termina sem se desanexar. No encerramento, o JPype
chama `DestroyJavaVM`, que espera essa thread para sempre. O `teste_cliente.py` não percebe isso porque o
cliente stdio do SDK mata o processo ao sair; os hosts também costumam matar. Risco: processos órfãos se o
host só fechar o pipe. Provavelmente não é específico do Windows (não verificado no Linux). **Não corrigido**,
porque a instrução era não alterar a lógica.

**P3. O `python.exe` do venv no Windows é um *launcher*.** Ele cria um processo filho com o Python base
(`.venv\Scripts\python.exe` → `C:\Python312\python.exe -m rocket_mcp.server`). Não é um shim `.cmd`, então a
§6.4 continua certa ao dispensar `cmd /c`, mas o PID que o host enxerga não é o do servidor. O
`taskkill /T` mata a árvore inteira. Combinado com P2, um kill só do pai pode deixar o filho órfão
(não testado).

**P4. Fim de linha CRLF.** `Path.write_text` em modo texto converte `\n` em `\r\n` no Windows:
- o `.eng` gerado sai em CRLF, e o OpenRocket o carregou sem problema;
- o `salvar_ric()` também vai gravar CRLF (não testado contra a GUI do openMotor);
- o stdout JSON-RPC do SDK também sai com `\r\n` (o JSON tolera; 2 de 2 linhas válidas).

**P5. A primeira execução após a instalação é lenta.** O `initialize` levou 8,4 s na 1ª execução e 2,2 s nas
seguintes. O `om_bridge` importa motorlib/scipy/scikit-image **antes** do `mcp.run`, então a compilação de
`.pyc` e a varredura do antivírus (risco já previsto na §9) entram no tempo do handshake.

**P6. `unzip -p` não existe no Windows.** A §6.2 manda abrir o OpenRocket e usar "Save As". Em vez disso,
extraí `datafiles/examples/A simple model rocket.ork` do JAR com o `zipfile` do Python (uma linha). A
alternativa poderia entrar na §6.2.

**P7. Encoding do console.** Na saída de `pip` e do servidor, o PowerShell 5.1 embrulha o stderr de
executáveis nativos em `NativeCommandError` (cosmético, exit code 0). O Python escreve em pipes com
`cp1252` (locale pt-BR); por enquanto os textos impressos pelo teste cabem em cp1252.

**D1. Os avisos do OpenRocket saem no idioma e no formato numérico do locale da JVM.** No Windows pt-BR, o
aviso veio traduzido, com tradução ruim ("Implantação recuperação do dispositivo..."), com vírgula decimal e
com o nome do componente (`"Parachute"`). A saída que o LLM recebe muda conforme a máquina. Poderia ser
fixada com `-Duser.language=en -Duser.country=US` no `startJVM` (mudança de lógica, não aplicada).

**D2. `arquivo_eng` usa barra invertida.** `str(eng.relative_to(WORKSPACE))` devolve `motores\F46-OM.eng`.
Funciona no Windows, inclusive quando `projetar_e_voar` repassa o caminho para `simular_voo`, mas o retorno
muda entre plataformas. `as_posix()` resolveria (não aplicado).

**D3. O apogeu varia ±0,1 m entre execuções** (1016,1 e 1016,0). A causa provável é a semente aleatória da
turbulência do vento no OpenRocket. Os testes do pytest (roadmap 2) devem usar tolerância ou fixar a semente.

### Observações menores
- O `preparar_vendor.py` cria `motores/` na **raiz do projeto**, mas o servidor usa
  `<WORKSPACE>/motores`. Essa pasta na raiz fica vazia e sem uso.
- Ao abrir o `.ork` de exemplo, o logback emite 19 linhas WARN no stderr
  (`FlightDataBranchHandler - Could not find the flight data type ...`). Elas vão corretamente para o stderr.
  A configuração `logback-mcp.xml` foi aplicada (o formato do padrão confere) mesmo com caminho Windows.
- O motorlib emite `RuntimeWarning` do numpy (`nozzle.py:47`, `fsolve`) no stderr durante a simulação do
  motor `tiny`. É inofensivo e vem do código original.
- Risco da §9 "caminho com espaço no `-Dlogback.configurationFile`": **não testado**, porque o projeto está
  em `C:\codiguins\mcp-open-rocket-motor`, sem espaço.
- O `CLAUDE.md` cita `docs/ANALISE.md`, mas o arquivo existente é `docs/rocket-mcp-analise-e-codigo.md`.

### Próximo passo
Roadmap item 2 (pytest). Antes, decidir se P2, D1 e D2 entram como correções (são mudanças de lógica).

---

## 2026-10-17 (cont.) — pytest, correções e criação de motores (roadmap itens 2 e 4)

Ordem combinada com o João: primeiro testes e correções, depois `criar_motor`/`editar_motor`. Os parâmetros
dos motores ficam com os **nomes do motorlib em inglês + sufixo de unidade** (decisão do João).

**Estado:** `pytest`: **47 passed** (~14 s, Java 25). `teste_cliente.py` igual à §8, agora com 9 tools.

### Roadmap 2: testes (`tests/`)
- `conftest.py` monta um workspace temporário a partir de `vendor/` (motor `tiny` e `.ork` extraído do JAR),
  então os testes não dependem do `workspace_teste`. A JVM sobe uma vez por sessão, em segundo plano.
- As tools são testadas pelo protocolo MCP com o servidor **em memória** (`mcp.Client(server.mcp)`), sem
  subprocesso. Só `test_stdio.py` sobe o servidor de verdade: verifica que o stdout tem só JSON-RPC e que
  o processo sai sozinho após o EOF.
- `pip install -e .[dev]` instala o pytest. O `pyproject.toml` desliga o `faulthandler` do pytest, porque a
  JVM usa access violations de propósito e o pytest as reportava como "Windows fatal exception".

### Correções
- **P2 resolvido:** a thread `jvm-boot` agora chama `java.lang.Thread.detach()` ao terminar, como o
  launcher do Java faz antes do `DestroyJavaVM`. A hipótese se confirmou: o processo sai com código 0
  logo após o EOF. Há teste de regressão em `test_stdio.py`.
- **D1 resolvido:** `-Duser.language=en -Duser.country=US` no `startJVM`. O aviso agora sai como
  "Recovery device deployment at high speed (83.9 m/s)", igual ao Linux.
- **D2 resolvido:** caminhos devolvidos ao LLM usam `as_posix()` (`motores/F46-OM.eng`).
- **Bug novo, achado pelos testes:** `simular_voo(salvar_como=...)` **nunca funcionava para arquivo novo**,
  porque `_caminho()` exigia que todo `.ork` existisse. Com subpasta inexistente, o Java dava `IOException`
  e o LLM recebia só o erro genérico. Agora `_caminho(existente=False)` e `_destino()` validam a extensão,
  criam a pasta e checam tudo **antes** de simular.
- `carregar_ric` lança `ToolError` em vez de `ValueError` para `.ric` inválido.

### Roadmap 4: criação de motores
Tools novas: `listar_tipos_grao`, `criar_motor`, `ler_motor` e `editar_motor`. O roadmap pedia duas; as
outras duas se mostraram necessárias. `listar_tipos_grao` é como o LLM descobre os parâmetros, e
`ler_motor` dá os nomes usados nos caminhos de edição.
- **Parâmetros gerados das propriedades do motorlib** (`coreDiameter_mm`, `divAngle_deg`, `maxPressure_MPa`...),
  com a conversão de unidade pela tabela `_UNIDADES`. Grãos novos do openMotor aparecem sem manter lista
  à mão. O Custom Grain fica de fora, porque exige DXF.
- **Armadilha nova do motorlib:** `FloatProperty.setValue` **ignora em silêncio** valores fora da faixa e
  mantém o valor anterior. A faixa é checada na nossa camada, e o erro volta ao LLM.
- Nos grãos, todo parâmetro numérico é obrigatório, porque o padrão do motorlib é o mínimo da faixa (0 ou
  negativo). Na tubeira, `throatLength`, `slagCoeff` e `erosionCoeff` são opcionais (0). A config usa os
  padrões do openMotor.
- Validação antes de gravar: as mesmas checagens do `runSimulation` (grãos, End Burner na frente, tubeira,
  propelente). Se houver erro, nada é gravado, e o local vem no formato do caminho (`grains[1]`).
- **Propelente:** aceita um nome da biblioteca (`KNSB` resolve para `Nakka - KNSB`; ambíguo vira erro) ou um
  objeto próprio (`name`, `density_kg_m3`, `tabs`). Foi necessário porque o `.ric` de teste tem uma versão
  **antiga** do "MIT - Cherry Limeade", diferente da biblioteca atual. Recriar o motor pelo nome dava F38
  em vez de F46. O `ler_motor` devolve só o nome quando o propelente é idêntico ao da biblioteca e o objeto
  completo quando não é, e assim a ida e volta `ler_motor` → `criar_motor` reproduz o F46 exato.
- **O critério de aceitação do roadmap estava fisicamente errado para este motor:** "aumente o núcleo até
  Pmax < 5 MPa". Num BATES com L > d, abrir o núcleo **aumenta** a área de queima (dA/dd = π(L − d)) e a
  pressão (5,70 → 6,71 MPa com +1 mm). O teste itera abrindo a **garganta**, que reduz o Kn.

### Pendências
- O `.ric` gravado pelo `salvar_ric()` **ainda não foi aberto na GUI do openMotor**, e no Windows sai com
  CRLF (P4). Vale testar antes de confiar em arquivos editados pelo MCP.
- A sessão do Claude Code que já estava conectada roda o código antigo: é preciso reconectar o servidor
  (`/mcp`) ou recarregar a janela.
- O roadmap item 5 (`previa_motor`) ficou coberto pela `previa` que `criar_motor`, `editar_motor` e
  `ler_motor` devolvem.

---

## 2026-10-18 — Reconstrução do Notus, voo no Hemera e edição do foguete (roadmap itens 6 a 10)

**Estado:** `pytest`: **78 passed** (~21 s). `teste_cliente.py` igual à §8, agora com 14 tools.

### Reconstrução do motor N3654 (Notus_Teorico.eng) e voo no Hemera
- Com a descrição do João (KNSB, 5 BATES, 3 com núcleo de 45 mm e 2 com 50 mm), uma varredura achou
  **grãos de 101,6 mm (4") × 170 mm**: com esse diâmetro, a massa do `.eng` (9,471932 kg) dá exatamente
  170,00 mm. A garganta ficou em ~25,07 mm e a saída em ~55 mm, com eficiência de 0,85 (padrão assumido).
  O erro RMS ficou em 2%, com pico, queda (fim dos grãos de 50 mm em 2,61 s) e fim nos instantes exatos.
  Arquivos: `workspace_teste/motores/notus_reconstrucao_v2.ric` e `Notus-Rec-v2.eng`. A v1 pode ser apagada.
- No `hemera.ork` (voo nominal), a reconstrução dá 2.829 m de apogeu contra 2.836 m do original (−0,2%).
- Ainda não confirmados com o João: as dimensões medidas, a garganta, a ordem dos grãos e a caracterização
  do KNSB.

### Decisões do João
- Edições do foguete gravam **em arquivo novo**, salvo pedido explícito (`salvar_como` obrigatório;
  `sobrescrever=True` para gravar por cima). A regra vale também para `salvar_como` do `simular_voo` e
  para os CSV. O `editar_motor`, anterior a essa regra, continua editando no lugar.
- Parâmetros físicos com nomes em inglês + unidade (`span_mm`, `deployAltitude_m`), como no motor.

### O que entrou
- **Correções:** os `.eng` de `motores/` são registrados a cada carga de `.ork` (`sincronizar_motores`), e
  não só na inicialização; um `.eng` quebrado só gera log, sem derrubar o OpenRocket. A montagem
  fantasma (comprimento 0) virou um aviso informativo em vez de um falso "não cabe"/overhang.
  `indice_simulacao` inválido e exceções Java viram `ToolError` legível (`_erros_java`).
- **`simular_voo`:** novas opções de direção do trilho, vento (direção e turbulência), latitude, longitude,
  temperatura e pressão, e `semente` (padrão 0). Retorna também a `estabilidade` (margem estática pelo
  Barrowman a Mach 0,3, CG, CP e massas, como a interface do OpenRocket) e a `estabilidade_voo` (na saída
  do trilho e mínima na subida), além de `alertas_projeto` (saída do trilho < 15 m/s, estabilidade < 1 cal,
  margem > 3 cal, Mach > 0,8, atraso de ejeção longe do ótimo).
- **`ler_foguete`:** árvore com caminho, tipo e parâmetros editáveis de cada componente.
- **`editar_foguete`:** edição por caminho (basta o final, se único), criação e remoção de massas
  (lastro), overrides de massa e CG, e abertura de paraquedas. Devolve a estabilidade antes e depois.
- **`varrer_parametro`**, **`exportar_series_voo`** e **`exportar_series_motor`** (CSV).

### Armadilhas novas do OpenRocket 24.12
- **`setRandomSeed()` não fixa a turbulência do vento.** Cada `PinkNoiseWindModel` sorteia a própria
  semente no construtor (campo `final`, sem setter). Medido no foguete de exemplo com vento de 6 m/s e
  40% de turbulência: sem semente, 45,6 a 49,1 m; com semente, 47,8 m sempre. A semente é gravada por
  reflexão (`_fixar_semente`). Isso funciona até o Java 25; o JEP 500 deve restringir escrita em campo
  `final` no futuro.
- **`isDefault(fcid)` dá `False` mesmo quando a simulação herda a configuração de abertura padrão
  compartilhada.** O teste certo é `containsId(fcid)`. O pytest pegou isso: editar a abertura da
  simulação 0 alterava todas as outras.
- **A estabilidade em voo é NaN no trilho** e não tem significado na descida (no Hemera chega a −9). O
  mínimo só é calculado entre a saída do trilho e o apogeu.
- **Irmãos com o mesmo nome no mesmo pai** (dois "Centering ring" no foguete de exemplo) recebem índice no
  caminho: `Centering ring[1]`.
- No modelo de vento multinível, os setters de vento médio não têm efeito. Quando o usuário informa vento,
  o modelo é trocado para o médio e isso aparece em `notas`.

### Hemera (para quem for editar)
- Quase todos os módulos sobrescrevem a massa **incluindo os filhos**. Mudar dimensões ou criar lastro
  dentro deles **não muda a massa** (a resposta avisa em `efeitos`). Para o lastro valer, é preciso somar
  a massa ao `overrideMass_kg` do módulo.
- Cada simulação tem a própria configuração de abertura (no voo nominal, o principal abre a 500 m).
- Margem estática de 1,64 cal na decolagem e 2,49 após a queima. Estabilidade na saída do trilho: 1,24 cal.

### Varredura de condições de lançamento (mesmo dia)
- `varrer_parametro` aceita como `parametro` uma condição de lançamento (nomes dos argumentos de
  `simular_voo`: `vento_medio_ms`, `angulo_trilho_graus`, `direcao_trilho_graus`, `turbulencia_pct`,
  `semente`...), além de parâmetros do foguete. Com `semente` e turbulência, mostra a dispersão.
- `simular_voo` e as linhas da varredura trazem o **ponto de pouso** (distância e rumo a partir da base) e
  as condições **efetivas** de lançamento (`lancamento`).
- **Convenções verificadas por simulação:** `vento_direcao_graus` é **de onde o vento vem** (vento de 90°
  faz o foguete pousar a oeste); `direcao_trilho_graus` é **para onde o trilho aponta**.
- **Armadilha (bug corrigido):** o `.ork` de exemplo e o Hemera vêm com **"Launch into wind" ligado**. Nesse
  modo, o OpenRocket aponta o trilho contra o vento e **ignora** a direção do trilho, então
  `direcao_trilho_graus` não tinha efeito, sem erro nenhum. A minha primeira "verificação" da convenção
  do trilho passou por coincidência (vento e trilho em 90°). Agora informar a direção desliga esse modo
  (com nota), e há a opção `lancar_contra_vento`.
- **Hemera**, voo nominal (trilho de 6 m, 10°, contra o vento):
  - **Vento** de leste: a estabilidade na saída do trilho cai de 1,69 cal (sem vento) para 1,18 (3 m/s),
    0,84 (6 m/s) e 0,44 cal (12 m/s). Com vento lateral, o ângulo de ataque na saída do trilho (33 m/s)
    é grande. Com 6 m/s ou mais, aparecem alertas de estabilidade < 1 cal.
  - **Ângulo do trilho** com 6 m/s de vento: a deriva é mínima perto de 2° (49 m), contra 869 m com os
    10° do arquivo.
