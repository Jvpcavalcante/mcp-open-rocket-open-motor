"""fin_flutter.py — flutter de aletas compósitas: CLPT + Rayleigh-Ritz de placa + seção típica 2 GDL,
método V-g. Implementação de referência de docs/FLUTTER.md §15 (sem dependência do OpenRocket).
Unidades SI em tudo (m, kg, s, Pa, rad/s). Ângulos de lâmina em graus, medidos a partir da ENVERGADURA.

Diferenças em relação ao código do documento (os três testes da §14 continuam batendo; ver
tests/test_flutter.py, que também compara com números do código original):
- vg() resolve todos os k de uma vez (mesma equação, ~30× mais rápido) e flutter_from_vg() idem;
- run_case() sempre inclui os pontos de q máximo e de velocidade máxima (a subamostragem podia
  pulá-los), não quebra com trajetória vazia, aceita frequências medidas (tap test, §7), devolve a
  margem em cada ponto e verifica o ponto casado (Mach na velocidade de flutter) no ponto crítico.
Rode `python -m rocket_mcp.fin_flutter` antes de um estudo: os valores da §14 não podem mudar.
"""
import numpy as np
from numpy.polynomial import legendre as L
from scipy.linalg import eigh
from scipy.special import hankel2


# ---------- Material / laminado (Reddy, CLPT) ----------
def Q_matrix(E1, E2, G12, nu12):
    nu21 = nu12 * E2 / E1
    D = 1 - nu12 * nu21
    return np.array([[E1/D, nu12*E2/D, 0], [nu12*E2/D, E2/D, 0], [0, 0, G12]])


def Qbar(Q, theta_deg):
    m, n = np.cos(np.radians(theta_deg)), np.sin(np.radians(theta_deg))
    Q11, Q12, Q22, Q66 = Q[0, 0], Q[0, 1], Q[1, 1], Q[2, 2]
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
    A = np.zeros((3, 3)); B = np.zeros((3, 3)); D = np.zeros((3, 3)); mA = 0.0
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
    EI = c/d[0, 0]; GJ = 4*c/d[2, 2]     # x1 = direção da envergadura
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


# ---------- Aerodinâmica (k escalar ou vetor) ----------
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
KS_PADRAO = np.logspace(np.log10(20.0), np.log10(0.005), 2000)


def vg(mu, a, xa, ra2, wh, wa, b, aero, ks=None):
    """Retorna k, V (m/s), g, omega (rad/s), cada um (n_k, 2): os 2 ramos ao longo de k.
    Mesma equação do documento (§10), resolvida para todos os k de uma vez."""
    ks = KS_PADRAO if ks is None else np.asarray(ks, dtype=float)
    sig2 = (wh/wa)**2
    lh, la, mh, ma = aero(ks)
    P = mu + lh; Qd = mu*ra2 + ma; R = mu*xa + la; S = mu*xa + mh
    c2 = mu**2*sig2*ra2; c1 = -mu*(sig2*Qd + ra2*P); c0 = P*Qd - R*S
    disc = np.sqrt(c1*c1 - 4*c2*c0 + 0j)
    disc = np.where((np.conj(c1)*disc).real >= 0, disc, -disc)   # raízes estáveis (sem cancelamento)
    q = -0.5*(c1 + disc)
    Z = np.stack([q/c2, c0/q], axis=1)
    Z = np.take_along_axis(Z, np.argsort(-Z.real, axis=1), axis=1)  # Re Z maior -> omega menor
    with np.errstate(invalid="ignore", divide="ignore"):
        om = np.where(Z.real > 0, wa/np.sqrt(np.abs(Z.real)), np.nan)
        g = Z.imag/Z.real
    k = np.repeat(ks[:, None], 2, axis=1)
    return k, om*b/k, g, om


