# Flutter de aletas compósitas: método, implementação e estudo paramétrico com OpenRocket

Documento de referência para um agente (Claude Code) com acesso ao modelo OpenRocket do foguete de dois estágios (booster + sustentador). O objetivo é variar a geometria e a laminação das aletas e escolher a melhor configuração. A escolha considera a margem de flutter, a divergência, a estabilidade e o apogeu.

Ele nasce da planilha Mathcad "Cálculo de Empenas – Nova versão v2". Ela segue Reddy (*Mechanics of Laminated Composite Plates and Shells*) para a estrutura e Bisplinghoff, Ashley & Halfman (*Aeroelasticity*) para a aerodinâmica. A planilha estava incompleta. Este documento completa o método e inclui uma implementação de referência em Python já validada (seção 15). Os problemas encontrados na planilha estão no Apêndice A.

> **Para o agente:** use o módulo da seção 15 (`fin_flutter.py`) como está. Ele passou nos três testes de validação da seção 14. Seu trabalho é (1) ler do OpenRocket a geometria das aletas e a trajetória simulada de cada configuração, (2) montar a laminação a partir do arquivo de configuração da seção 3.2, (3) rodar `run_case` para as aletas do booster e do sustentador, (4) aplicar os critérios da seção 12 e (5) produzir a tabela e os gráficos da seção 13. Não invente propriedades de material. Se o arquivo de configuração tiver valores marcados como `placeholder`, rode mesmo assim, mas destaque isso em todo resultado.

> **No rocket-mcp** isso já está implementado: `src/rocket_mcp/fin_flutter.py` (a seção 15, com as correções do Apêndice C), `flutter_estudo.py` (configuração, critérios e sensibilidade) e as tools `analisar_flutter` e `comparar_aletas` (com `arquivo_config`). Modelo de configuração: `workspace_teste/fins_config.yaml`. A verificação deste documento está no Apêndice C.

---

## 1. O que é flutter

Flutter é uma instabilidade aeroelástica dinâmica. Uma aleta tem pelo menos dois modos de vibração relevantes: **flexão** (a ponta sobe e desce) e **torção** (a aleta gira em torno do eixo elástico). Em baixa velocidade, as forças aerodinâmicas amortecem esses modos. À medida que a pressão dinâmica cresce, a sustentação causada pela torção acopla os dois modos. As frequências se aproximam e, a partir da **velocidade de flutter** $V_F$, o ar passa a injetar energia na estrutura a cada ciclo. A amplitude cresce exponencialmente e a aleta falha em frações de segundo. Por isso o flutter é uma verificação de projeto obrigatória, e não de conforto.

Três fatores dominam o problema:

- **Rigidez torsional**, que em laminados vem de $D_{66}$. Ela é a principal alavanca de projeto e cresce com a espessura ao cubo.
- **Separação entre as frequências de flexão e de torção.** Quanto mais próximas, pior.
- **Posição do eixo elástico em relação ao centro aerodinâmico e ao centro de massa.** CG atrás do eixo elástico desestabiliza.

A divergência é o caso estático do mesmo fenômeno: a torção aumenta a sustentação, que aumenta a torção, sem oscilação. Ela também é verificada aqui.

## 2. Visão geral do cálculo

O fluxo é linear, em seis etapas:

1. **Escoamento:** atmosfera e trajetória do OpenRocket fornecem $\rho, a, V, M, q$ em cada instante.
2. **Laminado (CLPT):** $Q \to \bar Q(\theta) \to A, B, D$, mais a massa por área $m_A$.
3. **Modos da aleta:** Rayleigh–Ritz de placa trapezoidal engastada fornece $\omega_h$ (flexão) e $\omega_\alpha$ (torção).
4. **Seção típica equivalente:** a 75% da envergadura, com os parâmetros $b, a, x_\alpha, r_\alpha^2, \mu$.
5. **Aerodinâmica não estacionária:** Theodorsen com Prandtl–Glauert (subsônico) ou piston theory (supersônico). Solução pelo método V-g, que dá $V_F$.
6. **Critérios:** margem de flutter ao longo da trajetória, divergência, checagem por NACA TN-4197 e estabilidade/apogeu no OpenRocket.

## 3. Entradas

### 3.1 Vindas do OpenRocket (por configuração simulada)

| Grandeza | Uso | Observação |
|---|---|---|
| Corda de raiz $c_r$, corda de ponta $c_t$, envergadura (height) $s$ | geometria | por estágio |
| Sweep: comprimento de enflechamento $x_{sw}$ ou ângulo | $\tan\Lambda_{LE} = x_{sw}/s$ | o OpenRocket guarda o comprimento |
| Espessura da aleta | deve ser igual a $h$ do laminado | o agente impõe isso |
| Densidade do material da aleta | deve ser igual a $\rho_{eq}=m_A/h$ | para a massa no OpenRocket bater |
| Série temporal: tempo, velocidade total, Mach, pressão do ar, temperatura do ar, velocidade do som | trajetória | $\rho = p/(R\,T)$; converta mbar→Pa e °C→K |
| Margem de estabilidade (calibres), apogeu, $V_{max}$, $M_{max}$ | restrições e objetivo | |
| Eventos: queima do booster, separação, ignição do sustentador | identificar fases | |

Atenção a qual trajetória vale para cada conjunto de aletas:

- **Aletas do sustentador:** voam o voo **inteiro**. Elas passam pela fase do booster (baixa altitude, alta densidade) e depois pela queima do sustentador. A verificação deve cobrir toda a trajetória do ramo do sustentador.
- **Aletas do booster:** voam da decolagem até a separação. Depois disso o booster só desacelera. Basta o trecho até a separação, mas pode incluir o ramo do booster inteiro sem problema.

Se o OpenRocket não exportar pressão e temperatura, use a ISA. Na troposfera ($h \le 11$ km): $T = 288{,}15 - 0{,}0065\,h$ e $p = 101325\,(T/288{,}15)^{5{,}2559}$. Acima disso: $T = 216{,}65$ K e $p = 22632\,\exp[-9{,}80665\,(h-11000)/(287\cdot216{,}65)]$. Em todos os casos, $\rho = p/(287\,T)$ e $a = \sqrt{1{,}4\cdot287\,T}$.

### 3.2 Fornecidas pela equipe (`fins_config.yaml`)

O OpenRocket trata a aleta como material isotrópico com uma densidade. A laminação, portanto, vem de um arquivo separado. Estrutura sugerida:

```yaml
# expoente COM sinal (55.0e+9): o PyYAML lê 55.0e9 como texto (Apêndice C)
materials:
  carbono:            # propriedades da LÂMINA (no sistema 1-2 da lâmina), SI
    E1: 55.0e+9
    E2: 55.0e+9
    G12: 4.0e+9
    nu12: 0.08
    rho: 1500
    source: placeholder   # trocar por "ensaio YYYY-MM" quando houver dados
  vidro:
    E1: 20.0e+9
    E2: 20.0e+9
    G12: 3.5e+9
    nu12: 0.12
    rho: 1850
    source: placeholder

laminates:               # sequência de baixo para cima; theta medido a partir da ENVERGADURA
  L8_quasi:
    - {mat: carbono, theta_deg: 0,  t: 0.30e-3}
    - {mat: carbono, theta_deg: 45, t: 0.30e-3}
    - {mat: vidro,   theta_deg: 0,  t: 0.20e-3}
    - {mat: vidro,   theta_deg: 0,  t: 0.20e-3}
    - {mat: carbono, theta_deg: 45, t: 0.30e-3}
    - {mat: carbono, theta_deg: 0,  t: 0.30e-3}

analysis:
  e_ea: 0.40        # eixo elástico em fração da corda (sensibilidade: 0.35-0.50)
  eta_ref: 0.75     # estação da seção típica (fração da envergadura)
  g_s: 0.0          # amortecimento estrutural considerado (0 = conservador)
```

