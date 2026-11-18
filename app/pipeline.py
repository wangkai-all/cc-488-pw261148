"""对照分析流水线：样本流 → 口径调整 → 回补/缺失/事件处理 → 匹配 → 分层效应 → 对照 → 敏感性。

每个阶段产出证据（输入摘要 + 哈希），最终形成证据链；结果包含影响范围而非单一差值。
"""
from __future__ import annotations

import datetime as dt
import itertools
import subprocess
from typing import Iterable

from . import APP_VERSION
from .schemas import AnalysisParams, canonical_json, short_hash
from .stats import bootstrap_ci, mean
from .validate import validate_backfill, validate_boundaries

DATE = dt.date


def code_version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd="/workspace", timeout=5,
        ).stdout.strip()
        return f"app-{APP_VERSION}@{sha}" if sha else f"app-{APP_VERSION}"
    except Exception:
        return f"app-{APP_VERSION}"


def daterange(d0: DATE, d1: DATE) -> list[DATE]:
    out, d = [], d0
    while d <= d1:
        out.append(d)
        d += dt.timedelta(days=1)
    return out


def build_windows(p: AnalysisParams) -> dict:
    """pre = [revamp-pre_days, revamp-1]；post = [revamp, revamp+post_days-1]；
    去年同期窗口按 364 天（52 周）偏移，保证星期对齐。"""
    pre = daterange(p.revamp_date - dt.timedelta(days=p.pre_days), p.revamp_date - dt.timedelta(days=1))
    post = daterange(p.revamp_date, p.revamp_date + dt.timedelta(days=p.post_days - 1))
    yoy_pre = [d - dt.timedelta(days=364) for d in pre]
    yoy_post = [d - dt.timedelta(days=364) for d in post]
    return {"pre": pre, "post": post, "yoy_pre": yoy_pre, "yoy_post": yoy_post}


