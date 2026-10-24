"""宏重放骨架（v0.2 占位，v0.3 落地）。

v0.3 落地要求（备忘）：
- 每一步必须走 app.core.controller.execute 的同一安全管道（全局串行锁、
  前后截图审计、Fail-Safe 409 时中止整个宏）。
- dry_run=True 时只校验步骤与锚点，不执行任何输入。
- allow_fallback：锚点失效时是否允许图像匹配兜底。
- 全程写 macro_runs（run_id / steps_ok / fail_step_index / fail_reason / 现场截图）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class NotImplementedYet(NotImplementedError):
    """v0.2 占位：路由/schema/鉴权已就位，重放逻辑未实现。"""


def replay(macro_id: str, dry_run: bool = False, allow_fallback: bool = False,
           task_id: Optional[str] = None) -> Dict[str, Any]:
    raise NotImplementedYet("macro replay is scaffolded for v0.3")


def heal(macro_id: str, steps_json: Optional[str] = None,
         note: Optional[str] = None) -> Dict[str, Any]:
    raise NotImplementedYet("macro heal is scaffolded for v0.3")