**Convenção de eixos, importante.** No código, o eixo 1 do laminado ($x$) aponta ao longo da **envergadura**, da raiz para a ponta, e $y$ ao longo da corda. Uma lâmina a 0° tem fibras radiais, saindo do tubo. Na planilha Mathcad aparecem ângulos de 90°, então confirme com a equipe qual referência foi usada antes de transcrever. Para tecido bidirecional, $E_1 \approx E_2$ e uma lâmina a 45° equivale a ±45°.

### 3.3 Valores de material

**Nada aqui substitui ensaio.** A própria planilha anota que a equipe deve decidir entre regra das misturas e dados experimentais das laminações. Dados experimentais são preferíveis.

| Material (referência) | $E_1$ [GPa] | $E_2$ [GPa] | $G_{12}$ [GPa] | $\nu_{12}$ | $\rho$ [kg/m³] |
|---|---|---|---|---|---|
| Carbono/epóxi UD (T300/5208, livro-texto) | 181 | 10,3 | 7,17 | 0,28 | 1600 |
| Vidro/epóxi UD (Scotchply 1002, livro-texto) | 38,6 | 8,27 | 4,14 | 0,26 | 1800 |
| Tecido de carbono, laminação manual (ordem de grandeza) | ~45–60 | ~45–60 | ~3–5 | ~0,05–0,1 | ~1400–1550 |
| Tecido de vidro, laminação manual (ordem de grandeza) | ~15–22 | ~15–22 | ~3–4 | ~0,1–0,15 | ~1700–1900 |

$G_{12}$ é a propriedade mais importante para flutter e a menos confiável nas tabelas: depende quase só da resina e da fração volumétrica. Se for preciso estimar pela regra das misturas, com fração de fibra $V_f$:

$$E_1 = E_f V_f + E_m V_m,\quad \frac{1}{E_2} = \frac{V_f}{E_f} + \frac{V_m}{E_m},\quad \frac{1}{G_{12}} = \frac{V_f}{G_f} + \frac{V_m}{G_m},\quad \nu_{12} = \nu_f V_f + \nu_m V_m,\quad \rho = \rho_f V_f + \rho_m V_m$$

$G_{13}$ e $G_{23}$, que aparecem na planilha, só entram em teoria de primeira ordem (FSDT, cisalhamento transversal). Para aletas finas ($h/c \lesssim 0{,}03$), a CLPT basta e eles não são usados.

## 4. Escoamento

$$M = \frac{V}{a},\qquad q = \tfrac12 \rho V^2$$

A planilha já calcula a atmosfera ao nível do mar: $\rho_\infty = 1{,}2252$ kg/m³ e $a_\infty = 340{,}26$ m/s, ambos conferidos. Para o estudo, porém, use os valores locais da trajetória em cada instante. Com o nível do mar, o sustentador ficaria com densidade conservadora, mas com o Mach errado.

## 5. Geometria

O sistema tem $y$ ao longo da envergadura ($0 \le y \le s$) e $x$ na direção do escoamento. As grandezas geométricas são:

$$c(y) = c_r - \frac{c_r - c_t}{s}\,y,\qquad x_{LE}(y) = y\tan\Lambda_{LE},\qquad x_{TE}(y) = x_{LE}(y) + c(y)$$
$$x_{AC}(y) = x_{LE} + 0{,}25\,c,\qquad x_{EA}(y) = x_{LE} + e_{EA}\,c,\qquad b(y) = c(y)/2$$
$$S_{aleta} = \frac{(c_r + c_t)\,s}{2},\qquad AR = \frac{s^2}{S_{aleta}},\qquad \lambda = \frac{c_t}{c_r}$$

Duas observações sobre os pontos característicos. No supersônico, o centro aerodinâmico de uma placa fina vai para cerca de 50% da corda. Já o eixo elástico em 40% é uma convenção, porque numa placa uniforme ele não é bem definido. Rode a sensibilidade com $e_{EA}$ entre 0,35 e 0,50.

## 6. Laminado: teoria clássica de laminados (Reddy)

**Rigidez reduzida de cada material**, no sistema da lâmina:

$$\nu_{21} = \nu_{12}\frac{E_2}{E_1},\quad \Delta = 1 - \nu_{12}\nu_{21},\quad Q_{11} = \frac{E_1}{\Delta},\quad Q_{22} = \frac{E_2}{\Delta},\quad Q_{12} = \frac{\nu_{12}E_2}{\Delta},\quad Q_{66} = G_{12}$$

**Rotação de cada lâmina** pelo ângulo $\theta_k$, com $m = \cos\theta$ e $n = \sin\theta$:

$$\bar Q_{11} = Q_{11}m^4 + 2(Q_{12} + 2Q_{66})m^2n^2 + Q_{22}n^4$$
$$\bar Q_{22} = Q_{11}n^4 + 2(Q_{12} + 2Q_{66})m^2n^2 + Q_{22}m^4$$
$$\bar Q_{12} = (Q_{11} + Q_{22} - 4Q_{66})m^2n^2 + Q_{12}(m^4 + n^4)$$
$$\bar Q_{66} = (Q_{11} + Q_{22} - 2Q_{12} - 2Q_{66})m^2n^2 + Q_{66}(m^4 + n^4)$$
$$\bar Q_{16} = (Q_{11} - Q_{12} - 2Q_{66})m^3n + (Q_{12} - Q_{22} + 2Q_{66})mn^3$$
$$\bar Q_{26} = (Q_{11} - Q_{12} - 2Q_{66})mn^3 + (Q_{12} - Q_{22} + 2Q_{66})m^3n$$

**Matrizes ABD.** As coordenadas $z$ são medidas a partir do plano médio, com $z_0 = -h/2$ e $z_k = z_{k-1} + t_k$:

$$A = \sum_k \bar Q_k (z_k - z_{k-1}),\qquad B = \tfrac12\sum_k \bar Q_k (z_k^2 - z_{k-1}^2),\qquad D = \tfrac13\sum_k \bar Q_k (z_k^3 - z_{k-1}^3)$$
$$m_A = \sum_k \rho_k t_k,\qquad h = \sum_k t_k,\qquad \rho_{eq} = m_A/h$$

No flutter, quem manda é $D$. $D_{11}$ dá a flexão na envergadura e $D_{66}$ dá a torção. Lâminas a ±45° são as que mais aumentam $D_{66}$, e lâminas externas pesam muito mais do que as internas, por causa do $z^3$.

**Regras de projeto do laminado:**

- **Simétrico**, para ter $B = 0$. Sem isso, extensão e flexão se acoplam e a aleta empena na cura. O modelo de placa também assume $B = 0$. O código reporta `B_max` e o agente deve rejeitar ou destacar casos com $B \ne 0$.
- **Balanceado** (+45 junto de −45, ou tecido), para que $D_{16}, D_{26} \approx 0$. Esses termos acoplam flexão e torção e podem subir ou derrubar $V_F$. O código os inclui no Ritz, mas a seção típica não os representa. O código reporta `D16_D11`; acima de ~0,1, destaque.
- Para rigidez torsional, prefira **±45 nas faces** e 0° no núcleo.

