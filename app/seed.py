"""演示数据种子：生成两个数据版本的日级指标数据。

植入场景：
- 2026-09-01 改版，处理组分层真实提升（T1 +8% / T2 +6% / T3 +3%），对照组无效应；
- 构成变化：改版后处理组 T3 用户 +30%（低客单层占比上升）；
- 口径变化：gmv 2026-09-15 起 v1→v2（含运费，×1.10），09-08..09-14 双报重叠期；
- 异常事件：99大促（2026-09-10..12，处理组 ×1.25）、支付网关故障（2026-08-20，全量 ×0.5）；
- 缺失期：2026-08-15 T2/处理组、2026-09-25 T1/对照组 全指标缺失；
- 回补数据（仅 v2026.11.01）：2026-08-14 T2/处理组正常回补；2026-09-20 T3/处理组 gmv 异常回补（×3 离群）；
- 去年同期（2025-09）处理组 +2% 季节性自然增长，供同期对照剔除。
"""
from __future__ import annotations

import datetime as dt
import random

from . import storage
from .schemas import short_hash

STRATA = ["T1", "T2", "T3"]
SEGMENTS = ["treatment", "control"]

BASE_GMV = {("T1", "treatment"): 1_000_000, ("T2", "treatment"): 600_000, ("T3", "treatment"): 300_000,
            ("T1", "control"): 820_000, ("T2", "control"): 500_000, ("T3", "control"): 260_000}
BASE_USERS = {("T1", "treatment"): 48_000, ("T2", "treatment"): 39_000, ("T3", "treatment"): 21_000,
              ("T1", "control"): 40_000, ("T2", "control"): 33_000, ("T3", "control"): 18_000}
AOV = {"T1": 95.0, "T2": 80.0, "T3": 68.0}
WEEKDAY_FACTOR = [0.97, 0.99, 1.00, 1.01, 1.03, 1.09, 1.07]

REVAMP_DATE = dt.date(2026, 9, 1)
GMV_EFFECT = {"T1": 1.08, "T2": 1.06, "T3": 1.03}
USERS_EFFECT = {"T1": 1.01, "T2": 1.02, "T3": 1.30}

START = dt.date(2025, 8, 1)
END = dt.date(2026, 10, 15)

EVENTS = [
    {"event_id": "EV-001", "name": "99大促", "type": "promotion",
     "start": "2026-09-10", "end": "2026-09-12",
     "scope": {"segments": ["treatment"]}, "note": "平台大促，处理组 GMV 约 +25%"},
    {"event_id": "EV-002", "name": "支付网关故障", "type": "outage",
     "start": "2026-08-20", "end": "2026-08-20",
     "scope": {"segments": ["treatment", "control"]}, "note": "支付通道中断 6 小时"},
]

REGISTRY = [
    {"metric": "gmv", "caliber": "v1", "effective_from": "2025-01-01", "effective_to": "2026-09-14",
     "definition": "成交口径 v1：支付金额，不含运费"},
    {"metric": "gmv", "caliber": "v2", "effective_from": "2026-09-15", "effective_to": None,
     "definition": "成交口径 v2：支付金额，含运费（约 +10%）"},
    {"metric": "orders", "caliber": "v1", "effective_from": "2025-01-01", "effective_to": None,
     "definition": "下单口径：支付成功订单数"},
    {"metric": "users", "caliber": "v1", "effective_from": "2025-01-01", "effective_to": None,
     "definition": "日活口径：当日有行为的去重用户"},
]

CALIBER_SWITCH = dt.date(2026, 9, 15)
CALIBER_OVERLAP = (dt.date(2026, 9, 8), dt.date(2026, 9, 14))
CALIBER_FACTOR = 1.10

MISSING_BOTH = [  # 两个版本都缺失
    ("2026-08-15", "T2", "treatment"),
    ("2026-09-25", "T1", "control"),
]
BACKFILL_ROWS = [  # 仅新版本存在，带回补标记；(date, stratum, segment, metric, value_mult, backfilled_at)
    ("2026-08-14", "T2", "treatment", "gmv", 1.0, "2026-08-18"),
    ("2026-08-14", "T2", "treatment", "users", 1.0, "2026-08-18"),
    ("2026-08-14", "T2", "treatment", "orders", 1.0, "2026-08-18"),
    ("2026-09-20", "T3", "treatment", "gmv", 3.0, "2026-09-22"),  # 异常回补：离群
]


