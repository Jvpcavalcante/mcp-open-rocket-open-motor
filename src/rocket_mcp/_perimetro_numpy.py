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
