"""时间边界验证与回补数据验证。所有检查返回统一结构，供页面展示与流水线门禁使用。"""
from __future__ import annotations

import datetime as dt
from typing import Iterable

from .stats import robust_z

Check = dict


def _check(cid: str, name: str, passed: bool, detail: str, severity: str = "error") -> Check:
    return {"id": cid, "name": name, "passed": bool(passed), "severity": severity, "detail": detail}


def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def validate_boundaries(windows: dict, params, data_range: tuple[str, str] | None,
                        matched_pairs: dict | None = None) -> list[Check]:
    """时间边界检查：

    1. pre/post 窗口不重叠且 revamp_date 恰为 post 起点；
    2. 窗口连续、长度与参数一致；
    3. 匹配对不跨越 revamp_date（无后视泄漏）；
    4. 去年同期窗口与主窗口等长、星期对齐（364 天偏移）、不重叠；
    5. 数据覆盖所有窗口日期范围；
    6. 窗口内星期分布均衡（7 的整数倍时每个星期几出现次数一致）。
    """
    checks: list[Check] = []
    pre, post = windows["pre"], windows["post"]
    revamp = params.revamp_date

    ok = max(pre) < revamp <= min(post) and revamp not in pre
    checks.append(_check(
        "pre_post_no_overlap", "pre/post 窗口不重叠，上线日为 post 起点",
        ok, f"pre=[{min(pre)}..{max(pre)}] post=[{min(post)}..{max(post)}] revamp={revamp}"))

    def _contiguous(ds):
        return all((ds[i + 1] - ds[i]).days == 1 for i in range(len(ds) - 1))

    ok = len(pre) == params.pre_days and _contiguous(pre)
    checks.append(_check(
        "pre_window_shape", "pre 窗口连续且长度等于 pre_days",
        ok, f"len(pre)={len(pre)} pre_days={params.pre_days} contiguous={_contiguous(pre)}"))
    ok = len(post) == params.post_days and _contiguous(post)
    checks.append(_check(
        "post_window_shape", "post 窗口连续且长度等于 post_days",
        ok, f"len(post)={len(post)} post_days={params.post_days} contiguous={_contiguous(post)}"))

    if matched_pairs is not None:
        leaks = [(str(d), [str(m) for m in ms if m >= revamp])
                 for d, ms in matched_pairs.items()]
        leaks = [(d, m) for d, m in leaks if m]
        checks.append(_check(
            "matching_no_leakage", "匹配窗口不跨越上线日（无后视泄漏）",
            not leaks, f"泄漏对数={len(leaks)}" + (f" 示例={leaks[:3]}" if leaks else "")))

    if params.yoy_control:
        ypre, ypost = windows["yoy_pre"], windows["yoy_post"]
        same_len = len(ypre) == len(pre) and len(ypost) == len(post)
        aligned = all((p - y).days == 364 for p, y in zip(pre, ypre)) and \
                  all((p - y).days == 364 for p, y in zip(post, ypost))
        no_overlap = max(ypost) < min(pre)
        checks.append(_check(
            "yoy_alignment", "去年同期窗口等长、星期对齐(364d)、与主窗口不重叠",
            same_len and aligned and no_overlap,
            f"yoy_pre=[{min(ypre)}..{max(ypre)}] yoy_post=[{min(ypost)}..{max(ypost)}] "
            f"len_ok={same_len} weekday_aligned={aligned} no_overlap={no_overlap}"))

    if data_range:
        lo, hi = _d(data_range[0]), _d(data_range[1])
        need = [min(pre), max(post)]
        if params.yoy_control:
            need += [min(windows["yoy_pre"]), max(windows["yoy_post"])]
        ok = lo <= min(need) and hi >= max(need)
        checks.append(_check(
            "data_coverage", "数据集覆盖全部窗口日期范围",
            ok, f"data=[{lo}..{hi}] need=[{min(need)}..{max(need)}]"))
    else:
        checks.append(_check("data_coverage", "数据集覆盖全部窗口日期范围", False, "数据集为空"))

    def _weekday_balance(ds):
        counts: dict[int, int] = {}
        for d in ds:
            counts[d.weekday()] = counts.get(d.weekday(), 0) + 1
        return len(set(counts.values())) <= 1

    if params.pre_days % 7 == 0:
        checks.append(_check(
            "weekday_balance_pre", "pre 窗口星期分布均衡",
            _weekday_balance(pre), f"pre_days={params.pre_days}", severity="warning"))
    if params.post_days % 7 == 0:
        checks.append(_check(
            "weekday_balance_post", "post 窗口星期分布均衡",
            _weekday_balance(post), f"post_days={params.post_days}", severity="warning"))

    return checks


def validate_backfill(rows: list[dict], windows: dict, backfill_policy: str,
                      final_cells: Iterable[tuple] | None = None) -> tuple[list[Check], list[dict]]:
    """回补数据检查，返回 (checks, outlier_rows)：

    1. 回补行必须带 backfilled_at 且不早于业务日期；
    2. 回补值相对同层同段非回补序列的稳健 z 分数离群检测；
    3. 回补行在各窗口的分布（信息项）；
    4. 策略一致性：exclude 策略下最终样本不得含回补单元（按 日期×层×段 粒度）。
    """
    checks: list[Check] = []
    bf = [r for r in rows if r["is_backfilled"]]

    bad_ts = [r for r in bf if not r["backfilled_at"] or _d(r["backfilled_at"]) < _d(r["date"])]
    checks.append(_check(
        "backfill_timestamp", "回补行 backfilled_at 存在且不早于业务日期",
        not bad_ts, f"回补行数={len(bf)} 异常时间戳={len(bad_ts)}"))

    # 离群检测：同 (stratum, segment) 非回补值做基线
    baseline: dict[tuple, list[float]] = {}
    for r in rows:
        if not r["is_backfilled"]:
            baseline.setdefault((r["stratum"], r["segment"]), []).append(r["value"])
    outliers = []
    for r in bf:
        z = robust_z(r["value"], baseline.get((r["stratum"], r["segment"]), []))
        if abs(z) > 5:
            outliers.append({**r, "robust_z": round(z, 2)})
    checks.append(_check(
        "backfill_outlier", "回补值离群检测（稳健 z > 5 告警）",
        not outliers, f"离群回补行={len(outliers)}"
        + (f" 示例={[(o['date'], o['stratum'], o['segment'], o['robust_z']) for o in outliers[:3]]}" if outliers else "")))

    pre_set, post_set = set(map(str, windows["pre"])), set(map(str, windows["post"]))
    dist = {"pre": 0, "post": 0, "other": 0}
    for r in bf:
        d = r["date"]
        dist["pre" if d in pre_set else "post" if d in post_set else "other"] += 1
    checks.append(_check(
        "backfill_distribution", "回补行窗口分布（信息）",
        True, f"pre窗口={dist['pre']} post窗口={dist['post']} 窗口外={dist['other']}",
        severity="info"))

    if backfill_policy == "exclude" and final_cells is not None:
        cells = {(r["date"], r["stratum"], r["segment"]) for r in bf}
        leaked = sorted(cells & set(final_cells))
        checks.append(_check(
            "backfill_policy_consistency", "exclude 策略下最终样本不含回补单元",
            not leaked, f"泄漏回补单元数={len(leaked)}"))

    return checks, outliers
