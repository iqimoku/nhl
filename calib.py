# -*- coding: utf-8 -*-
"""
Kalibracja modelu na podstawie backtestu.

Dla każdego rynku poprawiamy oczekiwaną liczbę zdarzeń:  lambda' = a · lambda^b
  b < 1  -> prognozy "ściągane" do środka (model był zbyt pewny przy wysokich szansach),
  a      -> ogólny poziom (model zaniżał / zawyżał średnio).
Gol / asysta / punkt: dopasowanie do zdarzenia "co najmniej 1" (rynek anytime).
Strzały: dopasowanie do pełnej liczby strzałów z rozkładem ujemnym dwumianowym (NB),
  który ma większy rozrzut niż Poisson (parametr r: im mniejszy, tym większy rozrzut).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

FILE_NAME = "calibration.json"


# ---------------------------------------------------------------- rozkłady
def nb_ge(lam: float, r: float, k: int) -> float:
    """P(X >= k) dla ujemnego dwumianowego o średniej lam i parametrze r."""
    if lam <= 0:
        return 0.0
    q = lam / (r + lam)
    pmf = (r / (r + lam)) ** r
    cdf = 0.0
    for i in range(k):
        if i > 0:
            pmf *= (r + i - 1) / i * q
        cdf += pmf
    return max(0.0, min(1.0, 1.0 - cdf))


def apply(cal: dict | None, market: str, lam: float) -> float:
    if not cal or market not in cal or lam <= 0:
        return lam
    c = cal[market]
    return c["a"] * lam ** c["b"]


def load(base: Path):
    p = Path(base) / FILE_NAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(cal: dict, base: Path):
    (Path(base) / FILE_NAME).write_text(json.dumps(cal, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- dopasowanie
def _grid(lo, hi, n):
    return np.linspace(lo, hi, n)


def fit_binary(lam: np.ndarray, y: np.ndarray):
    """Maksymalizuje wiarygodność P(X>=1) = 1 - exp(-a·lam^b)."""
    lam = np.clip(lam, 1e-4, None)
    ll_best, best = -np.inf, (1.0, 1.0)
    loglam = np.log(lam)

    def ll(a, b):
        m = a * np.exp(b * loglam)
        p = np.clip(1 - np.exp(-m), 1e-6, 1 - 1e-6)
        return float(np.sum(y * np.log(p) + (1 - y) * np.log(1 - p)))

    for a_rng, b_rng, n in (((0.5, 1.6), (0.5, 1.4), 23), (None, None, 15)):
        if a_rng is None:  # zawężenie wokół najlepszego punktu
            a0, b0 = best
            a_rng, b_rng = (a0 * 0.92, a0 * 1.08), (b0 - 0.06, b0 + 0.06)
        for a in _grid(*a_rng, n):
            for b in _grid(*b_rng, n):
                v = ll(a, b)
                if v > ll_best:
                    ll_best, best = v, (float(a), float(b))
    return {"a": round(best[0], 4), "b": round(best[1], 4)}


def fit_nb(lam: np.ndarray, x: np.ndarray):
    """Strzały: maksymalizuje wiarygodność NB(średnia = a·lam^b, r)."""
    lam = np.clip(lam, 1e-3, None)
    loglam = np.log(lam)
    x = x.astype(int)
    xmax = int(x.max()) + 1
    best, ll_best = (1.0, 1.0, 1000.0), -np.inf

    def ll(a, b, r):
        m = a * np.exp(b * loglam)
        # log Γ(x+r) - log Γ(r) = Σ_{i<x} log(r+i)
        tab = np.concatenate([[0.0], np.cumsum(np.log(r + np.arange(xmax)))])
        return float(np.sum(tab[x] + r * np.log(r / (r + m)) + x * np.log(m / (r + m))))

    rs = [2, 3, 4, 5, 6, 8, 10, 13, 17, 25, 40, 80, 300, 2000]
    for a in _grid(0.6, 1.4, 17):
        for b in _grid(0.6, 1.3, 15):
            for r in rs:
                v = ll(a, b, r)
                if v > ll_best:
                    ll_best, best = v, (float(a), float(b), float(r))
    a0, b0, r0 = best
    for a in _grid(a0 * 0.95, a0 * 1.05, 11):
        for b in _grid(b0 - 0.04, b0 + 0.04, 9):
            for r in np.geomspace(max(1.0, r0 / 1.6), r0 * 1.6, 9):
                v = ll(a, b, r)
                if v > ll_best:
                    ll_best, best = v, (float(a), float(b), float(r))
    return {"a": round(best[0], 4), "b": round(best[1], 4), "r": round(best[2], 2)}


def fit_all(D: dict) -> dict:
    """D: słownik tablic numpy: lam_g, lam_a, lam_p, lam_s, g, a, p, s."""
    return {
        "g": fit_binary(D["lam_g"], (D["g"] > 0).astype(float)),
        "a": fit_binary(D["lam_a"], (D["a"] > 0).astype(float)),
        "p": fit_binary(D["lam_p"], (D["p"] > 0).astype(float)),
        "s": fit_nb(D["lam_s"], D["s"]),
    }


# ---------------------------------------------------------------- prognozy dla oceny
def probs(D: dict, cal: dict | None):
    """Zwraca słownik etykieta -> (prognozy, wyniki 0/1)."""
    def lam(k):
        x = D["lam_" + k]
        if cal and k in cal:
            return cal[k]["a"] * np.clip(x, 1e-6, None) ** cal[k]["b"]
        return x

    out = {}
    for label, k in (("Gol 1+", "g"), ("Asysta 1+", "a"), ("Punkt 1+", "p")):
        out[label] = (1 - np.exp(-lam(k)), (D[k] > 0).astype(float))
    ls = lam("s")
    r = cal["s"].get("r") if cal and "s" in cal else None
    for t in (2, 3, 4):
        if r:
            p = np.array([nb_ge(float(m), r, t) for m in ls])
        else:
            p = np.array([_pois_ge(float(m), t) for m in ls])
        out[f"Strzały {t}+"] = (p, (D["s"] >= t).astype(float))
    return out


def _pois_ge(lam, k):
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        if i > 0:
            term *= lam / i
        cdf += term
    return 1 - cdf
