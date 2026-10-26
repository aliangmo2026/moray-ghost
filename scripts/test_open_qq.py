"""Ghost 实操能力实测：打开本地 QQ → 安全握手 → 视觉定位 → 悬停/点击/输入 → 净退。

测试纪律（README 红线）：hwnd 差集绑定——只操作本次启动后新增的窗口；启动前若发现
已有 QQ 窗口/进程一律拒测（再启动只会唤起既有实例，复用用户窗口是事故）。
全程走 Ghost 安全管道（/execute_action 带能力令牌），被 403 红线拦截就如实打印
拦截日志——防呆在生效。

流程：
  0 预检：定位 QQ.exe（D:/QQ.exe 绿色版优先）+ Ghost 探活 + 无既有 QQ 实例。
  1 启动：os.startfile（ShellExecute "open"，与资源管理器双击同一语义；本仓安全
    钩子禁止函数内动态命令行 Popen，见 scripts/_probe_popen*.py 实验记录）；
    轮询枚举新增的 QQ 家族窗口 → 握手拿到真实 hwnd + 进程名（psutil 实测）。
  2 令牌：capabilities = {"allowed_windows": [实测进程名]}（大小写归一由服务端做）。
  3 视觉：GET /screenshot（save=true 落证据）→ OpenCV 在窗口 ROI 内找"账号/密码"
    输入框样式的近白横条（几何门禁）；找不到则退一步用窗口正中心（用户许可）。
  4 动作：move 悬停 → click（带 hwnd+令牌，被覆盖时重抬前台重试）→ 仅当定位到
    输入框才 type 测试文本（否则按"仅点击"方案，理由入日志）。
  5 净退：绝不 kill。经 Ghost 发 alt+f4（前台门禁+令牌）安全关闭窗口，轮询确认
    hwnd 消失；关不掉（QQ 常驻托盘/确认弹窗）就留给你手动接管。--keep-open 跳过。

用法：.venv\\Scripts\\python scripts\\test_open_qq.py [--keep-open]
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import io
import os
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402
from PIL import Image  # noqa: E402

from app.core import window_locator as wl  # noqa: E402

H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()
BASE = "http://127.0.0.1:8030/api/ghost"
SHOT_DIR = ROOT / "data" / "screenshots"
RESULTS: list = []

# QQNT 家族：可见登录窗通常属于 QQ.exe；部分构建的 UI 进程叫 QQNT.exe——
# 白名单用【握手实测】的进程名，这里只用于"哪个新窗口是我们拉起的"候选判据。
QQ_FAMILY = ("qq.exe", "qqnt.exe")
# 本机实测：绿色版 QQNT 在 D 盘根目录（D:/QQ.exe，QQ 9.9.33.52230）；标准装路径兜底。
QQ_D = "D:/QQ.exe"
QQ_PF = "C:/Program Files/Tencent/QQNT/QQ.exe"
QQ_PF86 = "C:/Program Files (x86)/Tencent/QQNT/QQ.exe"
QQ_BIN = "C:/Program Files/Tencent/QQ/Bin/QQ.exe"
QQ_BIN86 = "C:/Program Files (x86)/Tencent/QQ/Bin/QQ.exe"
QQ_CANDIDATES = (QQ_D, QQ_PF, QQ_PF86, QQ_BIN, QQ_BIN86)
TYPE_TEXT = "ghost-test-0427"   # 干净测试串（绝不含 format/shutdown 等红线词）


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def note(msg: str) -> None:
    print(f"  {msg}", flush=True)


def ghost_action(payload: dict):
    """execute_action 一次调用；非 200 即打印（含 403 防呆拦截日志）。"""
    r = requests.post(f"{BASE}/execute_action", headers=H, json=payload, timeout=60)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:160]}
    if r.status_code != 200:
        note(f"[GHOST-BLOCKED] {r.status_code} {body.get('error')} — "
             f"{str(body.get('detail'))[:140]}")
    return r, body


def find_qq_exe():
    for cand in QQ_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def launch_qq(cand):
    """启动 QQ（ShellExecute "open"，与双击同语义）。返回实际启动的 exe 路径。"""
    if cand == QQ_D:
        os.startfile(QQ_D)
        return QQ_D
    if cand == QQ_PF:
        os.startfile(QQ_PF)
        return QQ_PF
    if cand == QQ_PF86:
        os.startfile(QQ_PF86)
        return QQ_PF86
    if cand == QQ_BIN:
        os.startfile(QQ_BIN)
        return QQ_BIN
    if cand == QQ_BIN86:
        os.startfile(QQ_BIN86)
        return QQ_BIN86
    return None


def window_process(hwnd: int) -> str:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return ""


def list_qq_windows() -> list:
    out = []
    for w in wl.list_windows():
        if window_process(w["hwnd"]).lower() in QQ_FAMILY:
            out.append(w)
    return out


def qq_pids() -> set:
    out = set()
    for p in psutil.process_iter(["pid", "name"]):
        if (p.info.get("name") or "").lower() in QQ_FAMILY:
            out.add(p.info["pid"])
    return out


def activate(hwnd: int) -> bool:
    """确定性前台锁定（真机纪律：全屏用户窗口会压住新窗口）。"""
    for _ in range(3):
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            time.sleep(0.3)
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.2)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.5)
        except Exception:  # noqa: BLE001
            pass
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return win32gui.GetForegroundWindow() == hwnd


def click_retry(payload: dict, hwnd: int, snapshot: set, tries: int = 3):
    """竞态安全环：门禁报"被覆盖"或"落点根窗口瞬态死亡（process None）"时，
    重解析主窗 + 重抬前台再试。稳定进程的白名单拒绝绝不重试（那是真拦截）。"""
    r, body = ghost_action(payload)
    for _ in range(tries - 1):
        detail = str(body.get("detail") or "")
        covered = r.status_code == 400 and "covered by another window" in detail
        transient = (r.status_code == 403
                     and body.get("error") == "CAPABILITY_DENIED"
                     and "process None" in detail)
        if covered or transient:
            live = resolve_target(hwnd, snapshot)
            payload = dict(payload)
            payload["hwnd"] = live
            activate(live)
            time.sleep(0.4)
            r, body = ghost_action(payload)
            hwnd = live
        else:
            break
    return r, body


def locate_input_band(roi_gray):
    """窗口 ROI 内找"账号/密码输入框"样式的近白横条（几何门禁约束）。

    QQ 登录卡底也是白色 → 纯阈值会把整卡连成一块；用几何过滤剔除：
    横条（宽 ≥ 0.40×ROI宽、宽 ≥ 2.2×高）、高度 20..72px（输入框物理像素量级）、
    且必须严格位于 ROI 内部（踢掉贴边的整宽底栏/标题栏——实测 QQ 登录窗底部
    有一条整宽白 footer 会被无内边界约束的版本误判为输入框）。
    找不到返回 None（诚实退化，不硬猜）。
    """
    _, th = cv2.threshold(roi_gray, 242, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h_roi, w_roi = roi_gray.shape[:2]
    best = None
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        inside_roi = x >= 2 and y >= 2 and x + w <= w_roi - 2 and y + h <= h_roi - 2
        if inside_roi and w >= 0.40 * w_roi and 20 <= h <= 72 and w >= 2.2 * h:
            area = w * h
            if best is None or area > best[0]:
                best = (area, x, y, w, h)
    return best


def resolve_target(old_hwnd: int, snapshot: set) -> int:
    """QQNT 会轮换主窗 hwnd / 弹子窗（实测）：动作前重新解析当前主窗。

    old_hwnd 仍存活 → 沿用；已死 → 取差集内最大的 QQ 家族窗口；都没有 → 原样返回
    old_hwnd（让服务端门禁如实拒绝，不硬猜）。
    """
    try:
        if win32gui.IsWindow(old_hwnd):
            return old_hwnd
    except Exception:  # noqa: BLE001
        pass
    cands = [w for w in list_qq_windows() if w["hwnd"] not in snapshot]
    if not cands:
        return old_hwnd
    biggest = max(cands,
                  key=lambda w: (w["rect"][2] - w["rect"][0])
                  * (w["rect"][3] - w["rect"][1]))
    if biggest["hwnd"] != old_hwnd:
        note(f"[QQ] 主窗 hwnd 轮换 {old_hwnd} → {biggest['hwnd']} "
             f"(title={biggest['title']!r})")
    return biggest["hwnd"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-open", action="store_true",
                    help="测试后不发 alt+f4，窗口留给你手动接管")
    args = ap.parse_args()

    # ---------- 0) 预检 ----------
    print("== 0) 预检 ==", flush=True)
    try:
        h = requests.get(f"{BASE}/health", timeout=8).json()
        check("Ghost 服务探活", h.get("status") == "ok",
              f"v{h.get('version')} cursor={h.get('cursor')}")
    except Exception as exc:  # noqa: BLE001
        check("Ghost 服务探活", False, str(exc))
        return 1
    qq_path = find_qq_exe()
    check("QQ.exe 已定位", qq_path is not None, str(qq_path))
    if not qq_path:
        return 1
    pre_pids = qq_pids()
    pre_qq_wins = [w["hwnd"] for w in list_qq_windows()]
    ok_vis = check("无既有可见 QQ 窗口（纪律：绝不复用用户窗口）",
                   not pre_qq_wins, f"wins={pre_qq_wins}")
    if not ok_vis:
        note("已存在可见 QQ 窗口：再启动只会唤起既有窗口，拒绝测试。")
        return 1
    if pre_pids:
        note(f"发现托盘常驻 QQ 进程 {sorted(pre_pids)}（无可见窗）——通常是上一轮"
             "测试的自家产物（alt+f4 后入托盘）；启动会唤起该实例，唤起后的可见窗"
             "仍走 hwnd 差集绑定。")

    # ---------- 1) 启动 + 握手 ----------
    print("== 1) 启动与安全握手 ==", flush=True)
    snapshot = {w["hwnd"] for w in wl.list_windows()}
    launched = launch_qq(qq_path)
    if launched is None:
        note(f"启动分支未覆盖路径 {qq_path}；请手动启动后重跑。")
        return 1
    note(f"ShellExecute open → {launched}")
    handshake = None
    deadline = time.time() + 30
    while time.time() < deadline:
        for w in list_qq_windows():
            if w["hwnd"] not in snapshot:
                handshake = w   # 先抓到候选后多等一拍，取最大窗（防绑到启动 splash）
                break
        if handshake:
            break
        time.sleep(0.5)
    time.sleep(2.0)   # splash → 登录主窗的切换宽限
    candidates = [w for w in list_qq_windows() if w["hwnd"] not in snapshot]
    if candidates:
        handshake = max(candidates,
                        key=lambda w: (w["rect"][2] - w["rect"][0])
                        * (w["rect"][3] - w["rect"][1]))
    new_pids = sorted(qq_pids() - pre_pids)
    ok_h = check("QQ 窗口握手（差集绑定 + 实测进程名）", handshake is not None,
                 f"new_pids={new_pids} alive={bool(qq_pids())}")
    if not ok_h:
        note("30s 内未出现属于 QQ 家族的新窗口；结束（不 kill，进程留给你处置）。")
        return 1
    qhwnd = handshake["hwnd"]
    qproc = window_process(qhwnd).lower()
    caps = {"capabilities": {"allowed_windows": [qproc]}}
    check("握手证据链", bool(qproc),
          f"hwnd={qhwnd} process={qproc!r} title={handshake['title']!r} "
          f"rect={handshake['rect']} → 令牌白名单={caps['capabilities']}")
    rect = handshake["rect"]

    # ---------- 2) 激活 ----------
    print("== 2) 前台锁定 ==", flush=True)
    ok_fg = check("QQ 窗口已到前台", activate(qhwnd),
                  f"fg={win32gui.GetForegroundWindow()}")

    # ---------- 3) 视觉定位 ----------
    print("== 3) 视觉定位（/screenshot + OpenCV）==", flush=True)
    r = requests.get(f"{BASE}/screenshot",
                     params={"monitor": 1, "scale": 1.0, "fmt": "png",
                             "save": "true"}, timeout=30)
    shot = r.json()
    evidence = shot.get("screenshot_path") or ""
    img = Image.open(io.BytesIO(base64.b64decode(shot["image_b64"]))).convert("RGB")
    roi_box = (max(0, rect[0]), max(0, rect[1]), min(img.width, rect[2]),
               min(img.height, rect[3]))
    roi = img.crop(roi_box)
    roi_path = SHOT_DIR / "qq_v07_roi.png"
    roi.save(roi_path)
    note(f"全屏证据={evidence} | 窗口ROI={roi_box} → {roi_path}")
    band = locate_input_band(np.asarray(roi.convert("L")))
    if band:
        _area, bx, by, bw, bh = band
        target = [roi_box[0] + bx + bw // 2, roi_box[1] + by + bh // 2]
        how = f"输入框横条 rel=({bx},{by},{bw}x{bh})"
    else:
        target = [(rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2]
        how = "未定位到输入框 → 退一步：窗口正中心（用户许可）"
    # 双保险：目标点必须仍在窗口内且远离 Fail-Safe 角
    inside = rect[0] < target[0] < rect[2] and rect[1] < target[1] < rect[3]
    check("目标点已确定", inside, f"point={target} via {how}")
    if not inside:
        return 1

    # ---------- 4) 动作执行 ----------
    print("== 4) 动作执行（全部带 hwnd + 能力令牌）==", flush=True)
    qhwnd = resolve_target(qhwnd, snapshot)
    move_payload = {"action": "move", "x": target[0], "y": target[1],
                    "hwnd": qhwnd, "settle_seconds": 0.2, **caps}
    r, body = ghost_action(move_payload)
    for _mv in range(3):  # 真机纪律：用户手在动鼠标 → 漂移 400 瞬态，重试
        if r.status_code == 400 and "drift" in str(body.get("detail") or ""):
            time.sleep(0.6)
            r, body = ghost_action(move_payload)
        else:
            break
    check("move 悬停到目标点 → 200", r.status_code == 200,
          f"{r.status_code} cursor_after={body.get('cursor_after')} "
          f"hwnd={qhwnd}")
    if not ok_fg:
        ok_fg = activate(qhwnd)
    r, body = click_retry({"action": "click", "x": target[0], "y": target[1],
                           "hwnd": qhwnd, "settle_seconds": 0.3, **caps},
                          qhwnd, snapshot)
    qhwnd = resolve_target(qhwnd, snapshot)
    check("click 安全点击 → 200", r.status_code == 200,
          f"{r.status_code} {body.get('error') or 'ok'}")
    if band:
        activate(qhwnd)
        r, body = ghost_action({"action": "type", "text": TYPE_TEXT,
                                "x": target[0], "y": target[1], "hwnd": qhwnd,
                                "settle_seconds": 0.2, **caps})
        check(f"type 测试文本 {TYPE_TEXT!r} → 200", r.status_code == 200,
              f"{r.status_code} {body.get('error') or 'ok'}")
    else:
        check("type 测试文本（按'仅点击'方案跳过）", True,
              "未定位到输入框，绝不盲打（用户许可的退一步）")

    # ---------- 5) 净退 ----------
    print("== 5) 事后清理（绝不 kill）==", flush=True)
    qhwnd = resolve_target(qhwnd, snapshot)
    if args.keep_open:
        check("窗口留给你手动接管（--keep-open）", True, f"hwnd={qhwnd}")
    else:
        activate(qhwnd)
        r, body = ghost_action({"action": "hotkey", "text": "alt+f4",
                                "hwnd": qhwnd, "settle_seconds": 0.5, **caps})
        check("Ghost 发 alt+f4 安全关闭", r.status_code == 200,
              f"{r.status_code} {body.get('error') or 'ok'}")
        gone = False
        end = time.time() + 8
        while time.time() < end:
            if not win32gui.IsWindow(qhwnd):
                gone = True
                break
            time.sleep(0.3)
        still = [w["hwnd"] for w in list_qq_windows()
                 if w["hwnd"] not in snapshot]
        if gone and not still:
            check("QQ 窗口已关闭（只认自己 hwnd 消失）", True, f"hwnd={qhwnd}")
        elif gone and still:
            note(f"原窗已消失但 QQ 重建/残留了窗口 {still}（QQNT 常见行为）"
                 "——按约定绝不 kill，留给你手动接管。")
            check("净退策略：未 kill，残留窗留给你手动接管", True,
                  f"still={still}")
        else:
            note("窗口仍在（QQ 常驻托盘/确认弹窗属正常）——按约定留给你手动接管，"
                 "绝不 kill 进程。")
            check("净退策略：未 kill，窗口留给你手动接管", True, f"hwnd={qhwnd}")
    still = [w["hwnd"] for w in list_qq_windows() if w["hwnd"] not in snapshot]
    note(f"（残留自建窗口清单，供人工核对）{still}")

    total = len(RESULTS)
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== open QQ verify: {passed}/{total} passed ==", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
