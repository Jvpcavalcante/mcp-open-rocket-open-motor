"""Geometria de aletas sem dependência do OpenRocket (testável sem JVM). Base do estudo de flutter.

Convenções (as do OpenRocket): corda na raiz cr, corda na ponta ct, envergadura s (o "height" da GUI),
deslocamento do bordo de ataque da ponta em relação ao da raiz (sweep length, positivo para trás) e
espessura t. Tudo em SI.
"""
from __future__ import annotations

import math


def geometria_trapezio(cr: float, ct: float, s: float, sweep: float, t: float) -> dict:
    """Grandezas derivadas de UMA aleta trapezoidal, em mm, mm² e graus.
    - alongamento = s²/S de uma aleta (a definição da fórmula de flutter da NACA TN-4197; o alongamento
      do par de aletas opostas, comum em aerodinâmica de asas, é o dobro);
    - enflechamento da meia corda: o ângulo que entra no CNα do Barrowman;
    - corda média aerodinâmica (CMA) e sua posição: y medido da raiz, x do bordo de ataque da raiz."""
    area = (cr + ct) / 2 * s
    lam = ct / cr if cr > 0 else float("nan")
    cma = 2 / 3 * cr * (1 + lam + lam * lam) / (1 + lam) if cr > 0 else float("nan")
    y_cma = s / 3 * (1 + 2 * lam) / (1 + lam) if cr > 0 else float("nan")
    x_cma = sweep * y_cma / s if s > 0 else 0.0
    graus = lambda x: math.degrees(math.atan2(x, s)) if s > 0 else float("nan")  # noqa: E731
    return {
        "area_mm2": _r(area * 1e6, 1),
        "alongamento": _r(s * s / area, 4) if area > 0 else None,
        "afilamento": _r(lam, 4),
        "enflechamento_bordo_ataque_deg": _r(graus(sweep), 2),
        "enflechamento_meia_corda_deg": _r(graus(sweep + ct / 2 - cr / 2), 2),
        "enflechamento_bordo_fuga_deg": _r(graus(sweep + ct - cr), 2),
        "corda_media_aerodinamica_mm": _r(cma * 1e3, 2),
        "y_cma_mm": _r(y_cma * 1e3, 2),
        "x_cma_mm": _r(x_cma * 1e3, 2),
        "espessura_relativa_raiz": _r(t / cr, 5) if cr > 0 else None,
        "espessura_relativa_cma": _r(t / cma, 5) if cma > 0 else None,
    }


def _r(x: float, n: int) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, n)
