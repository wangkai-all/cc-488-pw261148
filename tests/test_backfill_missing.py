"""回补数据验证与缺失期处理测试。"""
import datetime as dt

from app.pipeline import build_windows, prepare_series
from app.schemas import AnalysisParams
from app.validate import validate_backfill


def make_params(**kw):
    base = dict(data_version="v2026.11.01", revamp_date=dt.date(2026, 9, 1),
                pre_days=28, post_days=28)
    base.update(kw)
    return AnalysisParams(**base)


def _row(date, value, backfilled=0, backfilled_at=None, stratum="T1", segment="treatment"):
    return {"date": date, "stratum": stratum, "segment": segment, "metric": "gmv",
            "value": value, "caliber": "v1", "is_backfilled": backfilled,
            "backfilled_at": backfilled_at}


def test_backfill_timestamp_validation():
    p = make_params()
    w = build_windows(p)
    rows = [_row("2026-08-10", 100, 1, "2026-08-12"),          # 正常
            _row("2026-08-11", 100, 1, "2026-08-10")]          # backfilled_at 早于业务日期
    checks, _ = validate_backfill(rows, w, "exclude")
    by_id = {c["id"]: c for c in checks}
    assert not by_id["backfill_timestamp"]["passed"]


def test_backfill_outlier_detection():
    p = make_params()
    w = build_windows(p)
    rows = [_row(f"2026-08-{d:02d}", 100.0) for d in range(1, 14)]
    rows.append(_row("2026-08-14", 100.0, 1, "2026-08-18"))    # 正常回补
    rows.append(_row("2026-08-15", 3000.0, 1, "2026-08-18"))   # 离群回补
    checks, outliers = validate_backfill(rows, w, "exclude")
    by_id = {c["id"]: c for c in checks}
    assert not by_id["backfill_outlier"]["passed"]
    assert len(outliers) == 1 and outliers[0]["date"] == "2026-08-15"


def test_backfill_policy_consistency_exclude():
    p = make_params()
    w = build_windows(p)
    rows = [_row("2026-08-10", 100.0, 1, "2026-08-12")]
    cell = ("2026-08-10", "T1", "treatment")
    # 最终样本不含该回补单元 → 一致
    checks, _ = validate_backfill(rows, w, "exclude", final_cells={("2026-08-11", "T1", "treatment")})
    by_id = {c["id"]: c for c in checks}
    assert by_id["backfill_policy_consistency"]["passed"]
    # 最终样本含该回补单元 → 不一致
    checks, _ = validate_backfill(rows, w, "exclude", final_cells={cell})
    by_id = {c["id"]: c for c in checks}
    assert not by_id["backfill_policy_consistency"]["passed"]


def test_missing_policy_drop_vs_interpolate():
    p = make_params(missing_policy="drop", yoy_control=False)
    w = build_windows(p)
    rows = []
    for d in w["pre"] + w["post"]:
        if d != dt.date(2026, 8, 20):
            rows.append(_row(d.isoformat(), 100.0))
    series, info = prepare_series(rows, p, w, events=[])
    vals = series[("T1", "treatment")]
    assert "2026-08-20" not in vals
    assert len(info["missing_cells"]) == 1

    p2 = make_params(missing_policy="interpolate", yoy_control=False)
    series2, info2 = prepare_series(rows, p2, w, events=[])
    vals2 = series2[("T1", "treatment")]
    assert vals2.get("2026-08-20") == 100.0  # 同星期邻居均值插补
    assert info2["interpolated"] == 1


def test_event_exclusion_removes_event_days():
    p = make_params(event_policy="exclude", yoy_control=False)
    w = build_windows(p)
    rows = [_row(d.isoformat(), 100.0) for d in w["pre"] + w["post"]]
    events = [{"event_id": "E1", "name": "大促", "type": "promotion",
               "start": "2026-09-10", "end": "2026-09-12", "scope": "{}", "note": ""}]
    series, info = prepare_series(rows, p, w, events=events)
    vals = series[("T1", "treatment")]
    for d in ("2026-09-10", "2026-09-11", "2026-09-12"):
        assert d not in vals
    assert len(info["event_dates"]) == 3
