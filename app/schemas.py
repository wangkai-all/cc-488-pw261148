"""分析参数与运行记录的 schema 定义。参数快照随运行持久化，保证可追溯。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field


def canonical_json(obj) -> str:
    """稳定序列化：用于参数哈希、证据哈希。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def short_hash(obj, n: int = 12) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:n]


class AnalysisParams(BaseModel):
    """一次对照分析的完整参数快照。"""

    name: str = "改版前后对照分析"
    data_version: str = Field(..., description="数据集版本，必须存在于版本注册表")
    metric: str = "gmv"
    revamp_date: dt.date = Field(..., description="改版上线日（post 窗口起点）")
    pre_days: int = Field(28, gt=0, le=120)
    post_days: int = Field(28, gt=0, le=120)
    matching_window_weeks: int = Field(
        4, ge=1, le=8, description="匹配窗口：每个 post 日匹配前 N 个同星期 pre 日"
    )
    treatment_segment: str = "treatment"
    control_segment: str = "control"
    missing_policy: Literal["drop", "interpolate"] = "drop"
    event_policy: Literal["exclude", "include"] = "exclude"
    backfill_policy: Literal["exclude", "include"] = "exclude"
    caliber_policy: Literal["adjust", "flag_only"] = "adjust"
    yoy_control: bool = Field(True, description="是否启用去年同期对照")
    bootstrap_iters: int = Field(1000, ge=100, le=10000)
    alpha: float = Field(0.05, gt=0, lt=0.5)
    share_shift_threshold: float = Field(0.02, gt=0, lt=0.5, description="构成变化告警阈值(份额)")

    def param_hash(self) -> str:
        return short_hash(self.model_dump(mode="json"))


class RunRecord(BaseModel):
    run_id: str
    created_at: str
    name: str
    param_hash: str
    data_version: str
    code_version: str
    result_hash: str
    status: str
