"""纯标准库统计工具：均值、中位数、分位数、Bootstrap 置信区间、稳健 z 分数。"""
from __future__ import annotations

import math
import random
from typing import Sequence

NAN = float("nan")


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else NAN


def median(xs: Sequence[float]) -> float:
    if not xs:
        return NAN
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def percentile(xs: Sequence[float], p: float) -> float:
    """线性插值分位数，p ∈ [0, 1]。"""
    if not xs:
        return NAN
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def bootstrap_ci(
    values: Sequence[float],
    iters: int = 1000,
    alpha: float = 0.05,
    seed: int = 20261119,
) -> tuple[float, float]:
    """对均值做 Bootstrap 置信区间（按天重采样）。"""
    vals = list(values)
    n = len(vals)
    if n == 0:
        return (NAN, NAN)
    if n == 1:
        return (vals[0], vals[0])
    rng = random.Random(seed)
    stats = []
    for _ in range(iters):
        total = 0.0
        for _ in range(n):
            total += vals[rng.randrange(n)]
        stats.append(total / n)
    return (percentile(stats, alpha / 2), percentile(stats, 1 - alpha / 2))


def mad(xs: Sequence[float]) -> float:
    """中位绝对偏差。"""
    if not xs:
        return NAN
    med = median(xs)
    return median([abs(x - med) for x in xs])


def robust_z(x: float, xs: Sequence[float]) -> float:
    """基于中位数/MAD 的稳健 z 分数，用于回补数据离群检测。"""
    if not xs:
        return 0.0
    med = median(xs)
    m = mad(xs)
    if m == 0:
        return 0.0 if x == med else math.copysign(float("inf"), x - med)
    return (x - med) / (1.4826 * m)