## 7. Frequências naturais

**Método padrão: Rayleigh–Ritz de placa** (`plate_modes`). A aleta é tratada como placa trapezoidal enflechada, engastada na raiz, com energia de deformação e cinética dadas por

$$U = \tfrac12\int_A \boldsymbol\kappa^T D\,\boldsymbol\kappa\, dA,\quad \boldsymbol\kappa = \{w_{,xx},\ w_{,yy},\ 2w_{,xy}\},\qquad T = \tfrac12\,\omega^2\int_A m_A\,w^2\, dA$$

A base é $w = X^2\,P_i(2X-1)\,P_j(\hat Y)$, com polinômios de Legendre. O fator $X^2$ impõe o engaste ($w = w_{,x} = 0$ na raiz). Resolve-se $K\mathbf a = \omega^2 M\mathbf a$. O primeiro modo de flexão vira $\omega_h$ e o primeiro de torção vira $\omega_\alpha$. A classificação usa a forma na ponta: a torção muda de sinal ao longo da corda.

**Por que não usar o modelo de viga**, que era o caminho natural da planilha. Ele usa $GJ \approx 4D_{66}c$ e $EI \approx c/d_{11}$, com $d = D^{-1}$, e funciona bem para aletas esbeltas. Aletas de foguete costumam ter alongamento baixo ($AR \lesssim 1{,}5$). Nelas, o engaste ao longo de toda a corda da raiz dá muita rigidez torsional extra, que a viga ignora. Numa aleta de teste com $c_r = 0{,}30$ m, $c_t = 0{,}10$ m, $s = 0{,}15$ m, 40° de enflechamento e 8 lâminas de tecido de carbono de 0,3 mm, a viga dá $f_\alpha \approx 141$ Hz e a placa dá $\approx 279$ Hz. A viga fica em `beam_frequencies` apenas como checagem conservadora.

**Calibração experimental, altamente recomendada.** Um tap test na aleta já laminada mede $f_h$ e $f_\alpha$ diretamente. Basta engastar a raiz numa morsa ou no próprio fin can, bater na ponta e gravar com acelerômetro ou microfone. Faça uma FFT; bater no bordo de ataque da ponta excita bem a torção. Com as frequências medidas, substitua as calculadas. Isso também captura a flexibilidade real da fixação: o engaste perfeito é **não conservador** se a raiz for colada num fin can flexível.

## 8. Seção típica equivalente

O problema 3D é reduzido a um aerofólio de 2 graus de liberdade, flexão $h$ e torção $\alpha$, na estação $\eta_{ref} = 0{,}75$ da envergadura. Os parâmetros são os seguintes, para uma placa de espessura uniforme (CG a 50% da corda) com $c = c(\eta_{ref}\,s)$:

| Parâmetro | Definição | Valor com $e_{EA}=0{,}40$ |
|---|---|---|
| Semicorda | $b = c/2$ | — |
| Posição do eixo elástico | $a = (x_{EA} - x_{meio})/b = 2(e_{EA} - 0{,}5)$ | $-0{,}2$ |
| Distância CG–EA | $x_\alpha = 2(0{,}5 - e_{EA})$ | $0{,}2$ |
| Raio de giração² | $r_\alpha^2 = I_\alpha/(m b^2) = 4\left[\tfrac{1}{12} + (0{,}5 - e_{EA})^2\right]$ | $0{,}373$ |
| Razão de massa | $\mu = \dfrac{m}{\pi\rho b^2} = \dfrac{4\,m_A}{\pi\,\rho\,c}$ | depende de $\rho$ |
| Razão de frequências | $\sigma = \omega_h/\omega_\alpha$ | da seção 7 |

Note que $\mu$ cai com a densidade do ar. Uma aleta mais leve, ou ar mais denso, tem $\mu$ menor e $V_F$ menor.

## 9. Aerodinâmica não estacionária

**Equações de movimento** da seção típica, com $h$ positivo para baixo e $\alpha$ positivo cabrando:

$$m\ddot h + S_\alpha\ddot\alpha + k_h h = -L,\qquad S_\alpha\ddot h + I_\alpha\ddot\alpha + k_\alpha\alpha = M_{EA}$$

**Subsônico: Theodorsen** (Bisplinghoff, cap. 5):

$$L = \pi\rho b^2(\ddot h + V\dot\alpha - ba\ddot\alpha) + 2\pi\rho V b\,C(k)\left[\dot h + V\alpha + b(\tfrac12 - a)\dot\alpha\right]$$
$$M_{EA} = \pi\rho b^2\left[ba\ddot h - Vb(\tfrac12 - a)\dot\alpha - b^2(\tfrac18 + a^2)\ddot\alpha\right] + 2\pi\rho V b^2(a + \tfrac12)\,C(k)\left[\dot h + V\alpha + b(\tfrac12 - a)\dot\alpha\right]$$

A frequência reduzida é $k = \omega b/V$. A função de Theodorsen, que representa o atraso da esteira, é

$$C(k) = \frac{H_1^{(2)}(k)}{H_1^{(2)}(k) + iH_0^{(2)}(k)}.$$

Ela vale $C \to 1$ (quase estacionário) quando $k \to 0$ e $C \to 0{,}5$ quando $k \to \infty$.

Com movimento harmônico e $\hat h = h/b$, as cargas se escrevem como $-L/(\pi\rho b^3\omega^2) = \ell_h\hat h + \ell_\alpha\alpha$ e $M_{EA}/(\pi\rho b^4\omega^2) = m_h\hat h + m_\alpha\alpha$, com

$$\ell_h = 1 - \frac{2iC}{k},\qquad \ell_\alpha = -a - \frac{i}{k} - \frac{2C}{k^2} - \frac{2i(\tfrac12 - a)C}{k}$$
$$m_h = -a + \frac{2i(\tfrac12 + a)C}{k},\qquad m_\alpha = \tfrac18 + a^2 - \frac{i(\tfrac12 - a)}{k} + \frac{2(\tfrac12 + a)C}{k^2} + \frac{2i(\tfrac14 - a^2)C}{k}$$

A compressibilidade subsônica entra por uma correção grosseira de Prandtl–Glauert: $C \to C/\beta$, com $\beta = \sqrt{1 - M^2}$, só nos termos circulatórios. Ela só é usada até $M = 0{,}8$.

**Supersônico: piston theory linear.** A pressão local é $p - p_\infty = \rho a\, w$, com downwash $w = \partial z/\partial t + V\,\partial z/\partial x$. Integrando na corda:

$$L = 4\rho a_\infty b\left[\dot h + V\alpha - ab\dot\alpha\right],\qquad M_{EA} = 4\rho a_\infty b^2\left[a\dot h + aV\alpha - b(\tfrac13 + a^2)\dot\alpha\right]$$

Na forma adimensional, com $f = 4/(\pi M_{eff}\,k)$:

$$\ell_h = -if,\quad \ell_\alpha = -f\left(\frac1k - ia\right),\quad m_h = iaf,\quad m_\alpha = f\left(\frac ak - i(\tfrac13 + a^2)\right)$$

A piston theory pura usa $M_{eff} = M$. O código usa $M_{eff} = \sqrt{M^2 - 1}$, que dá a inclinação de Ackeret $4/\sqrt{M^2-1}$. Isso é mais conservador e mais correto em $1{,}2 < M < 2$.

