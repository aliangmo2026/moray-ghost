"""v0.7.1 生产环境输入源过滤探针（test_v071_security 专用辅助脚本）。

在**进程内**直接调用 controller.run_action（绕过 HTTP 的旁路），断言：
- GHOST_ENV=production → 403 SOURCE_DENIED（被输入源门禁拦截）；
- GHOST_ENV=dev        → 400 SAFETY_BLOCKED（通过输入源门禁，倒在参数校验）；
两种情况都必须**零鼠标位移**（拦截发生在任何物理动作之前）。

输出单行结果 `RESULT <status> <error>`，exit 0 = 符合预期。
"""
from __future__ import annotations

import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        import ctypes
        import ctypes.wintypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import controller  # noqa: E402
from app.core import config  # noqa: E402


def cursor() -> tuple:
    import ctypes
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def main() -> int:
    before = cursor()
    status, error = 0, ""
    try:
        # 非法动作名：生产模式应在输入源门禁就被拒（403），
        # 开发模式会通过输入源门禁、倒在参数校验（400），两种都零副作用。
        controller.run_action({"action": "definitely_not_an_action"})
        status, error = 200, "NO_ERROR"
    except controller.ActionError as exc:
        status, error = exc.status, exc.error
    after = cursor()
    moved = (before[0] - after[0], before[1] - after[1])
    expect_status = 403 if config.IS_PRODUCTION else 400
    expect_error = "SOURCE_DENIED" if config.IS_PRODUCTION else "SAFETY_BLOCKED"
    ok = (status == expect_status and error == expect_error
          and moved == (0, 0))
    print("RESULT", status, error, "moved=" + str(moved),
          "env=" + config.ENV_MODE, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
