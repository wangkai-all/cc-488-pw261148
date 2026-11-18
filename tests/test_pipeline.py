"""端到端流水线测试：口径调整、效应估计、敏感性、可追溯性。"""
import datetime as dt

import pytest

from app import storage
from app.pipeline import (apply_caliber, build_windows, estimate_effects,
                          prepare_series, run_analysis)
from app.schemas import AnalysisParams
from app.seed import seed


@pytest.fixture(scope="module")
def conn():
    c = storage.connect()
    seed(c, force=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def params():
    return AnalysisParams(data_version="v2026.11.01", revamp_date=dt.date(2026, 9, 1),
                          pre_days=28, post_days=28, name="测试运行")


@pytest.fixture(scope="module")
def result(conn, params):
    return run_analysis(conn, params)


def test_caliber_factor_recovered(conn, params):
    rows = storage.load_rows(conn, "v2026.11.01", "gmv")
    registry = storage.list_registry(conn)
    _, info = apply_caliber(rows, params, registry)
    assert info["changed"] and info["switch_date"] == "2026-09-15"
    assert info["method"] == "overlap_ratio"
    assert info["adjustment_factor"] == pytest.approx(1.10, abs=0.01)
    assert info["rescaled_rows"] > 0


def test_effect_estimate_near_truth(result):
    """处理组真实效应约 +6%（分层混合），口径调整后估计应接近。"""
    adj = result["effects"]["overall"]["adjusted"]["effect_pct"]
    assert adj == pytest.approx(0.06, abs=0.02)
    ci = result["impact_range"]["ci_pct"]
    assert ci[0] < adj < ci[1]
    # 对照组应无显著效应
    ctrl = result["effects"]["overall"]["control_effect_pct"]
    assert abs(ctrl) < 0.02


def test_did_and_yoy(result):
    did = result["impact_range"]["did_pct"]
    assert did == pytest.approx(0.06, abs=0.02)
    # 去年同期 +2% 季节性 → 净效应约 4%
    yoy = result["effects"]["yoy"]["effect_pct"]
    assert yoy == pytest.approx(0.02, abs=0.015)
    net = result["impact_range"]["net_of_yoy_pct"]
    assert net == pytest.approx(0.04, abs=0.025)


def test_composition_shift_detected(result):
    comp = result["composition"]
    assert comp["overall_flag"]
    t3 = next(r for r in comp["strata"] if r["stratum"] == "T3")
    assert t3["shift"] > 0.02  # T3 用户占比显著上升


def test_backfill_outlier_flagged(result):
    outliers = result["backfill_outliers"]
    assert any(o["date"] == "2026-09-20" for o in outliers)
    checks = {c["id"]: c for c in result["validations"]["backfill"]}
    assert not checks["backfill_outlier"]["passed"]  # 验证应告警
    assert checks["backfill_timestamp"]["passed"]


def test_missing_detected(result):
    missing = {(m["date"], m["stratum"]) for m in result["missing"]["cells"]}
    assert ("2026-08-15", "T2") in missing
    assert ("2026-09-25", "T1") in missing


def test_sensitivity_range_contains_base(result):
    sens = result["sensitivity"]
    base = result["impact_range"]["point_pct"]
    lo, hi = sens["effect_range"]
    assert lo <= base <= hi
    assert sens["n_variants"] >= 32
    # flag_only（不调整口径）的变体应显著高估 → 区间上限明显大于基准
    assert hi > base + 0.03


def test_impact_range_not_single_value(result):
    ir = result["impact_range"]
    assert ir["ci_pct"][0] < ir["point_pct"] < ir["ci_pct"][1]
    assert ir["sensitivity_pct"][0] <= ir["point_pct"] <= ir["sensitivity_pct"][1]
    assert ir["verdict"]


def test_evidence_chain_complete(result):
    names = [s["name"] for s in result["evidence"]]
    for expected in ("load_dataset", "caliber_adjustment", "prepare_series",
                     "validate_boundaries", "validate_backfill", "estimate_effects",
                     "composition_check", "event_impacts", "sensitivity"):
        assert expected in names
    assert all(len(s["hash"]) == 12 for s in result["evidence"])


def test_run_reproducibility(conn, params):
    """相同参数 + 相同数据版本 → 相同结果哈希。"""
    r1 = run_analysis(conn, params)
    r2 = run_analysis(conn, params)
    assert r1["result_hash"] == r2["result_hash"]
    assert r1["param_hash"] == r2["param_hash"]


def test_data_version_changes_result(conn, params):
    """换数据版本（无回补）结果哈希应不同 → 版本可追溯。"""
    p_old = params.model_copy(update={"data_version": "v2026.10.15"})
    r_old = run_analysis(conn, p_old)
    r_new = run_analysis(conn, params)
    assert r_old["result_hash"] != r_new["result_hash"]
    # 旧版本没有回补行
    assert not r_old["backfill_outliers"]


def test_sample_flow_monotonic(result):
    flow = result["sample_flow"]
    assert flow[0]["stage"] == "raw"
    assert all(stage["out"] >= 0 for stage in flow)
    stages = [s["stage"] for s in flow]
    assert stages == ["raw", "backfill", "window", "caliber_dedup", "missing", "event", "analysis_sample"]