**Transônico ($0{,}8 \le M \le 1{,}2$).** Nenhum método linear é confiável nessa faixa, que é justamente onde o flutter costuma ser mais crítico (o "transonic dip"). O código adota o menor $V_F$ entre Theodorsen a $M = 0{,}8$ e piston a $M = 1{,}2$, e marca o regime como `transonico`. Resultados críticos nessa faixa pedem margem maior (seção 12).

## 10. Solução pelo método V-g

Substituindo cargas e estrutura, com amortecimento estrutural artificial $g$ e a incógnita $Z = (\omega_\alpha/\omega)^2(1 + ig)$:

$$\begin{vmatrix} \mu(1 - \sigma^2 Z) + \ell_h & \mu x_\alpha + \ell_\alpha \\ \mu x_\alpha + m_h & \mu r_\alpha^2(1 - Z) + m_\alpha \end{vmatrix} = 0$$

Isso é um polinômio de grau 2 em $Z$. Com $P = \mu + \ell_h$, $Q = \mu r_\alpha^2 + m_\alpha$, $R = \mu x_\alpha + \ell_\alpha$ e $S = \mu x_\alpha + m_h$:

$$\mu^2\sigma^2 r_\alpha^2\, Z^2 - \mu(\sigma^2 Q + r_\alpha^2 P)\,Z + (PQ - RS) = 0$$

O algoritmo varre $k$ de 20 a 0,005 (velocidade crescente). Para cada $k$ e cada raiz:

$$\omega = \frac{\omega_\alpha}{\sqrt{\operatorname{Re} Z}},\qquad g = \frac{\operatorname{Im} Z}{\operatorname{Re} Z},\qquad V = \frac{\omega b}{k}$$

O $g$ obtido é o amortecimento estrutural necessário para manter o movimento harmônico. O flutter ocorre onde algum ramo cruza $g = g_s$ subindo. O padrão é $g_s = 0$, conservador; compósitos reais têm $g_s \approx 0{,}01$–$0{,}03$. Esse cruzamento dá $V_F$ e $\omega_F$.

## 11. Verificações complementares

**Divergência torsional.** Pela seção típica, com $e$ a distância do CA ao EA:

$$q_D = \frac{K_\alpha}{c\, e\, C_{L\alpha}},\qquad K_\alpha = I_\alpha\omega_\alpha^2,\qquad e = (e_{EA} - x_{AC}/c)\,c$$

No subsônico, $C_{L\alpha} = 2\pi/\beta$. No supersônico, $C_{L\alpha} = 4/\sqrt{M^2 - 1}$ e o CA vai a ~50%. Assim, com $e_{EA} = 0{,}40$, $e < 0$ e não há divergência no supersônico.

**NACA TN-4197** (Martin, 1958), checagem empírica rápida para placas isotrópicas. Em SI consistente:

$$V_F = a\sqrt{\frac{G_E\,(AR + 2)\,(t/c)^3}{1{,}337\,AR^3\,(\lambda + 1)\,p}}$$

O fator 1,337 vem de $\tfrac{24\varepsilon\gamma}{\pi}\cdot\tfrac12$, com $\varepsilon = 0{,}25$ e $\gamma = 1{,}4$. A constante 39,3 da TN original equivale a $24\varepsilon\gamma p_0/\pi$ com $p_0$ em psi. O código usa $t/c$ na raiz e $G_E = 12D_{66}/h^3$, um módulo de cisalhamento equivalente. Atenção: versões populares dessa fórmula têm um fator 2 extra no denominador da fração interna. Esse fator eleva $V_F$ em $\sqrt2$ e é **não conservador**.

A TN-4197 costuma ser bem mais conservadora do que o V-g para aletas de baixo alongamento. Use-a como cota inferior e sinal de alerta, não como critério eliminatório isolado.

## 12. Critérios de aceitação

| Verificação | Critério | Comentário |
|---|---|---|
| Margem de flutter V-g, mínimo ao longo da trajetória: $\min_t V_F(M_t, \rho_t)/V_t$ | **≥ 1,5** no estudo; ≥ 1,15 só após calibrar $f_h, f_\alpha$ por tap test ou FEM | 1,15 em velocidade equivalente corresponde à MIL-A-8870C; 1,5 cobre a incerteza do modelo simplificado |
| Ponto crítico no regime `transonico` | margem **≥ 1,7** | método linear fora da validade |
| Divergência: $\min_t q_D/q_t$ | ≥ 1,32 ($= 1{,}15^2$) | só existe no subsônico com EA atrás do CA |
| TN-4197: $\min_t V_F^{TN}/V_t$ | ≥ 1,0 desejável | se o V-g passa e a TN falha, marque "validar com ensaio" |
| `B_max`, `D16_D11` | $B = 0$; $D_{16}/D_{11} < 0{,}1$ | caso contrário, a seção típica não representa o acoplamento |
| Estabilidade (OpenRocket) | margem fora do trilho e durante o voo dentro da faixa definida pela equipe (tipicamente 1,5–3 cal), nas duas configurações (empilhado e só sustentador) | trocar aletas move o CP |

## 13. Estudo paramétrico com o OpenRocket

**Variáveis**, por estágio. As faixas são uma sugestão inicial; ajuste com a equipe.

| Variável | Faixa inicial |
|---|---|
| $c_r$ | ±30% do atual |
| $\lambda = c_t/c_r$ | 0,2 – 0,6 |
| $s$ | ±30% do atual |
| $\Lambda_{LE}$ | 20° – 60° |
| Laminado | do arquivo `fins_config.yaml`; varie o número de lâminas ±45 nas faces |

**Laço para cada candidato:**

1. Montar a laminação e calcular $h$ e $\rho_{eq}$.
2. Escrever a geometria, $h$ e $\rho_{eq}$ no `.ork`, para o OpenRocket atualizar massa, CP e arrasto.
3. Simular e extrair a trajetória `(t, V, M, rho, p, a)` dos ramos relevantes (seção 3.1).
4. Rodar `run_case` para as aletas do booster e do sustentador.
5. Registrar a linha da tabela.

O `run_case` leva alguns segundos por caso. Para varreduras grandes, faça uma triagem com grade grossa e refine perto da fronteira de viabilidade.

**Saídas esperadas:**

- **`resultados.csv`**: uma linha por candidato e por estágio, com todas as chaves retornadas por `run_case`, mais apogeu, margem de estabilidade mínima e máxima, massa das aletas e $V_{max}/M_{max}$.
- **Gráfico 1:** apogeu × margem de flutter, colorido por massa de aleta, com a linha do critério. A fronteira de Pareto é a recomendação.
- **Gráfico 2:** margem de flutter ao longo do tempo para os 3 melhores candidatos, destacando a fase do booster, a separação e o sustentador.
- **Sensibilidade do vencedor:** $e_{EA} \in \{0{,}35;\ 0{,}40;\ 0{,}50\}$, $G_{12}$ com −30% e $\eta_{ref} \in \{0{,}7;\ 0{,}75;\ 0{,}8\}$. Se a decisão mudar, reporte isso.
- **Resumo em texto:** candidato recomendado, margens e pontos críticos (tempo, Mach, altitude), avisos de `placeholder` e próximos passos de validação (tap test, FEM).

**Objetivo sugerido:** maximizar o apogeu sujeito aos critérios da seção 12. Em empate, escolher a menor massa de aleta. Em geral, a espessura e as lâminas ±45 externas resolvem o flutter; geometria com menos envergadura e mais enflechamento resolve o restante, mas mexe na estabilidade.

## 14. Hipóteses, limitações e validação

**Hipóteses e limitações:**

