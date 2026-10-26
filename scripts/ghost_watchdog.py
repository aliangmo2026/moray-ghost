"""独立看门狗进程（v0.7.2 物理自救）：Ghost 主进程死亡 → 立即释放系统修饰键。

场景：Ghost 主进程在按住 Ctrl/Shift（动作进行中）被任务管理器强杀——进程死了，
但**系统键盘状态里的 key-down 不会随进程消失**，物理键盘从此卡在修饰键按下态。
本进程与 Ghost 完全独立（无共享句柄、无父子牵连），持续监控主进程 PID +
创建时间（防 PID 复用），一旦判定死亡就用 ctypes 直接发 KEYEVENTF_KEYUP，
释放 Ctrl/Shift/Alt/Win 全部左右变体——物理键盘恢复自由，然后自行退出。

用法：python scripts/ghost_watchdog.py --pid <GHOST_PID> [--ctime <创建时间戳>]
由 Ghost lifespan 自动拉起（GHOST_WATCHDOG=false 可禁用）。
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time

# 修饰键 VK 码：通用 + 左右变体全部覆盖（宁可多释放，key-up 对未按下的键无害）。
_MODIFIER_VKS = (
    0x10,        # VK_SHIFT
    0x11,        # VK_CONTROL
    0x12,        # VK_MENU (Alt)
    0x5B, 0x5C,  # VK_LWIN / VK_RWIN
    0xA0, 0xA1,  # VK_LSHIFT / VK_RSHIFT
    0xA2, 0xA3,  # VK_LCONTROL / VK_RCONTROL
    0xA4, 0xA5,  # VK_LMENU / VK_RMENU
)
KEYEVENTF_KEYUP = 0x0002


def release_all_modifiers() -> None:
    """物理释放全部系统修饰键（对未按下的键是无害空操作）。"""
    user32 = ctypes.windll.user32
    for vk in _MODIFIER_VKS:
        try:
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        except Exception:  # noqa: BLE001  自救路径绝不允许自身抛异常
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="moray-ghost watchdog")
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--ctime", type=float, default=0.0,
                    help="主进程创建时间戳（防 PID 复用；0=不校验）")
    ap.add_argument("--interval", type=float, default=0.4)
    args = ap.parse_args()

    try:
        import psutil
    except ImportError:
        print("watchdog: psutil missing", flush=True)
        return 2

    baseline_ctime = args.ctime or 0.0

    def parent_alive() -> bool:
        try:
            p = psutil.Process(args.pid)
            if baseline_ctime and abs(p.create_time() - baseline_ctime) > 1.5:
                return False  # PID 已被复用给别的进程 → 视为原主已死
            return True
        except psutil.NoSuchProcess:
            return False
        except Exception:  # noqa: BLE001  瞬时错误（权限/竞态）按存活处理，宁可多等
            return True

    print("watchdog: watching pid", args.pid, flush=True)
    while parent_alive():
        time.sleep(args.interval)
    # 主进程消亡（正常退出 / 崩溃 / 任务管理器强杀）→ 物理释放修饰键
    release_all_modifiers()
    print("watchdog: target", args.pid, "gone; modifiers released", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
