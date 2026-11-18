"""时间边界验证测试。"""
import datetime as dt

import pytest

from app.pipeline import build_pairs, build_windows
from app.schemas import AnalysisParams
from app.validate import validate_boundaries


def make_params(**kw):
    base = dict(data_version="v2026.11.01", revamp_date=dt.date(2026, 9, 1),
                pre_days=28, post_days=28, matching_window_weeks=4)
    base.update(kw)
    return AnalysisParams(**base)


def test_windows_shape_and_yoy_alignment():
    p = make_params()
    w = build_windows(p)
    assert len(w["pre"]) == 28 and len(w["post"]) == 28
    assert max(w["pre"]) == dt.date(2026, 8, 31)
    assert min(w["post"]) == dt.date(2026, 9, 1)
    # 去年同期窗口：364 天偏移保证星期对齐
    for a, b in zip(w["pre"], w["yoy_pre"]):
        assert (a - b).days == 364
        assert a.weekday() == b.weekday()


def test_matching_pairs_never_cross_revamp():
    """匹配窗口内的 pre 日期必须全部早于上线日（无后视泄漏）。"""
    p = make_params()
    w = build_windows(p)
    # 构造完整序列：窗口内每天都有值
    vals = {d.isoformat(): 1.0 for d in w["pre"] + w["post"]}
    pairs = build_pairs(vals, w["pre"], w["post"], p.matching_window_weeks)
    assert pairs, "应当存在匹配对"
    for ds, pair in pairs.items():
        for m in pair["matches"]:
            assert dt.date.fromisoformat(m) < p.revamp_date, f"{ds} 匹配到了 {m}"
    # 每个 post 日至少有一个匹配
    assert len(pairs) == 28


def test_matching_window_weeks_limits_lookback():
    p = make_params(matching_window_weeks=2)
    w = build_windows(p)
    vals = {d.isoformat(): 1.0 for d in w["pre"] + w["post"]}
    pairs = build_pairs(vals, w["pre"], w["post"], 2)
    for ds, pair in pairs.items():
        d = dt.date.fromisoformat(ds)
        for m in pair["matches"]:
            delta = (d - dt.date.fromisoformat(m)).days
            assert delta in (7, 14), f"匹配窗口外日期: {m}"


def test_boundary_validation_passes_on_good_windows():
    p = make_params()
    w = build_windows(p)
    checks = validate_boundaries(w, p, ("2025-08-01", "2026-10-15"))
    by_id = {c["id"]: c for c in checks}
    assert by_id["pre_post_no_overlap"]["passed"]
    assert by_id["pre_window_shape"]["passed"]
    assert by_id["post_window_shape"]["passed"]
    assert by_id["yoy_alignment"]["passed"]
    assert by_id["data_coverage"]["passed"]


def test_boundary_validation_detects_coverage_gap():
    p = make_params()
    w = build_windows(p)
    checks = validate_boundaries(w, p, ("2026-01-01", "2026-10-15"))
    by_id = {c["id"]: c for c in checks}
    assert not by_id["data_coverage"]["passed"]


def test_boundary_validation_detects_leakage():
    p = make_params()
    w = build_windows(p)
    bad_pairs = {dt.date(2026, 9, 10): [dt.date(2026, 9, 3)]}  # 匹配到 post 期
    checks = validate_boundaries(w, p, ("2025-08-01", "2026-10-15"), matched_pairs=bad_pairs)
    by_id = {c["id"]: c for c in checks}
    assert not by_id["matching_no_leakage"]["passed"]