- Seção típica 2 GDL com frequências de placa. É uma aproximação de engenharia, sem modos de ordem superior nem distribuição 3D de carga.
- Theodorsen é 2D e incompressível. Para aletas de baixo alongamento, a sustentação real é menor do que a 2D, o que tende a ser conservador.
- O enflechamento entra só nas frequências. Na aerodinâmica, ignorá-lo também tende a ser conservador.
- Engaste perfeito na raiz é **não conservador**; o tap test corrige isso.
- CLPT sem cisalhamento transversal.
- No transônico, nenhum método linear é válido.
- O aquecimento aerodinâmico em $M \gtrsim 2$ reduz $G_{12}$ e $E_2$ da resina. Se o voo passar disso, reduza $G_{12}$ na sensibilidade.

**Validação do código.** O bloco `__main__` do módulo roda três testes, e todos passaram:

| Teste | Referência | Obtido |
|---|---|---|
| Seção típica de Hodges & Pierce ($a=-0{,}2$, $x_\alpha=0{,}1$, $\mu=20$, $r_\alpha^2=0{,}24$, $\sigma=0{,}4$) | $V_F/(b\omega_\alpha) \approx 2{,}17$ | 2,184 |
| Placa quadrada isotrópica engastada-livre (Leissa), $\lambda = \omega a^2\sqrt{\rho h/D}$ | 3,471 / 8,507 / 21,29 / 27,20 | 3,472 / 8,512 / 21,29 / 27,20 |
| Laminado isotrópico | $D_{11} = Eh^3/[12(1-\nu^2)]$ | exato |

O agente deve rodar `python fin_flutter.py` antes de qualquer estudo e parar se algum desses valores mudar.

## 15. Implementação de referência

Depende só de `numpy` e `scipy`. A trajetória entra em `run_case` como lista de tuplas `(t, V, M, rho, p, a)` em SI.