class Evidence:
    """证据链：每一步记录名称、摘要与内容哈希。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def log(self, name: str, detail: dict) -> None:
        self.steps.append({
            "step": len(self.steps) + 1,
            "name": name,
            "hash": short_hash(detail),
            "detail": detail,
        })


# ---------------------------------------------------------------- 口径

def detect_caliber(rows: list[dict], registry: list[dict], metric: str) -> dict:
    """检测窗口内口径版本分布与切换日。"""
    calibers = sorted({r["caliber"] for r in rows})
    info = {"metric": metric, "calibers": calibers, "changed": len(calibers) > 1,
            "switch_date": None, "definitions": {}}
    for reg in registry:
        if reg["metric"] == metric:
            info["definitions"][reg["caliber"]] = {
                "effective_from": reg["effective_from"],
                "effective_to": reg["effective_to"],
                "definition": reg["definition"],
            }
    if info["changed"]:
        old, new = calibers[0], calibers[-1]
        last_old = max(r["date"] for r in rows if r["caliber"] == old)
        first_new = min(r["date"] for r in rows if r["caliber"] == new)
        # 切换日 = 旧口径最后出现日 + 1；重叠期 = 新口径提前双报的区间
        switch = dt.date.fromisoformat(last_old) + dt.timedelta(days=1)
        info["switch_date"] = switch.isoformat()
        info["overlap_start"] = first_new if first_new < info["switch_date"] else None
    return info


def estimate_caliber_factor(rows: list[dict], old: str, new: str) -> tuple[float | None, str, int]:
    """用双报重叠期估计口径换算因子：factor = Σnew / Σold（按 key 匹配）。"""
    by_key: dict[tuple, dict[str, float]] = {}
    for r in rows:
        if r["caliber"] in (old, new):
            by_key.setdefault((r["date"], r["stratum"], r["segment"]), {})[r["caliber"]] = r["value"]
    pairs = [v for v in by_key.values() if old in v and new in v]
    if pairs:
        num = sum(v[new] for v in pairs)
        den = sum(v[old] for v in pairs)
        days = len({k for k, v in by_key.items() if old in v and new in v})
        return (num / den if den else None), "overlap_ratio", days
    return None, "no_overlap", 0


def apply_caliber(rows: list[dict], p: AnalysisParams, registry: list[dict]) -> tuple[list[dict], dict]:
    """口径归一：把旧口径值换算到最新口径基准。"""
    info = detect_caliber(rows, registry, p.metric)
    info.update({"policy": p.caliber_policy, "adjustment_factor": None, "rescaled_rows": 0})
    if not info["changed"] or p.caliber_policy == "flag_only":
        return list(rows), info
    calibers = info["calibers"]
    old, new = calibers[0], calibers[-1]
    factor, method, overlap_days = estimate_caliber_factor(rows, old, new)
    info.update({"adjustment_factor": factor, "method": method, "overlap_days": overlap_days})
    if factor is None:
        return list(rows), info
    out = []
    for r in rows:
        r = dict(r)
        if r["caliber"] == old:
            r["value"] = r["value"] * factor
            r["caliber_rescaled"] = True
            info["rescaled_rows"] += 1
        out.append(r)
    return out, info


# ---------------------------------------------------------------- 序列准备

def _key(r: dict) -> tuple[str, str]:
    return (r["stratum"], r["segment"])


def prepare_series(rows: list[dict], p: AnalysisParams, windows: dict,
                   events: list[dict], flow: list[dict] | None = None) -> tuple[dict, dict]:
    """回补策略 → 序列构建 → 缺失期处理 → 事件处理。返回 {(stratum, segment): {date: value}}。

    flow 非空时记录样本流各阶段计数（仅基准运行）。
    """
    def stage(name, label, before, after, note):
        if flow is not None:
            flow.append({"stage": name, "label": label, "in": before, "out": after,
                         "dropped": before - after, "note": note})

    stage("raw", "原始行（指标=%s）" % p.metric, len(rows), len(rows), "按数据版本加载")

    # 1. 回补策略
    n0 = len(rows)
    if p.backfill_policy == "exclude":
        rows = [r for r in rows if not r["is_backfilled"]]
    stage("backfill", "回补数据处理", n0, len(rows), f"policy={p.backfill_policy}")

    # 2. 构建序列（限定窗口内日期；同 key 同日期存在多口径双报时保留最新口径）
    win_dates = set(map(str, windows["pre"] + windows["post"]))
    if p.yoy_control:
        win_dates |= set(map(str, windows["yoy_pre"] + windows["yoy_post"]))
    scoped = [r for r in rows if r["date"] in win_dates]
    stage("window", "进入分析窗口", len(rows), len(scoped), "pre/post" + ("/yoy" if p.yoy_control else ""))

    best: dict[tuple, dict] = {}
    for r in scoped:
        k = (r["stratum"], r["segment"], r["date"])
        if k not in best or r["caliber"] >= best[k]["caliber"]:
            best[k] = r
    deduped = list(best.values())
    stage("caliber_dedup", "口径双报去重", len(scoped), len(deduped), "同 key 同日期保留最新口径")

    series: dict[tuple[str, str], dict[str, float]] = {}
    for r in deduped:
        series.setdefault(_key(r), {})[r["date"]] = r["value"]

    # 3. 缺失期处理
    expected = set(map(str, windows["pre"] + windows["post"]))
    if p.yoy_control:
        expected |= set(map(str, windows["yoy_pre"] + windows["yoy_post"]))
    missing_cells: list[tuple[str, str, str]] = []
    interpolated = 0
    for key, vals in series.items():
        for d in sorted(expected - set(vals)):
            missing_cells.append((key[0], key[1], d))
            if p.missing_policy == "interpolate":
                neigh = []
                dd = DATE.fromisoformat(d)
                for k in range(1, 9):
                    for cand in (dd - dt.timedelta(days=7 * k), dd + dt.timedelta(days=7 * k)):
                        cs = cand.isoformat()
                        if cs in vals and cs in expected:
                            neigh.append(vals[cs])
                if neigh:
                    vals[d] = mean(neigh)
                    interpolated += 1
    stage("missing", "缺失期处理", len(deduped), len(deduped) + interpolated,
          f"policy={p.missing_policy} 缺失单元={len(missing_cells)} 插补={interpolated}")

    # 4. 事件处理
    event_dates: dict[str, str] = {}
    for ev in events:
        for d in daterange(DATE.fromisoformat(ev["start"]), DATE.fromisoformat(ev["end"])):
            ds = d.isoformat()
            if ds in expected:
                event_dates[ds] = ev["name"]
    removed_events = 0
    if p.event_policy == "exclude":
        for vals in series.values():
            for d in list(vals):
                if d in event_dates:
                    del vals[d]
                    removed_events += 1
    stage("event", "异常事件处理", len(deduped) + interpolated,
          len(deduped) + interpolated - removed_events,
          f"policy={p.event_policy} 事件日={len(event_dates)} 剔除单元={removed_events}")

    info = {
        "missing_cells": [{"stratum": s, "segment": g, "date": d} for s, g, d in missing_cells],
        "interpolated": interpolated,
        "event_dates": event_dates,
    }
    return series, info


# ---------------------------------------------------------------- 匹配与效应

def build_pairs(vals: dict[str, float], pre_dates: Iterable[DATE], post_dates: Iterable[DATE],
                weeks: int) -> dict[str, dict]:
    """每个 post 日匹配 pre 窗口内同星期的前 N 周日期（绝不跨越 revamp）。"""
    pre_set = {d.isoformat() for d in pre_dates}
    pairs: dict[str, dict] = {}
    for d in post_dates:
        ds = d.isoformat()
        if ds not in vals:
            continue
        matches = []
        for k in range(1, weeks + 1):
            m = (d - dt.timedelta(days=7 * k)).isoformat()
            if m in pre_set and m in vals:
                matches.append(m)
        if matches:
            pairs[ds] = {"value": vals[ds], "matches": matches,
                         "base": mean([vals[m] for m in matches])}
    return pairs


def paired_effect(vals: dict[str, float], pre_dates, post_dates, p: AnalysisParams,
                  boot: bool = True) -> dict:
    """基于匹配对的日级效应：rel = post/匹配基线 - 1。"""
    pairs = build_pairs(vals, pre_dates, post_dates, p.matching_window_weeks)
    rel = {d: v["value"] / v["base"] - 1 for d, v in pairs.items() if v["base"]}
    absd = {d: v["value"] - v["base"] for d, v in pairs.items()}
    pre_vals = [vals[d.isoformat()] for d in pre_dates if d.isoformat() in vals]
    post_vals = [vals[d.isoformat()] for d in post_dates if d.isoformat() in vals]
    out = {
        "n_post_days": len(pairs),
        "unmatched_post_days": len(post_dates) - len(pairs),
        "pre_mean": mean(pre_vals),
        "post_mean": mean(post_vals),
        "effect_pct": mean(list(rel.values())),
        "effect_abs": mean(list(absd.values())),
        "daily_rel": rel,
        "daily_abs": absd,
        "pairs": pairs,
    }
    if boot:
        lo, hi = bootstrap_ci(list(rel.values()), p.bootstrap_iters, p.alpha)
        out["ci_pct"] = [lo, hi]
    return out


def _adjusted_daily(strata_eff: dict, weights: dict[str, float], segment: str) -> dict[str, float]:
    """按 pre 期固定权重聚合各层日级相对效应（构成调整）。"""
    days: set[str] = set()
    for s, eff in strata_eff.items():
        days |= set(eff[segment]["daily_rel"])
    out = {}
    for d in sorted(days):
        num = den = 0.0
        for s, eff in strata_eff.items():
            r = eff[segment]["daily_rel"].get(d)
            w = weights.get(s, 0.0)
            if r is not None and w:
                num += w * r
                den += w
        if den:
            out[d] = num / den
    return out


def estimate_effects(series: dict, windows: dict, p: AnalysisParams, boot: bool = True) -> dict:
    """分层效应 + 构成调整总体 + 朴素总体 + DiD + 同期对照。"""
    strata = sorted({k[0] for k in series})
    # pre 期处理组份额作为固定权重
    pre_sums = {}
    for s in strata:
        vals = series.get((s, p.treatment_segment), {})
        pre_sums[s] = sum(vals.get(d.isoformat(), 0.0) for d in windows["pre"])
    total = sum(pre_sums.values()) or 1.0
    weights = {s: v / total for s, v in pre_sums.items()}

    strata_eff: dict[str, dict] = {}
    for s in strata:
        eff = {}
        for seg in (p.treatment_segment, p.control_segment):
            vals = series.get((s, seg), {})
            eff[seg] = paired_effect(vals, windows["pre"], windows["post"], p, boot=boot)
        t_rel, c_rel = eff[p.treatment_segment]["daily_rel"], eff[p.control_segment]["daily_rel"]
        did_daily = {d: t_rel[d] - c_rel.get(d, 0.0) for d in t_rel if d in c_rel}
        eff["did"] = {"effect_pct": mean(list(did_daily.values())), "daily_rel": did_daily}
        if boot:
            lo, hi = bootstrap_ci(list(did_daily.values()), p.bootstrap_iters, p.alpha)
            eff["did"]["ci_pct"] = [lo, hi]
        strata_eff[s] = eff

    adj_t = _adjusted_daily(strata_eff, weights, p.treatment_segment)
    adj_c = _adjusted_daily(strata_eff, weights, p.control_segment)
    did_daily = {d: adj_t[d] - adj_c[d] for d in adj_t if d in adj_c}

    # 朴素总体（不调整构成）：按日汇总后直接配对
    pooled: dict[str, dict[str, float]] = {p.treatment_segment: {}, p.control_segment: {}}
    for (s, seg), vals in series.items():
        if seg in pooled:
            for d, v in vals.items():
                pooled[seg][d] = pooled[seg].get(d, 0.0) + v
    naive = paired_effect(pooled[p.treatment_segment], windows["pre"], windows["post"], p, boot=False)

    overall = {
        "weights": weights,
        "adjusted": {
            "effect_pct": mean(list(adj_t.values())),
            "daily_rel": adj_t,
        },
        "naive": {"effect_pct": naive["effect_pct"]},
        "control_effect_pct": mean(list(adj_c.values())),
        "did": {"effect_pct": mean(list(did_daily.values())), "daily_rel": did_daily},
    }
    overall["composition_impact_pp"] = (naive["effect_pct"] - overall["adjusted"]["effect_pct"]) * 100
    if boot:
        overall["adjusted"]["ci_pct"] = list(bootstrap_ci(list(adj_t.values()), p.bootstrap_iters, p.alpha))
        overall["did"]["ci_pct"] = list(bootstrap_ci(list(did_daily.values()), p.bootstrap_iters, p.alpha))

    # 同期对照（去年同期，同样匹配规则）
    yoy = None
    if p.yoy_control:
        yoy_daily_all: dict[str, dict[str, float]] = {}
        for s in strata:
            vals = series.get((s, p.treatment_segment), {})
            yoy_daily_all[s] = paired_effect(vals, windows["yoy_pre"], windows["yoy_post"], p, boot=False)["daily_rel"]
        days = set().union(*[set(v) for v in yoy_daily_all.values()]) if yoy_daily_all else set()
        yoy_daily = {}
        for d in sorted(days):
            num = den = 0.0
            for s, rel in yoy_daily_all.items():
                if d in rel and weights.get(s):
                    num += weights[s] * rel[d]
                    den += weights[s]
            if den:
                yoy_daily[d] = num / den
        yoy = {"effect_pct": mean(list(yoy_daily.values())), "daily_rel": yoy_daily}
        if boot:
            yoy["ci_pct"] = list(bootstrap_ci(list(yoy_daily.values()), p.bootstrap_iters, p.alpha))
        overall["net_of_yoy_pct"] = overall["adjusted"]["effect_pct"] - yoy["effect_pct"]

    return {"strata": strata_eff, "overall": overall, "yoy": yoy}


# ---------------------------------------------------------------- 构成变化

def composition_check(users_rows: list[dict], windows: dict, p: AnalysisParams) -> dict:
    """用 users 指标比较 pre/post 各层份额，识别样本构成变化。"""
    pre_set = {d.isoformat() for d in windows["pre"]}
    post_set = {d.isoformat() for d in windows["post"]}
    pre_sum: dict[str, float] = {}
    post_sum: dict[str, float] = {}
    for r in users_rows:
        if r["segment"] != p.treatment_segment:
            continue
        if r["date"] in pre_set:
            pre_sum[r["stratum"]] = pre_sum.get(r["stratum"], 0.0) + r["value"]
        elif r["date"] in post_set:
            post_sum[r["stratum"]] = post_sum.get(r["stratum"], 0.0) + r["value"]
    tp = sum(pre_sum.values()) or 1.0
    tq = sum(post_sum.values()) or 1.0
    rows = []
    chi2 = 0.0
    for s in sorted(set(pre_sum) | set(post_sum)):
        a, b = pre_sum.get(s, 0.0) / tp, post_sum.get(s, 0.0) / tq
        shift = b - a
        if a > 0:
            chi2 += (b - a) ** 2 / a
        rows.append({"stratum": s, "pre_share": a, "post_share": b, "shift": shift,
                     "flag": abs(shift) > p.share_shift_threshold})
    return {"strata": rows, "chi2_stat": chi2,
            "overall_flag": any(r["flag"] for r in rows),
            "threshold": p.share_shift_threshold}


# ---------------------------------------------------------------- 事件影响

def event_impacts(series: dict, windows: dict, events: list[dict], p: AnalysisParams) -> list[dict]:
    """估计事件窗口内处理组的超常量。

    按层计算相对基线的超常、按 pre 期固定权重聚合（与主效应估计同口径），
    避免缺失/剔除造成的层覆盖不全污染池化基线。
    series 必须是未剔除事件日的序列（event_policy=include 制备）。
    """
    post_set = {d.isoformat() for d in windows["post"]}
    pre_set = {d.isoformat() for d in windows["pre"]}
    event_day_set: set[str] = set()
    for ev in events:
        for d in daterange(DATE.fromisoformat(ev["start"]), DATE.fromisoformat(ev["end"])):
            event_day_set.add(d.isoformat())
    strata = sorted({k[0] for k in series})
    pre_sums = {}
    for s in strata:
        vals = series.get((s, p.treatment_segment), {})
        pre_sums[s] = sum(vals.get(d.isoformat(), 0.0) for d in windows["pre"])
    total = sum(pre_sums.values()) or 1.0
    weights = {s: v / total for s, v in pre_sums.items()}

    def baseline_for(vals: dict[str, float], ds: str) -> float | None:
        d = DATE.fromisoformat(ds)
        cands = []
        for k in range(1, p.matching_window_weeks + 1):
            for cand in (d - dt.timedelta(days=7 * k), d + dt.timedelta(days=7 * k)):
                cs = cand.isoformat()
                if cs in pre_set and cs in vals and cs not in event_day_set:
                    cands.append(vals[cs])
        return mean(cands) if cands else None

    out = []
    for ev in events:
        days = [d.isoformat() for d in daterange(DATE.fromisoformat(ev["start"]), DATE.fromisoformat(ev["end"]))]
        in_pre = [d for d in days if d in pre_set]
        in_post = [d for d in days if d in post_set]
        rels = []
        excess_abs = base_sum = 0.0
        for ds in in_pre + in_post:
            num = den = 0.0
            for s in strata:
                vals = series.get((s, p.treatment_segment), {})
                if ds not in vals:
                    continue
                base = baseline_for(vals, ds)
                if base:
                    num += weights[s] * (vals[ds] / base - 1)
                    den += weights[s]
                    excess_abs += vals[ds] - base
                    base_sum += base
            if den:
                rels.append(num / den)
        out.append({"event_id": ev["event_id"], "name": ev["name"], "type": ev["type"],
                    "start": ev["start"], "end": ev["end"],
                    "days_in_pre_window": len(in_pre),
                    "days_in_post_window": len(in_post),
                    "excess_abs": excess_abs if rels else None,
                    "excess_pct": mean(rels) if rels else None,
                    "note": ev.get("note", "")})
    return out


# ---------------------------------------------------------------- 敏感性

def run_sensitivity(rows: list[dict], p: AnalysisParams, windows: dict,
                    events: list[dict], registry: list[dict]) -> dict:
    """对关键参数做网格扫描，输出效应估计的分布范围。"""
    grid = {
        "matching_window_weeks": sorted({2, 3, 4, 5, p.matching_window_weeks}),
        "missing_policy": ["drop", "interpolate"],
        "event_policy": ["exclude", "include"],
        "backfill_policy": ["exclude", "include"],
        "caliber_policy": ["adjust", "flag_only"],
    }
    variants = []
    keys = list(grid)
    for combo in itertools.product(*(grid[k] for k in keys)):
        vp = p.model_copy(update=dict(zip(keys, combo)))
        crows, _ = apply_caliber(rows, vp, registry)
        series, _ = prepare_series(crows, vp, windows, events)
        eff = estimate_effects(series, windows, vp, boot=False)
        variants.append({
            "params": dict(zip(keys, combo)),
            "effect_pct": eff["overall"]["adjusted"]["effect_pct"],
            "did_pct": eff["overall"]["did"]["effect_pct"],
        })
    effs = [v["effect_pct"] for v in variants]
    dids = [v["did_pct"] for v in variants]
    return {
        "variants": variants,
        "n_variants": len(variants),
        "effect_range": [min(effs), max(effs)],
        "did_range": [min(dids), max(dids)],
    }


# ---------------------------------------------------------------- 主流程

def run_analysis(conn, p: AnalysisParams) -> dict:
    from . import storage  # 延迟导入避免环

    ev = Evidence()
    ds = storage.get_dataset(conn, p.data_version)
    if not ds:
        raise ValueError(f"数据版本不存在: {p.data_version}")
    rows = storage.load_rows(conn, p.data_version, p.metric)
    users_rows = storage.load_rows(conn, p.data_version, "users")
    events = storage.list_events(conn)
    registry = storage.list_registry(conn)
    windows = build_windows(p)
    data_range = storage.data_date_range(conn, p.data_version)

    ev.log("load_dataset", {"data_version": ds["version"], "content_hash": ds["content_hash"],
                            "row_count": ds["row_count"], "metric_rows": len(rows),
                            "metric": p.metric})

    # 口径调整（基准参数）
    crows, caliber_info = apply_caliber(rows, p, registry)
    ev.log("caliber_adjustment", {k: v for k, v in caliber_info.items() if k != "definitions"})

    # 样本流 + 序列准备
    flow: list[dict] = []
    series, prep_info = prepare_series(crows, p, windows, events, flow=flow)
    n_cells = sum(len(v) for v in series.values())
    flow.append({"stage": "analysis_sample", "label": "分析样本（层×段×日）",
                 "in": n_cells, "out": n_cells, "dropped": 0, "note": "进入匹配估计"})
    ev.log("prepare_series", {"backfill_policy": p.backfill_policy,
                              "missing_policy": p.missing_policy,
                              "interpolated": prep_info["interpolated"],
                              "missing_cells": len(prep_info["missing_cells"]),
                              "event_policy": p.event_policy,
                              "event_days": len(prep_info["event_dates"]),
                              "sample_cells": n_cells})

    # 匹配对（用于边界验证的泄漏检查）
    probe_vals = next(iter(series.values()), {})
    probe_pairs = build_pairs(probe_vals, windows["pre"], windows["post"], p.matching_window_weeks)
    boundary_checks = validate_boundaries(
        windows, p, data_range,
        matched_pairs={DATE.fromisoformat(d): [DATE.fromisoformat(m) for m in v["matches"]]
                       for d, v in probe_pairs.items()})
    ev.log("validate_boundaries", {"checks": [{"id": c["id"], "passed": c["passed"]} for c in boundary_checks]})

    # 回补验证（基于口径调整前原始行；一致性按 日期×层×段 单元粒度）
    final_cells = set()
    for (s, seg), vals in series.items():
        for d in vals:
            final_cells.add((d, s, seg))
    backfill_checks, backfill_outliers = validate_backfill(
        rows, windows, p.backfill_policy, final_cells=final_cells)
    ev.log("validate_backfill", {"checks": [{"id": c["id"], "passed": c["passed"]} for c in backfill_checks],
                                 "outliers": len(backfill_outliers)})

    # 效应估计
    eff = estimate_effects(series, windows, p, boot=True)
    ev.log("estimate_effects", {
        "adjusted_pct": eff["overall"]["adjusted"]["effect_pct"],
        "naive_pct": eff["overall"]["naive"]["effect_pct"],
        "did_pct": eff["overall"]["did"]["effect_pct"],
        "yoy_pct": eff["yoy"]["effect_pct"] if eff["yoy"] else None,
        "weights": eff["overall"]["weights"],
    })

    # 构成变化
    comp = composition_check(users_rows, windows, p)
    ev.log("composition_check", {"overall_flag": comp["overall_flag"], "chi2": round(comp["chi2_stat"], 5),
                                 "shifts": {r["stratum"]: round(r["shift"], 4) for r in comp["strata"]}})

    # 事件影响（用未剔除事件日的序列估计，与事件处理策略解耦）
    series_inc, _ = prepare_series(crows, p.model_copy(update={"event_policy": "include"}), windows, events)
    ev_impacts = event_impacts(series_inc, windows, events, p)
    ev.log("event_impacts", {"events": [{"name": e["name"], "excess_pct": e["excess_pct"]} for e in ev_impacts]})

    # 敏感性
    sens = run_sensitivity(rows, p, windows, events, registry)
    ev.log("sensitivity", {"n_variants": sens["n_variants"],
                           "effect_range": sens["effect_range"], "did_range": sens["did_range"]})

    # 影响范围汇总
    adj = eff["overall"]["adjusted"]
    point = adj["effect_pct"]
    ci = adj.get("ci_pct", [None, None])
    smin, smax = sens["effect_range"]
    if ci[0] is not None and ci[0] > 0 and smin > 0:
        verdict = "正向且对参数稳健"
    elif ci[0] is not None and ci[0] > 0:
        verdict = "正向，但对参数敏感（敏感性区间跨零）"
    elif ci[1] is not None and ci[1] < 0 and smax < 0:
        verdict = "负向且对参数稳健"
    elif ci[1] is not None and ci[1] < 0:
        verdict = "负向，但对参数敏感"
    else:
        verdict = "统计上不显著"
    impact_range = {
        "point_pct": point,
        "ci_pct": ci,
        "did_pct": eff["overall"]["did"]["effect_pct"],
        "did_ci_pct": eff["overall"]["did"].get("ci_pct"),
        "net_of_yoy_pct": eff["overall"].get("net_of_yoy_pct"),
        "sensitivity_pct": [smin, smax],
        "verdict": verdict,
    }

    # 日级序列（页面趋势图）
    daily = []
    pre_set = {d.isoformat() for d in windows["pre"]}
    post_set = {d.isoformat() for d in windows["post"]}
    pooled: dict[str, dict[str, float]] = {}
    for (s, seg), vals in series.items():
        for d, v in vals.items():
            pooled.setdefault(d, {})[seg] = pooled.setdefault(d, {}).get(seg, 0.0) + v
    bf_dates = {r["date"] for r in rows if r["is_backfilled"]}
    missing_dates = {m["date"] for m in prep_info["missing_cells"]}
    for d in sorted(pre_set | post_set):
        daily.append({
            "date": d,
            "period": "pre" if d in pre_set else "post",
            "treatment": pooled.get(d, {}).get(p.treatment_segment),
            "control": pooled.get(d, {}).get(p.control_segment),
            "is_event": d in prep_info["event_dates"],
            "event_name": prep_info["event_dates"].get(d),
            "is_backfilled": d in bf_dates,
            "is_missing": d in missing_dates,
        })

    result = {
        "params": p.model_dump(mode="json"),
        "param_hash": p.param_hash(),
        "data_version": ds,
        "code_version": code_version(),
        "windows": {k: [str(min(v)), str(max(v))] for k, v in windows.items()},
        "validations": {"time_boundaries": boundary_checks, "backfill": backfill_checks},
        "backfill_outliers": backfill_outliers,
        "sample_flow": flow,
        "caliber": caliber_info,
        "composition": comp,
        "missing": {"cells": prep_info["missing_cells"], "interpolated": prep_info["interpolated"],
                    "policy": p.missing_policy},
        "events": ev_impacts,
        "effects": {
            "overall": {k: v for k, v in eff["overall"].items() if k != "daily_rel"},
            "strata": [
                {"stratum": s,
                 "treatment": {k2: v2 for k2, v2 in e[p.treatment_segment].items()
                               if k2 not in ("daily_rel", "daily_abs", "pairs")},
                 "control": {k2: v2 for k2, v2 in e[p.control_segment].items()
                             if k2 not in ("daily_rel", "daily_abs", "pairs")},
                 "did": {k2: v2 for k2, v2 in e["did"].items() if k2 != "daily_rel"}}
                for s, e in eff["strata"].items()
            ],
            "yoy": {k: v for k, v in eff["yoy"].items() if k != "daily_rel"} if eff["yoy"] else None,
        },
        "sensitivity": sens,
        "impact_range": impact_range,
        "daily_series": daily,
        "evidence": ev.steps,
    }
    result["result_hash"] = short_hash(result)
    return result
