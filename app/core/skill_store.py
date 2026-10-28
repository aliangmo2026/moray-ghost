"""SQLite 技能库（data/ghost.db，与 macros 同库）：v0.15 阶段三「偷学」技能存储。

技能 = 一次人类操作录制（点击坐标/输入文本/滚动）+ 点击现场截图 + VLM 语义
标注（target_text）。技能仅存本机 data/，绝不上传。

SQL 红线：与 macro_store 一致——所有含外部输入的查询一律参数绑定（占位符 ?），
SQL 全部为内联静态字面量，禁止拼接/format/f-string 组装 SQL。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from typing import Any, Dict, List, Optional

from .config import DB_PATH, ensure_dirs

_WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  description TEXT DEFAULT '',
  source TEXT DEFAULT 'learned',
  annotation_backend TEXT DEFAULT 'none',
  confidence REAL DEFAULT 0.0,
  steps_total INTEGER DEFAULT 0,
  actions_json TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT
);
"""


def init_db() -> None:
    ensure_dirs()
    with _WRITE_LOCK, closing(_connect()) as con, con:
        con.executescript(SCHEMA)  # 静态 schema，不含外部输入


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class SkillNameConflictError(Exception):
    """技能名已存在（录制 stop 时换名重试，绝不静默覆盖）。"""


def save_skill(name: str, description: str, actions: List[Dict[str, Any]],
               confidence: float, annotation_backend: str,
               source: str = "learned") -> Dict[str, Any]:
    """落库一个技能；name 冲突抛 SkillNameConflictError。"""
    skill_id = "skl_" + uuid.uuid4().hex[:12]
    actions_json = json.dumps(actions, ensure_ascii=False)
    ts = _now()
    with _WRITE_LOCK, closing(_connect()) as con, con:
        try:
            con.execute(
                "INSERT INTO skills (id, name, description, source, "
                "annotation_backend, confidence, steps_total, actions_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (skill_id, str(name), str(description or ""), str(source),
                 str(annotation_backend), float(confidence), len(actions),
                 actions_json, ts, ts))
        except sqlite3.IntegrityError as exc:
            raise SkillNameConflictError(
                f"skill name already exists: {name!r}") from exc
    return get_skill(skill_id)  # type: ignore[return-value]


def list_skills() -> List[Dict[str, Any]]:
    with closing(_connect()) as con:
        rows = con.execute(
            "SELECT id, name, description, source, annotation_backend, "
            "confidence, steps_total, created_at, updated_at "
            "FROM skills ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_skill(skill_id: str) -> Optional[Dict[str, Any]]:
    with closing(_connect()) as con:
        row = con.execute("SELECT * FROM skills WHERE id = ?",
                          (str(skill_id),)).fetchone()
    return _expand(row) if row else None


def get_skill_by_name(name: str) -> Optional[Dict[str, Any]]:
    with closing(_connect()) as con:
        row = con.execute("SELECT * FROM skills WHERE name = ?",
                          (str(name),)).fetchone()
    return _expand(row) if row else None


def _expand(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["actions"] = json.loads(d.pop("actions_json") or "[]")
    except (TypeError, ValueError):
        d["actions"] = []
    return d


def delete_skill(skill_id: str) -> bool:
    with _WRITE_LOCK, closing(_connect()) as con, con:
        cur = con.execute("DELETE FROM skills WHERE id = ?", (str(skill_id),))
        return cur.rowcount > 0