```python
"""fin_flutter.py — implementação de referência: flutter de aletas compósitas
(CLPT + Rayleigh-Ritz de placa + seção típica 2 GDL, método V-g).
Unidades SI em tudo (m, kg, s, Pa, rad/s). Ângulos de lâmina em graus."""
import numpy as np
from scipy.special import hankel2
from scipy.linalg import eigh
from numpy.polynomial import legendre as L

# ---------- Material / laminado (Reddy, CLPT) ----------
def Q_matrix(E1, E2, G12, nu12):
    nu21 = nu12 * E2 / E1
    D = 1 - nu12 * nu21
    return np.array([[E1/D, nu12*E2/D, 0], [nu12*E2/D, E2/D, 0], [0, 0, G12]])

def Qbar(Q, theta_deg):
    m, n = np.cos(np.radians(theta_deg)), np.sin(np.radians(theta_deg))
    Q11, Q12, Q22, Q66 = Q[0,0], Q[0,1], Q[1,1], Q[2,2]
    Qb11 = Q11*m**4 + 2*(Q12+2*Q66)*m**2*n**2 + Q22*n**4
    Qb22 = Q11*n**4 + 2*(Q12+2*Q66)*m**2*n**2 + Q22*m**4
    Qb12 = (Q11+Q22-4*Q66)*m**2*n**2 + Q12*(m**4+n**4)
    Qb66 = (Q11+Q22-2*Q12-2*Q66)*m**2*n**2 + Q66*(m**4+n**4)
    Qb16 = (Q11-Q12-2*Q66)*m**3*n + (Q12-Q22+2*Q66)*m*n**3
    Qb26 = (Q11-Q12-2*Q66)*m*n**3 + (Q12-Q22+2*Q66)*m**3*n
    return np.array([[Qb11, Qb12, Qb16], [Qb12, Qb22, Qb26], [Qb16, Qb26, Qb66]])

def laminate(plies, materials):
    """plies: lista (baixo->cima) de dicts {mat, theta_deg, t}. Retorna A, B, D, h, m_A."""
    h = sum(p["t"] for p in plies)
    z = -h/2
    A = np.zeros((3,3)); B = np.zeros((3,3)); D = np.zeros((3,3)); mA = 0.0
    for p in plies:
        mat = materials[p["mat"]]
        Qb = Qbar(Q_matrix(mat["E1"], mat["E2"], mat["G12"], mat["nu12"]), p["theta_deg"])
        z0, z1 = z, z + p["t"]
        A += Qb*(z1-z0); B += Qb*(z1**2-z0**2)/2; D += Qb*(z1**3-z0**3)/3
        mA += mat["rho"]*p["t"]; z = z1
    return A, B, D, h, mA

# ---------- Geometria ----------
def chord(y, cr, ct, s):
    return cr - (cr-ct)*y/s

# ---------- Estrutura: viga engastada (apenas checagem; conservadora p/ baixo alongamento) ----------
def beam_frequencies(D, mA, cr, ct, s, e_ea=0.40, n=400):
    d = np.linalg.inv(D)                 # flexibilidades (bordas livres -> "beam")
    eta = np.linspace(0, 1, n); y = eta*s; c = chord(y, cr, ct, s)
    EI = c/d[0,0]; GJ = 4*c/d[2,2]      # x1 = direção da envergadura
    mp = mA*c
    Ia = mA*c**3*(1/12 + (0.5-e_ea)**2)
    # flexão: phi = 1 - cos(pi*eta/2)
    phi = 1-np.cos(np.pi*eta/2); phi2 = (np.pi/(2*s))**2*np.cos(np.pi*eta/2)
    wh = np.sqrt(np.trapezoid(EI*phi2**2, y)/np.trapezoid(mp*phi**2, y))
    # torção: psi = sin(pi*eta/2)
    psi = np.sin(np.pi*eta/2); psi1 = (np.pi/(2*s))*np.cos(np.pi*eta/2)
    wa = np.sqrt(np.trapezoid(GJ*psi1**2, y)/np.trapezoid(Ia*psi**2, y))
    return wh, wa

# ---------- Estrutura: Rayleigh-Ritz de placa (padrão) ----------
def _leg(n, t):
    """P_n(t), P_n'(t), P_n''(t)."""
    c = np.zeros(n+1); c[n] = 1
    return L.legval(t, c), L.legval(t, L.legder(c)), L.legval(t, L.legder(c, 2))

def plate_modes(D, mA, cr, ct, s, sweep_le_deg=0.0, p=7, nq=18, nmodes=4):
    """Rayleigh-Ritz para placa laminada trapezoidal engastada na raiz (CLPT, B=0).
    x = envergadura (raiz->ponta) = eixo 1 do laminado; y = corda (BA->BF).
    Base: w = X^2 P_i(2X-1) P_j(Yh), X = x/s, Yh in [-1,1] na caixa envolvente."""
    tl = np.tan(np.radians(sweep_le_deg))
    y0, y1 = min(0, s*tl), max(cr, s*tl+ct)
    ym, yh = (y0+y1)/2, (y1-y0)/2
    gq, wq = L.leggauss(nq); gq = (gq+1)/2; wq = wq/2
    idx = [(i, j) for i in range(p) for j in range(p)]; n = len(idx)
    K = np.zeros((n, n)); M = np.zeros((n, n))
    for xi, wx in zip(gq, wq):
        x = xi*s; c = cr-(cr-ct)*xi; yle = x*tl
        X = xi; tx = 2*X-1
        Pi = [_leg(i, tx) for i in range(p)]
        # f_i(X) = X^2 P_i(2X-1) e derivadas em x
        F = []
        for P, dP, ddP in Pi:
            f = X**2*P; df = 2*X*P + X**2*2*dP; ddf = 2*P + 4*X*2*dP + X**2*4*ddP
            F.append((f, df/s, ddf/s**2))
        for et, wy in zip(gq, wq):
            y = yle+et*c; ty = (y-ym)/yh
            Pj = [_leg(j, ty) for j in range(p)]
            G = [(P, dP/yh, ddP/yh**2) for P, dP, ddP in Pj]
            dA = wx*wy*s*c
            W = np.array([F[i][0]*G[j][0] for i, j in idx])
            kap = np.array([[F[i][2]*G[j][0], F[i][0]*G[j][2], 2*F[i][1]*G[j][1]] for i, j in idx]).T
            K += dA*kap.T@D@kap; M += dA*mA*np.outer(W, W)
    w2, V = eigh(K, M)
    om = np.sqrt(np.abs(w2[:nmodes]))
    # classificação pela forma na ponta: torção muda de sinal ao longo da corda
    X = 1.0; Fe = [X**2*_leg(i, 1.0)[0] for i in range(p)]
    ys = np.linspace(s*tl, s*tl+ct, 25); ty = (ys-ym)/yh
    kinds = []
    for m in range(nmodes):
        w = sum(V[k, m]*Fe[i]*_leg(j, ty)[0] for k, (i, j) in enumerate(idx))
        big = np.abs(w).max()
        kinds.append("torcao" if (w.max() > 0.2*big and w.min() < -0.2*big) else "flexao")
    return om, kinds


def pick_modes(om, kinds):
    """Primeiro modo de flexão e primeiro de torção."""
    if 'torcao' not in kinds or 'flexao' not in kinds:
        raise ValueError(f'modos não identificados: {kinds} — aumente nmodes ou confira a geometria')
    return om[kinds.index('flexao')], om[kinds.index('torcao')]

# ---------- Aerodinâmica ----------
def theodorsen_C(k):
    H1, H0 = hankel2(1, k), hankel2(0, k)
    return H1/(H1 + 1j*H0)

def aero_theodorsen(k, a, beta=1.0):
    C = theodorsen_C(k)/beta             # Prandtl-Glauert grosseiro nos termos circulatórios
    lh = 1 - 2j*C/k
    la = -a - 1j/k - 2*C/k**2 - 2j*(0.5-a)*C/k
    mh = -a + 2j*(0.5+a)*C/k
    ma = 1/8 + a**2 - 1j*(0.5-a)/k + 2*(0.5+a)*C/k**2 + 2j*(0.25-a**2)*C/k
    return lh, la, mh, ma

def aero_piston(k, a, Meff):
    f = 4/(np.pi*Meff*k)
    lh = -1j*f
    la = -f*(1/k - 1j*a)
    mh = 1j*a*f
    ma = f*(a/k - 1j*(1/3 + a**2))
    return lh, la, mh, ma

# ---------- V-g ----------
def vg(mu, a, xa, ra2, wh, wa, b, aero, ks=None):
    """Retorna V (m/s), g, omega (rad/s) para os 2 ramos ao longo de k."""
    ks = np.logspace(np.log10(20.0), np.log10(0.005), 2000) if ks is None else ks
    sig2 = (wh/wa)**2
    out = []
    for k in ks:
        lh, la, mh, ma = aero(k)
        P = mu + lh; Qd = mu*ra2 + ma; R = mu*xa + la; S = mu*xa + mh
        Z = np.roots([mu**2*sig2*ra2, -mu*(sig2*Qd + ra2*P), P*Qd - R*S])
        Z = Z[np.argsort(Z.real)[::-1]]   # ordena por freq. (Re Z maior -> omega menor)
        with np.errstate(invalid="ignore", divide="ignore"):
            om = np.where(Z.real > 0, wa/np.sqrt(np.abs(Z.real)), np.nan)
            g = Z.imag/Z.real
        out.append((k*np.ones(2), om*b/k, g, om))
    k, V, g, om = map(np.array, zip(*out))
    return k, V, g, om

def flutter_from_vg(V, g, om, gs=0.0):
    """Menor V em que algum ramo cruza g = gs subindo. None = sem flutter na faixa de k."""
    best = None
    for j in range(V.shape[1]):
        if g[0, j] >= gs:   # já instável na menor velocidade -> artefato ou modelo fora da validade
            return (V[0, j], om[0, j], j)
        for i in range(len(V)-1):
            if g[i,j] < gs <= g[i+1,j] and V[i+1,j] > V[i,j]:
                t = (gs-g[i,j])/(g[i+1,j]-g[i,j])
                Vf = V[i,j] + t*(V[i+1,j]-V[i,j]); wf = om[i,j] + t*(om[i+1,j]-om[i,j])
                if best is None or Vf < best[0]: best = (Vf, wf, j)
                break
    return best

# ---------- Pipeline de uma aleta num ponto de voo ----------
def typical_section(mA, cr, ct, s, e_ea=0.40, eta_ref=0.75):
    c = chord(eta_ref*s, cr, ct, s)
    a = 2*(e_ea-0.5); xa = 2*(0.5-e_ea); ra2 = 4*(1/12 + (0.5-e_ea)**2)
    return c, c/2, a, xa, ra2

def flutter_speed_at(M, wh, wa, mA, cr, ct, s, rho, e_ea=0.40, eta_ref=0.75, gs=0.0,
                     transonic=(0.8, 1.2)):
    """V_F (m/s) para um ponto de voo com Mach M e densidade rho.
    M < 0.8: Theodorsen + Prandtl-Glauert; M > 1.2: piston theory com sqrt(M^2-1);
    0.8..1.2: não há método linear confiável -> menor valor entre os dois extremos (flag)."""
    c, b, a, xa, ra2 = typical_section(mA, cr, ct, s, e_ea, eta_ref)
    mu = 4*mA/(np.pi*rho*c)
    def solve(Mx, kind):
        if kind == "theo":
            ae = lambda k: aero_theodorsen(k, a, beta=np.sqrt(1-Mx**2))
        else:
            ae = lambda k: aero_piston(k, a, np.sqrt(Mx**2-1))
        k, V, g, om = vg(mu, a, xa, ra2, wh, wa, b, ae)
        r = flutter_from_vg(V, g, om, gs)
        return np.inf if r is None else float(r[0])
    if M < transonic[0]:  return solve(M, "theo"), "sub"
    if M > transonic[1]:  return solve(M, "piston"), "super"
    return min(solve(transonic[0], "theo"), solve(transonic[1], "piston")), "transonico"

def matched_flutter_mach(wh, wa, mA, cr, ct, s, rho, sound, Ms=None, **kw):
    """Menor Mach em que a velocidade de voo alcança V_F (ponto 'casado'). inf se não ocorre até M_max."""
    Ms = np.r_[np.arange(0.1, 0.8, 0.05), 0.8, 1.0, 1.2, np.arange(1.3, 5.01, 0.1)] if Ms is None else Ms
    for M in Ms:
        Vf, reg = flutter_speed_at(M, wh, wa, mA, cr, ct, s, rho, **kw)
        if M*sound >= Vf: return M, Vf, reg
    return np.inf, np.inf, "nenhum"

def trajectory_margin(traj, wh, wa, mA, cr, ct, s, **kw):
    """traj: iterável de (t, V, M, rho). Retorna lista (t, V, M, V_F, margem, regime) e o ponto crítico."""
    rows = []
    for t, V, M, rho in traj:
        if V < 30: continue
        Vf, reg = flutter_speed_at(M, wh, wa, mA, cr, ct, s, rho, **kw)
        rows.append((t, V, M, Vf, Vf/V, reg))
    crit = min(rows, key=lambda r: r[4]) if rows else None
    return rows, crit

def divergence_q(mA, wa, cr, ct, s, M, e_ea=0.40, eta_ref=0.75):
    """q de divergência torsional da seção típica: q_D = K_a/(c e CLa), K_a = I_a wa^2.
    inf se o eixo elástico está à frente do CA (supersônico: CA ~ 50%)."""
    c = chord(eta_ref*s, cr, ct, s)
    x_ac = 0.25 if M < 1 else 0.50
    e = (e_ea - x_ac)*c
    if e <= 0: return np.inf
    if M < 1:   # Prandtl-Glauert limitado a M=0.8 (diverge perto de M=1; transônico não é linear)
        CLa = 2*np.pi/np.sqrt(1-min(M, 0.8)**2)
    else:
        CLa = 4/np.sqrt(max(M, 1.2)**2-1)
    Ia = mA*c**3*(1/12 + (0.5-e_ea)**2)
    return Ia*wa**2/(c*e*CLa)

def tn4197(G_E, cr, ct, s, h, p, sound):
    """NACA TN-4197 (Martin, 1958), SI consistente. Referência rápida, conservadora p/ isotrópico."""
    S = (cr+ct)*s/2; AR = s**2/S; lam = ct/cr; tc = h/cr
    return sound*np.sqrt(G_E*(AR+2)*tc**3/(1.337*AR**3*(lam+1)*p))

# ---------- Caso completo ----------
def run_case(fin, plies, materials, traj, e_ea=0.40, eta_ref=0.75, gs=0.0):
    """fin: dict cr, ct, s, sweep_le_deg. traj: lista (t, V, M, rho, p, sound).
    Retorna dicionário-resumo para a tabela do estudo paramétrico."""
    A, B, D, h, mA = laminate(plies, materials)
    om, kinds = plate_modes(D, mA, fin["cr"], fin["ct"], fin["s"], fin.get("sweep_le_deg", 0.0))
    wh, wa = pick_modes(om, kinds)
    geo = (fin["cr"], fin["ct"], fin["s"])
    qmax = max(0.5*r[3]*r[1]**2 for r in traj)
    # só pontos relevantes (q >= 10% de q_max), no máximo ~40, para o estudo paramétrico ser rápido
    traj = [r for r in traj if 0.5*r[3]*r[1]**2 >= 0.1*qmax and r[1] > 30]
    traj = traj[::max(1, len(traj)//40)]
    rows, crit = trajectory_margin([(t, V, M, rho) for t, V, M, rho, p, a in traj], wh, wa, mA, *geo,
                                   e_ea=e_ea, eta_ref=eta_ref, gs=gs)
    t_c, V_c, M_c, Vf_c, marg, reg = crit
    qD = min(divergence_q(mA, wa, *geo, r[2], e_ea, eta_ref)/(0.5*r[3]*r[1]**2) for r in traj)
    G_E = 12*D[2, 2]/h**3
    tn = min(tn4197(G_E, *geo, h, r[4], r[5])/r[1] for r in traj)
    out = dict(h_mm=h*1e3, mA=mA, rho_eq=mA/h, f_h=wh/2/np.pi, f_a=wa/2/np.pi,
                B_max=float(np.abs(B).max()), D16_D11=float(abs(D[0, 2])/D[0, 0]),
                margem_flutter=marg, t_critico=t_c, M_critico=M_c, V_critico=V_c, V_F_critico=Vf_c,
                regime_critico=reg, margem_q_divergencia=qD, margem_TN4197=tn, q_max=qmax)
    return {k: (float(v) if isinstance(v, (np.floating, float, int)) and not isinstance(v, bool) else v)
            for k, v in out.items()}

if __name__ == "__main__":
    # Validação: Hodges & Pierce — a=-1/5, e=-1/10 (xa=1/10), mu=20, ra2=6/25, sigma=2/5
    a, xa, mu, ra2 = -0.2, 0.1, 20.0, 6/25
    wa, b = 1.0, 1.0; wh = 0.4
    k, V, g, om = vg(mu, a, xa, ra2, wh, wa, b, lambda k: aero_theodorsen(k, a))
    print("Hodges & Pierce: V_F/(b w_a) =", flutter_from_vg(V, g, om)[0], " (esperado ~2.17-2.18)")
    # Validação 2: placa quadrada isotrópica engastada-livre (Leissa): lambda = w a^2 sqrt(rho h / D)
    E, nu, h, rho, aa = 70e9, 0.3, 2e-3, 2700, 0.2
    Dp = E*h**3/(12*(1-nu**2)); Dm = Dp*np.array([[1, nu, 0], [nu, 1, 0], [0, 0, (1-nu)/2]])
    om, kinds = plate_modes(Dm, rho*h, aa, aa, aa)
    print("Leissa:", np.round(om*aa**2*np.sqrt(rho*h/Dp), 3), kinds, " (esperado 3.471 8.507 21.29 27.20)")
    # Validação 3: laminado isotrópico -> D11 = E h^3 / (12 (1 - nu^2))
    A_, B_, D_, h_, m_ = laminate([{"mat": "Al", "theta_deg": 0, "t": 3e-3}],
                                  {"Al": dict(E1=E, E2=E, G12=E/2.6, nu12=nu, rho=2700)})
    print("D11 isotrópico:", D_[0, 0], "esperado", E*(3e-3)**3/(12*(1-nu**2)))
```