def flutter_from_vg(V, g, om, gs=0.0):
    """Menor V em que algum ramo cruza g = gs subindo. None = sem flutter na faixa de k."""
    best = None
    for j in range(V.shape[1]):
        if g[0, j] >= gs:   # já instável na menor velocidade -> artefato ou modelo fora da validade
            return (V[0, j], om[0, j], j)
        with np.errstate(invalid="ignore"):
            cruza = (g[:-1, j] < gs) & (gs <= g[1:, j]) & (V[1:, j] > V[:-1, j])
        i = np.flatnonzero(cruza)
        if i.size:
            i = i[0]
            t = (gs-g[i, j])/(g[i+1, j]-g[i, j])
            Vf = V[i, j] + t*(V[i+1, j]-V[i, j]); wf = om[i, j] + t*(om[i+1, j]-om[i, j])
            if best is None or Vf < best[0]:
                best = (Vf, wf, j)
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
    0.8..1.2: não há método linear confiável -> menor valor entre os dois extremos (flag).
    ATENÇÃO: o Mach fica fixo na varredura V-g (ponto "não casado"); ver matched_flutter_mach."""
    c, b, a, xa, ra2 = typical_section(mA, cr, ct, s, e_ea, eta_ref)
    mu = 4*mA/(np.pi*rho*c)

    def solve(Mx, kind):
        if kind == "theo":
            ae = lambda k: aero_theodorsen(k, a, beta=np.sqrt(1-Mx**2))  # noqa: E731
        else:
            ae = lambda k: aero_piston(k, a, np.sqrt(Mx**2-1))  # noqa: E731
        k, V, g, om = vg(mu, a, xa, ra2, wh, wa, b, ae)
        r = flutter_from_vg(V, g, om, gs)
        return np.inf if r is None else float(r[0])
    if M < transonic[0]:
        return solve(M, "theo"), "sub"
    if M > transonic[1]:
        return solve(M, "piston"), "super"
    return min(solve(transonic[0], "theo"), solve(transonic[1], "piston")), "transonico"


def matched_flutter_mach(wh, wa, mA, cr, ct, s, rho, sound, Ms=None, **kw):
    """Menor Mach em que a velocidade de voo alcança V_F (ponto 'casado'). inf se não ocorre até M_max."""
    Ms = np.r_[np.arange(0.1, 0.8, 0.05), 0.8, 1.0, 1.2, np.arange(1.3, 5.01, 0.1)] if Ms is None else Ms
    for M in Ms:
        Vf, reg = flutter_speed_at(M, wh, wa, mA, cr, ct, s, rho, **kw)
        if M*sound >= Vf:
            return M, Vf, reg
    return np.inf, np.inf, "nenhum"


def trajectory_margin(traj, wh, wa, mA, cr, ct, s, **kw):
    """traj: iterável de (t, V, M, rho). Retorna lista (t, V, M, V_F, margem, regime) e o ponto crítico."""
    rows = []
    for t, V, M, rho in traj:
        if V < 30:
            continue
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
    if e <= 0:
        return np.inf
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
def run_case(fin, plies, materials, traj, e_ea=0.40, eta_ref=0.75, gs=0.0, freqs_hz=None, n_pontos=40):
    """fin: dict cr, ct, s, sweep_le_deg. traj: lista (t, V, M, rho, p, sound).
    freqs_hz: (f_h, f_a) medidos (tap test) substituem os da placa.
    Retorna dicionário-resumo para a tabela do estudo paramétrico; 'pontos' traz a margem em cada
    instante avaliado. Margens inf = sem flutter (ou sem divergência) na faixa analisada."""
    A, B, D, h, mA = laminate(plies, materials)
    geo = (fin["cr"], fin["ct"], fin["s"])
    if freqs_hz is not None:
        wh, wa = 2*np.pi*freqs_hz[0], 2*np.pi*freqs_hz[1]
        om, kinds = None, None
    else:
        om, kinds = plate_modes(D, mA, *geo, fin.get("sweep_le_deg", 0.0))
        wh, wa = pick_modes(om, kinds)
    traj = list(traj)
    q = [0.5*r[3]*r[1]**2 for r in traj]
    qmax = max(q, default=0.0)
    # só pontos relevantes (q >= 10% de q_max), no máximo ~n_pontos, mais q máximo e V máxima
    rel = [i for i, r in enumerate(traj) if q[i] >= 0.1*qmax and r[1] > 30]
    if not rel:
        raise ValueError("trajetória sem pontos com V > 30 m/s: nada a verificar")
    escolhidos = set(rel[::max(1, len(rel)//n_pontos)])
    escolhidos |= {max(rel, key=q.__getitem__), max(rel, key=lambda i: traj[i][1])}
    traj = [traj[i] for i in sorted(escolhidos)]
    kw = dict(e_ea=e_ea, eta_ref=eta_ref, gs=gs)
    rows, crit = trajectory_margin([(t, V, M, rho) for t, V, M, rho, p, a in traj], wh, wa, mA, *geo, **kw)
    t_c, V_c, M_c, Vf_c, marg, reg = crit
    r_c = next(r for r in traj if r[0] == t_c)
    M_cas, Vf_cas, reg_cas = matched_flutter_mach(wh, wa, mA, *geo, r_c[3], r_c[5], **kw)
    qD = min(divergence_q(mA, wa, *geo, r[2], e_ea, eta_ref)/(0.5*r[3]*r[1]**2) for r in traj)
    G_E = 12*D[2, 2]/h**3
    tn = min(tn4197(G_E, *geo, h, r[4], r[5])/r[1] for r in traj)
    out = dict(h_mm=h*1e3, mA=mA, rho_eq=mA/h, f_h=wh/2/np.pi, f_a=wa/2/np.pi,
               B_max=float(np.abs(B).max()), D16_D11=float(abs(D[0, 2])/D[0, 0]),
               margem_flutter=marg, t_critico=t_c, M_critico=M_c, V_critico=V_c, V_F_critico=Vf_c,
               regime_critico=reg, margem_q_divergencia=qD, margem_TN4197=tn, q_max=qmax,
               # extras desta implementação
               M_flutter_casado=M_cas, V_F_casado=Vf_cas, margem_flutter_casada=Vf_cas/V_c,
               regime_casado=reg_cas, B_relativo=float(np.abs(B).max()/(np.abs(A).max()*h)),
               D11=D[0, 0], D22=D[1, 1], D12=D[0, 1], D66=D[2, 2], G_E=G_E,
               frequencias_medidas=freqs_hz is not None)
    out = {k: (float(v) if isinstance(v, (np.floating, float, int)) and not isinstance(v, bool) else v)
           for k, v in out.items()}
    out["modos_hz"] = None if om is None else [float(w/2/np.pi) for w in om]
    out["modos_tipo"] = kinds
    out["pontos"] = [dict(t=float(t), V=float(V), M=float(M), V_F=float(Vf), margem=float(m), regime=rg)
                     for t, V, M, Vf, m, rg in rows]
    return out


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
