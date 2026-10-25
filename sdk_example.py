"""sdk_example.py —— 用官方 SDK 以"零门槛"方式驱动 moray-ghost。

演示链路（全程不侵入用户桌面，符合测试纪律）：
  1. health / environment 体检
  2. 抓屏并落盘（调用方自己写文件——SDK 本身不碰文件系统）
  3. 单步动作：move 到光标当前位置（零位移，走完整安全管道）
  4. task.run()：建任务 → 入队 → 自动轮询 → done（含队列排位观察）
  5. degraded 挂起处置：require_vision → TaskDegradedError → resume → done
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from sdk import GhostClient, TaskDegradedError  # noqa: E402


def hr(title: str) -> None:
    print("\n=== " + title + " ===", flush=True)


def main() -> int:
    client = GhostClient(agent_id="sdk-demo")   # token 自动从 GHOST_TOKEN / .env 解析

    hr("1. health / environment")
    h = client.health.check()
    print("service:", h["service"], "v" + h["version"], "| monitors:", len(h["monitors"]),
          "| cursor:", h["cursor"])
    env = client.environment.check()
    print("desktop_readable:", env["desktop_readable"],
          "| input_allowed:", env["input_allowed"])

    hr("2. screenshot（SDK 返回 image_bytes，落盘由调用方决定）")
    shot = client.action.screenshot(scale=0.4, quality=60)
    out = ROOT / "data" / "sdk_demo_screenshot.jpg"
    out.write_bytes(shot["image_bytes"])
    print("saved:", out, "(", len(shot["image_bytes"]), "bytes,",
          shot["width"], "x", shot["height"], ")")

    hr("3. 单步动作：move 到光标当前位置（零位移，完整管道）")
    x, y = h["cursor"]
    r = client.action.move(x, y, settle_seconds=0.1)
    print("move ok, cursor_after:", r["cursor_after"],
          "| audit:", Path(r["after_screenshot_path"]).name)

    hr("4. task.run()：入队 + 自动轮询")
    t0 = time.time()
    view = client.task.run(
        steps=[{"kind": "wait", "ms": 400},
               {"kind": "wait", "ms": 400}],
        goal="SDK demo: two waits")
    print("status:", view["status"], "| steps:",
          [s["status"] for s in view["step_states"]],
          "| wall %.1fs" % (time.time() - t0))
    me = client.task.list()
    print("my tasks:", me["count"], "| agent:", me["agent_id"])

    hr("5. degraded 挂起处置（require_vision → resume）")
    try:
        client.task.run(steps=[{"kind": "require_vision", "prompt": "确认弹窗"}],
                        goal="SDK demo: vision gate")
        print("unexpected: no degraded")
        return 1
    except TaskDegradedError as exc:
        tid = exc.task["task_id"]
        print("degraded at step", exc.task["fail"]["step_id"],
              "(", exc.task["fail"]["fail_class"], ")")
        print("…主仓/Agent 完成视觉处置后 resume 重排队…")
        client.task.resume(tid)
        done = client.task.wait(tid)
        print("resumed →", done["status"])

    print("\nSDK demo: ALL OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