Exemplo de uso:

```python
import numpy as np, yaml
from fin_flutter import run_case

cfg = yaml.safe_load(open("fins_config.yaml"))
plies = cfg["laminates"]["L8_quasi"]
fin = dict(cr=0.30, ct=0.10, s=0.15, sweep_le_deg=40)       # m, m, m, graus — vindo do .ork
traj = [...]  # (t, V, M, rho, p, a) do ramo correto da simulação OpenRocket
res = run_case(fin, plies, cfg["materials"], traj, **{k: cfg["analysis"][k] for k in ("e_ea", "eta_ref")},
               gs=cfg["analysis"]["g_s"])
print(res["margem_flutter"], res["regime_critico"], res["t_critico"])
```

---

## Apêndice A: problemas na planilha Mathcad original

Estes são os itens que impediam a planilha de calcular, ou que dariam resultado errado, caso ela continue sendo usada em paralelo:

1. As velocidades `V_inf_Sust` e `V_inf_Booster`, todas as dimensões das aletas, as espessuras e as propriedades dos materiais estão vazias. Os materiais têm só a unidade "GPa", o que no Mathcad significa 1 GPa.
2. `rho_lam_*` e `nu_12_*` estão em GPa. É isso que causa o erro "These units are not compatible" em Δ: ν é adimensional e ρ é kg/m³.
3. `c_local_*(y)` usa `span` genérico em vez de `span_Booster`/`span_Sust`.
4. `x_TE_Booster` usa `c_local_Sust`.
5. `b_local_Sust := c_local_Sust(y)` está sem o `/2` (a semicorda é $c/2$).
6. `sweep_LE_*` depende de `x_booster`/`x_Sust`, que não estão definidos.
7. `Q_66_Carbon := G_12_Glass` deveria ser `G_12_Carbon`.
8. A matriz `Q_Carbon` contém o typo `Q_12_Carbom`.
9. `M_Carbon_Booster` deveria ser `N_Carbon_Booster`, e `t_Ply_Carbon_Booster` está sem valor.
10. `z_0_Booster` usa `h_lam_Booster` (L minúsculo), e `h_Lam_Booster` é definido duas vezes (placeholder na seção 1.2 e cálculo na 1.5.3).
11. `z_k` usa `t_Ply_Carbon`/`t_Ply_Glass` em vez das variáveis `_Booster`.
12. Faltam `theta_2` e `mat_2`, e há 3 interfaces em z para 5 ângulos. Melhor usar vetores indexados e um range `k`. A variável de sistema é `ORIGIN` em maiúsculas; `Origin := 0` não tem efeito.
13. `nu_21_*` está definido duas vezes (seções 1.4 e 1.5.1).
14. Faltam $\bar Q(\theta)$, ABD, frequências, aerodinâmica e a solução de flutter. Tudo isso está nas seções 6 a 10 deste documento.
15. O laminado esboçado (carbono, carbono, vidro com 90°/45°) não é simétrico nem balanceado (ver seção 6).

