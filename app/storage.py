"""SQLite 持久化：数据集版本、指标明细、事件、口径注册表、运行记录与结果。"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("ANALYSIS_DB", "/workspace/data/analysis.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    version      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    row_count    INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    note         TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS metric_daily (
    dversion      TEXT NOT NULL,
    date          TEXT NOT NULL,
    stratum       TEXT NOT NULL,
    segment       TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         REAL NOT NULL,
    caliber       TEXT NOT NULL,
    is_backfilled INTEGER NOT NULL DEFAULT 0,
    backfilled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_metric_daily ON metric_daily(dversion, metric, date);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    type     TEXT NOT NULL,
    start    TEXT NOT NULL,
    end      TEXT NOT NULL,
    scope    TEXT NOT NULL DEFAULT '{}',
    note     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS metric_registry (
    metric         TEXT NOT NULL,
    caliber        TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    definition     TEXT NOT NULL,
    PRIMARY KEY (metric, caliber)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    name         TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    param_hash   TEXT NOT NULL,
    data_version  TEXT NOT NULL,
    code_version  TEXT NOT NULL,
    result_hash  TEXT NOT NULL,
    status       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_results (
    run_id      TEXT PRIMARY KEY,
    result_json TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------- 数据集版本 ----------

def list_datasets(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM datasets ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_dataset(conn, version: str) -> dict | None:
    r = conn.execute("SELECT * FROM datasets WHERE version=?", (version,)).fetchone()
    return dict(r) if r else None


def load_rows(conn, version: str, metric: str) -> list[dict]:
    rows = conn.execute(
        "SELECT date, stratum, segment, metric, value, caliber, is_backfilled, backfilled_at "
        "FROM metric_daily WHERE dversion=? AND metric=? ORDER BY date",
        (version, metric),
    ).fetchall()
    return [dict(r) for r in rows]


def data_date_range(conn, version: str) -> tuple[str, str] | None:
    r = conn.execute(
        "SELECT MIN(date) a, MAX(date) b FROM metric_daily WHERE dversion=?", (version,)
    ).fetchone()
    return (r["a"], r["b"]) if r and r["a"] else None


# ---------- 事件 / 口径注册表 ----------

def list_events(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY start").fetchall()]


def list_registry(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM metric_registry ORDER BY metric, effective_from").fetchall()]


# ---------- 运行记录 ----------

def insert_run(conn, run_id: str, created_at: str, name: str, params_json: str,
               param_hash: str, data_version: str, code_version: str,
               result_hash: str, status: str, result_json: str) -> None:
    conn.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, created_at, name, params_json, param_hash, data_version,
         code_version, result_hash, status),
    )
    conn.execute("INSERT INTO run_results VALUES (?,?)", (run_id, result_json))
    conn.commit()


def list_runs(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT run_id, created_at, name, param_hash, data_version, code_version, result_hash, status "
        "FROM runs ORDER BY created_at DESC"
    ).fetchall()]


def get_run(conn, run_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(r) if r else None


def get_run_result(conn, run_id: str) -> dict | None:
    r = conn.execute("SELECT result_json FROM run_results WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(r["result_json"]) if r else None
