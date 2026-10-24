"""SQLite 宏库（data/ghost.db）：schema 初始化 + 基础读写 + Intent Token nonce 防重放。

SQL 红线：所有含外部输入的查询一律参数绑定（占位符 ?），SQL 全部为内联静态字面量，
禁止拼接/format/f-string 组装 SQL。
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import DB_PATH, ensure_dirs

_WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS macros (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  app_signature TEXT,
  screen_signature TEXT,
  version INTEGER DEFAULT 1,
  success_count INTEGER DEFAULT 0,
  fail_count INTEGER DEFAULT 0,
  last_used TEXT,
  created_at TEXT,
  updated_at TEXT,
  steps_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS macro_runs (
  run_id TEXT PRIMARY KEY,
  macro_id TEXT,
  task_id TEXT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  steps_total INTEGER,
  steps_ok INTEGER,
  fail_step_index INTEGER,
  fail_reason TEXT,
  fail_screenshot_path TEXT
);
CREATE TABLE IF NOT EXISTS used_nonces (
  nonce TEXT PRIMARY KEY,
  exp INTEGER
);
CREATE INDEX IF NOT EXISTS idx_used_nonces_exp ON used_nonces(exp);
"""


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    ensure_dirs()
    with _WRITE_LOCK, closing(_connect()) as con, con:
        con.executescript(SCHEMA)  # 静态 schema，不含外部输入


def list_macros() -> List[Dict[str, Any]]:
    with closing(_connect()) as con:
        rows = con.execute(
            "SELECT id, name, app_signature, screen_signature, version, "
            "success_count, fail_count, last_used, created_at, updated_at "
            "FROM macros ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_macro(macro_id: str) -> Optional[Dict[str, Any]]:
    with closing(_connect()) as con:
        row = con.execute("SELECT * FROM macros WHERE id = ?", (macro_id,)).fetchone()
    return dict(row) if row else None


class NonceReplayError(Exception):
    pass


class MacroNameConflictError(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def create_macro(name: str, app_signature: Optional[str],
                 steps_json: str) -> str:
    macro_id = "m_" + uuid.uuid4().hex[:12]
    now = _now()
    try:
        with _WRITE_LOCK, closing(_connect()) as con, con:
            con.execute(
                "INSERT INTO macros (id, name, app_signature, screen_signature, "
                "version, success_count, fail_count, last_used, created_at, "
                "updated_at, steps_json) VALUES (?, ?, ?, NULL, 1, 0, 0, NULL, ?, ?, ?)",
                (macro_id, name, app_signature, now, now, steps_json))
    except sqlite3.IntegrityError:
        raise MacroNameConflictError("macro name already exists: " + str(name))
    return macro_id


def create_run(run_id: str, macro_id: str, task_id: Optional[str],
               started_at: str, steps_total: int) -> None:
    with _WRITE_LOCK, closing(_connect()) as con, con:
        con.execute(
            "INSERT INTO macro_runs (run_id, macro_id, task_id, started_at, "
            "finished_at, status, steps_total, steps_ok, fail_step_index, "
            "fail_reason, fail_screenshot_path) "
            "VALUES (?, ?, ?, ?, NULL, 'running', ?, NULL, NULL, NULL, NULL)",
            (run_id, macro_id, task_id, started_at, int(steps_total)))


def finish_run(run_id: str, status: str, finished_at: str, steps_ok: int,
               fail_step_index: Optional[int], fail_reason: Optional[str],
               fail_screenshot_path: Optional[str]) -> None:
    with _WRITE_LOCK, closing(_connect()) as con, con:
        con.execute(
            "UPDATE macro_runs SET finished_at = ?, status = ?, steps_ok = ?, "
            "fail_step_index = ?, fail_reason = ?, fail_screenshot_path = ? "
            "WHERE run_id = ?",
            (finished_at, status, int(steps_ok), fail_step_index, fail_reason,
             fail_screenshot_path, run_id))


def bump_stats(macro_id: str, success: bool) -> None:
    now = _now()
    with _WRITE_LOCK, closing(_connect()) as con, con:
        if success:
            con.execute(
                "UPDATE macros SET success_count = success_count + 1, "
                "last_used = ?, updated_at = ? WHERE id = ?", (now, now, macro_id))
        else:
            con.execute(
                "UPDATE macros SET fail_count = fail_count + 1, "
                "last_used = ?, updated_at = ? WHERE id = ?", (now, now, macro_id))


def consume_nonce(nonce: str, exp: int) -> None:
    """插入 nonce 做防重放；重复 nonce 抛 NonceReplayError。顺带清理已过期项。"""
    with _WRITE_LOCK, closing(_connect()) as con, con:
        con.execute("DELETE FROM used_nonces WHERE exp < ?", (int(time.time()),))
        try:
            con.execute("INSERT INTO used_nonces (nonce, exp) VALUES (?, ?)",
                        (str(nonce), int(exp)))
        except sqlite3.IntegrityError:
            raise NonceReplayError("nonce already used: " + str(nonce))