## Apêndice B: referências

- J. N. Reddy, *Mechanics of Laminated Composite Plates and Shells*, 2ª ed., CRC Press, 2004 (CLPT, $\bar Q$, ABD, Ritz).
- R. L. Bisplinghoff, H. Ashley, R. L. Halfman, *Aeroelasticity*, Dover, 1996 (seção típica, Theodorsen, método V-g).
- D. H. Hodges, G. A. Pierce, *Introduction to Structural Dynamics and Aeroelasticity*, 2ª ed., Cambridge, 2011 (caso de validação).
- T. Theodorsen, *General Theory of Aerodynamic Instability and the Mechanism of Flutter*, NACA Report 496, 1935.
- H. Ashley, G. Zartarian, "Piston Theory — A New Aerodynamic Tool for the Aeroelastician", *J. Aeronautical Sciences*, 1956.
- D. J. Martin, *Summary of Flutter Experiences as a Guide to the Preliminary Design of Lifting Surfaces on Missiles*, NACA TN-4197, 1958.
- A. W. Leissa, *Vibration of Plates*, NASA SP-160, 1969 (placa engastada-livre).
- MIL-A-8870C, *Airplane Strength and Rigidity: Vibration, Flutter, and Divergence* (margem de 15%).

## Apêndice C: verificação e implementação no rocket-mcp (2026-10-21)

**Conferido e correto.** Refiz as derivações e rodei o código da seção 15 sem alterações:

- Theodorsen: $L$ e $M_{EA}$ conferem com Bisplinghoff e Hodges & Pierce. Os coeficientes $\ell_h, \ell_\alpha, m_h, m_\alpha$ foram derivados de novo, com movimento harmônico, e conferem. A matriz de flutter e o polinômio em $Z$ também conferem.
- Seção típica: $a = -0{,}2$, $x_\alpha = 0{,}2$, $r_\alpha^2 = 0{,}373$ e $\mu = 4m_A/(\pi\rho c)$ estão corretos.
- Piston theory: $L$ e $M_{EA}$ foram integrados de novo na corda e conferem. A troca $M \to \sqrt{M^2-1}$ reproduz a inclinação de Ackeret.
- CLPT: $Q$, os seis termos de $\bar Q$ e as matrizes ABD conferem. Tecido ($E_1 = E_2$) a 45° dá $D_{16} = 0$, e o laminado do exemplo é simétrico e balanceado.
- Divergência e ISA conferem. A TN-4197 em SI também: as constantes 1,337 e 39,3 batem, e a fórmula é idêntica à do bloco `seguranca` do servidor.
- As três validações da §14 se reproduzem: 2,1839; 3,472 / 8,512 / 21,29 / 27,20; e $D_{11}$ exato.
- O exemplo da §7 (viga ≈141 Hz, placa ≈279 Hz) se reproduz com a sequência 0/45/0/45/45/0/45/0. O texto não informa a sequência.

**Problemas encontrados e o que a implementação faz:**

1. **YAML.** O PyYAML (YAML 1.1) lê `55.0e9` como **texto**, porque o expoente precisa de sinal. Com isso, o exemplo de uso quebra com `TypeError`. A §3.2 foi corrigida para `55.0e+9`, e o servidor aceita os dois formatos.
2. O laminado `L8_quasi` tem **6** lâminas. No workspace ele se chama `L6_quasi`.
3. **`run_case` podia pular o ponto de q máximo.** A subamostragem `traj[::len//40]` deixava esse ponto de fora. Na trajetória de teste, a TN-4197 dava 0,1158 em vez de 0,1152. Agora os pontos de q máximo e de V máxima sempre entram.
4. **`run_case` falhava em dois casos-limite.** Sem pontos com V > 30 m/s, ele quebrava (`crit` vazio). Sem flutter, devolvia `inf`, que não é JSON válido. Agora há um erro claro no primeiro caso e `null` com nota no segundo.
5. **Ponto não casado.** O V-g mantém o Mach do ponto da trajetória. No subsônico com margem > 1, o Mach real no flutter seria maior, e o Prandtl–Glauert aumenta a sustentação. Isso faz o $V_F$ ficar **superestimado**, ou seja, não conservador. O `matched_flutter_mach` existia, mas não era usado. Agora ele roda no ponto crítico, e o critério usa a menor das duas margens. No exemplo de dois estágios do OpenRocket, a margem das aletas do sustentador cai de 1,003 para 0,993.
6. **Critério transônico.** A §12 fala em "ponto crítico no transônico". A implementação aplica o limite de 1,7 a **todo** ponto transônico. Sem isso, um ponto transônico com 1,6 passaria só porque o ponto crítico era subsônico com 1,55.
7. **§13, passo 2.** Duas coisas impedem que a massa da aleta no OpenRocket seja $m_A \cdot S$:
   - a seção AIRFOIL ou ROUNDED tem menos volume que a placa (≈85% no Hemera);
   - com a massa sobrescrita (caso do Hemera), mudar a aleta não muda a massa.

   O servidor avisa nos dois casos e, no segundo, compensa o override.
8. **Estabilidade.** A mínima "na subida" do OpenRocket vai até o apogeu e inclui o trecho com velocidade quase zero. No exemplo de dois estágios ela dá 0,37 cal e reprova qualquer candidato. Por isso o resultado tem `aprovado_flutter` (critérios estruturais) separado de `aprovado` (inclui a estabilidade). A faixa de estabilidade continua sendo decisão da equipe.
9. **Classificação dos modos** (troca de sinal na ponta). Funcionou em todas as geometrias testadas: Hemera com enflechamento de 0 a 60° e o exemplo de dois estágios. Em aletas de corda muito grande, porém, um modo de flexão na corda pode ser lido como torção. Confira `modos_hz` e `modos_tipo`; as frequências medidas por tap test eliminam o problema.

**Dois estágios no OpenRocket 24.12.** O ramo 0 é o do sustentador e cobre o voo inteiro, inclusive a fase empilhada. O ramo do booster tem o nome do estágio e **começa em t = 0**, porque os dados de antes da separação são copiados. Cada conjunto de aletas usa o ramo do próprio estágio (seção 3.1).

**Desempenho.** O V-g resolve todos os $k$ de uma vez (mesmos números, com diferença de 1e-15). O `run_case` caiu de 3,6 s para 0,12 s.

**Pendente.** Os gráficos 1 e 2 da §13 ainda não são gerados. Os CSV de `comparar_aletas` (uma linha por candidato) e de `analisar_flutter` (margem ao longo do tempo) trazem os dados para eles.