def _gen_base_rows() -> dict[tuple, dict]:
    """生成全量基础行：key=(date,stratum,segment,metric,caliber)。"""
    rng = random.Random(20261119)
    rows: dict[tuple, dict] = {}
    d = START
    while d <= END:
        wd = WEEKDAY_FACTOR[d.weekday()]
        trend = 1.0004 ** (d - START).days
        for s in STRATA:
            for seg in SEGMENTS:
                gmv_mult = users_mult = 1.0
                if seg == "treatment":
                    if d >= REVAMP_DATE:
                        gmv_mult *= GMV_EFFECT[s]
                        users_mult *= USERS_EFFECT[s]
                    if dt.date(2025, 9, 2) <= d <= dt.date(2025, 9, 29):
                        gmv_mult *= 1.02  # 去年同期季节性
                if dt.date(2026, 9, 10) <= d <= dt.date(2026, 9, 12) and seg == "treatment":
                    gmv_mult *= 1.25
                    users_mult *= 1.15
                if d == dt.date(2026, 8, 20):
                    gmv_mult *= 0.5
                    users_mult *= 0.8

                gmv_v1 = (BASE_GMV[(s, seg)] * wd * trend * gmv_mult
                          * rng.gauss(1, 0.015))
                users = BASE_USERS[(s, seg)] * wd * trend * users_mult * rng.gauss(1, 0.02)
                orders = gmv_v1 / AOV[s] * rng.gauss(1, 0.01)

                ds = d.isoformat()
                rows[(ds, s, seg, "users", "v1")] = _row(ds, s, seg, "users", users, "v1")
                rows[(ds, s, seg, "orders", "v1")] = _row(ds, s, seg, "orders", orders, "v1")
                # gmv 口径：切换日前 v1，切换日起 v2；重叠期双报
                if d < CALIBER_SWITCH:
                    rows[(ds, s, seg, "gmv", "v1")] = _row(ds, s, seg, "gmv", gmv_v1, "v1")
                if d >= CALIBER_OVERLAP[0]:
                    rows[(ds, s, seg, "gmv", "v2")] = _row(ds, s, seg, "gmv", gmv_v1 * CALIBER_FACTOR, "v2")
        d += dt.timedelta(days=1)
    return rows


def _row(ds, s, seg, metric, value, caliber, backfilled=0, backfilled_at=None):
    return {"date": ds, "stratum": s, "segment": seg, "metric": metric,
            "value": round(value, 2), "caliber": caliber,
            "is_backfilled": backfilled, "backfilled_at": backfilled_at}


def _apply_scenarios(rows: dict, version: str) -> list[dict]:
    """按版本应用缺失与回补场景。"""
    out = dict(rows)
    # 两版本共同缺失
    for ds, s, seg in MISSING_BOTH:
        for metric, caliber in (("gmv", "v1"), ("gmv", "v2"), ("users", "v1"), ("orders", "v1")):
            out.pop((ds, s, seg, metric, caliber), None)
    if version == "v2026.10.15":
        # 旧版本：回补日数据尚未到达
        for ds, s, seg, metric, _mult, _at in BACKFILL_ROWS:
            for caliber in ("v1", "v2"):
                out.pop((ds, s, seg, metric, caliber), None)
    else:
        # 新版本：写入回补行（覆盖同 key 原始行）
        for ds, s, seg, metric, mult, at in BACKFILL_ROWS:
            for caliber in ("v1", "v2"):
                key = (ds, s, seg, metric, caliber)
                if key in out:
                    r = dict(out[key])
                    r["value"] = round(r["value"] * mult, 2)
                    r["is_backfilled"] = 1
                    r["backfilled_at"] = at
                    out[key] = r
    return list(out.values())


def seed(conn, force: bool = False) -> list[str]:
    """生成两个数据版本并写入库。返回已存在的版本列表（force=True 时重建）。"""
    storage.init_db(conn)
    existing = {r["version"] for r in storage.list_datasets(conn)}
    versions = {
        "v2026.10.15": "首版装载：回补数据未到达",
        "v2026.11.01": "回补后版本：含 08-14 正常回补与 09-20 异常回补(gmv×3)",
    }
    if not force and set(versions) <= existing:
        return sorted(existing)

    base = _gen_base_rows()
    conn.execute("DELETE FROM metric_daily")
    conn.execute("DELETE FROM datasets")
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM metric_registry")

    for version, note in versions.items():
        rows = _apply_scenarios(base, version)
        conn.executemany(
            "INSERT INTO metric_daily VALUES (?,?,?,?,?,?,?,?,?)",
            [(version, r["date"], r["stratum"], r["segment"], r["metric"], r["value"],
              r["caliber"], r["is_backfilled"], r["backfilled_at"]) for r in rows],
        )
        content_hash = short_hash(sorted(
            (r["date"], r["stratum"], r["segment"], r["metric"], r["value"],
             r["caliber"], r["is_backfilled"]) for r in rows))
        conn.execute(
            "INSERT INTO datasets VALUES (?,?,?,?,?,?)",
            (version, f"业务日指标快照 {version}", f"{version.replace('v', '')}T02:00:00",
             len(rows), content_hash, note),
        )

    conn.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
        [(e["event_id"], e["name"], e["type"], e["start"], e["end"],
          str(e["scope"]).replace("'", '"'), e["note"]) for e in EVENTS],
    )
    conn.executemany(
        "INSERT INTO metric_registry VALUES (?,?,?,?,?)",
        [(r["metric"], r["caliber"], r["effective_from"], r["effective_to"], r["definition"])
         for r in REGISTRY],
    )
    conn.commit()
    return sorted(versions)
