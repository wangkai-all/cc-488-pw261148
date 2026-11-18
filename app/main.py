"""FastAPI 入口：对照分析 API + 静态页面。"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import storage
from .pipeline import run_analysis
from .schemas import AnalysisParams, canonical_json
from .seed import seed

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="业务改版前后对照分析", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    conn = storage.connect()
    try:
        seed(conn)
    finally:
        conn.close()


def _conn():
    return storage.connect()


@app.get("/api/health")
def health():
    return {"status": "ok", "time": dt.datetime.now().isoformat()}


@app.get("/api/datasets")
def datasets():
    conn = _conn()
    try:
        return {"datasets": storage.list_datasets(conn)}
    finally:
        conn.close()


@app.get("/api/events")
def events():
    conn = _conn()
    try:
        return {"events": storage.list_events(conn)}
    finally:
        conn.close()


@app.get("/api/metrics/registry")
def registry():
    conn = _conn()
    try:
        return {"registry": storage.list_registry(conn)}
    finally:
        conn.close()


@app.get("/api/runs")
def runs():
    conn = _conn()
    try:
        return {"runs": storage.list_runs(conn)}
    finally:
        conn.close()


@app.post("/api/runs")
def create_run(params: AnalysisParams):
    conn = _conn()
    try:
        result = run_analysis(conn, params)
        run_id = "run-" + uuid.uuid4().hex[:10]
        created_at = dt.datetime.now().isoformat(timespec="seconds")
        storage.insert_run(
            conn, run_id, created_at, params.name,
            canonical_json(params.model_dump(mode="json")),
            params.param_hash(), params.data_version,
            result["code_version"], result["result_hash"], "success",
            json.dumps(result, ensure_ascii=False),
        )
        result["run_id"] = run_id
        result["created_at"] = created_at
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    conn = _conn()
    try:
        meta = storage.get_run(conn, run_id)
        if not meta:
            raise HTTPException(status_code=404, detail="运行不存在")
        result = storage.get_run_result(conn, run_id)
        result["run_id"] = run_id
        result["created_at"] = meta["created_at"]
        return result
    finally:
        conn.close()


@app.post("/api/seed")
def reseed(force: bool = False):
    conn = _conn()
    try:
        versions = seed(conn, force=force)
        return {"versions": versions}
    finally:
        conn.close()


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
