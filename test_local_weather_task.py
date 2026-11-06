#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_local_weather_task.py — 纯本地天气模拟任务 E2E（临时主仓视角，降维版）。

任务链（全本地、零云端、零 URL）：
自建记事本 A 输入「假设明天的天气是 26℃」→ 视觉提取「26」→
自建记事本 B 输入「26 + 10 = 36」→ 最终全屏截图（两窗同框）取证。

安全红线（全程遵守，无一例外）：
- 一切桌面输入（click/type）经 Ghost SDK → 服务端能力令牌 / 窗口黑名单 /
  Ghost Guard / 频率熔断 / 结构化审计 100% 生效；本脚本零裸 pyautogui 输入。
- 能力令牌显式白名单 allowed_windows = {notepad.exe}——输入只允许打进记事本，
  越权直接 403。
- 记事本用**裸启动**自启（实测：file 参数会并入既有窗口当标签页、/N 是
  无效参数只会弹「文件名无效」，唯裸启动稳定开全新顶层窗）+ **句柄差集**
  绑定（裸启动复用既有进程，pid 不变），绝不触碰用户已有窗口。
- **输入前空缓冲断言**：本机记事本「会话恢复」会把历史未保存标签（含用户
  个人内容）复活进每个新窗口——打字前必须确认活动标签是空的，否则先点
  「+」新建空白标签；新标签仍非空则中止，绝不覆盖任何疑似用户内容。
- 记事本怪癖：注入 Enter/Ctrl 在 Win11 记事本不可靠 → 全程单行输入、
  不用快捷键；跨行内容一律不涉及。
- 锁屏 / UAC / 冻结：开跑前 environment + health 体检，不满足立即中止；
  光标在 Fail-Safe 四角也中止（防 409 连环）。
- agent 不持有审批令牌：命中 409 PENDING_APPROVAL 只如实报告，绝不自批。
- 零 URL 零网络：内置数据，天然不触发任何云端安全拦截。

用法：.venv\\Scripts\\python.exe test_local_weather_task.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)  # SDK 的 .env 解析基于 cwd
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import ctypes  # noqa: E402
import psutil  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402


def _enable_dpi_awareness() -> None:
    """本进程 Per-Monitor V2 DPI 感知（150% 缩放坐标系必须物理像素）。"""
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


_enable_dpi_awareness()

from sdk import (  # noqa: E402
    GhostApprovalRequiredError,
    GhostClient,
    GhostError,
    GhostFrozenError,
    SystemNotReadableError,
)

# ---------------------------------------------------------------- 常量
WEATHER_TEXT = "假设明天的天气是 26℃"   # 内置数据（任务规定文案）
ALLOWED_WINDOWS = ["notepad.exe"]        # 能力令牌：显式进程白名单
NOTEPAD_NAMES = {"notepad.exe"}
EXPECTED_TEMP = 26.0                     # 内置数据的真值（提取结果应与之一致）
TS = time.strftime("%Y%m%d_%H%M%S")
LOG_PATH = ROOT / "data" / "tmp" / f"local_weather_run_{TS}.log"
FINAL_SHOT = ROOT / "data" / "tmp" / f"local_weather_final_{TS}.png"

TEMP_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*[℃°]", re.IGNORECASE)
# 记事本状态栏/菜单固定文本——空缓冲断言必须忽略它们
STATUS_RE = re.compile(
    r"(第\s*\d+\s*行|行\s*\d+\s*[,，]\s*列|Ln\s*\d+|100\s*%|UTF-?8|CRLF|ANSI"
    r"|^(文件|编辑|查看|记事本|未命名|无标题|文件名无效。)$)")
# 状态栏字符计数——缓冲空的权威信号（0个字符 / 0 characters）
CHARS_RE = re.compile(r"(\d+)\s*个字符|(\d+)\s*characters?", re.IGNORECASE)

# 本轮探测遗留窗口：含用户个人未保存内容（会话恢复复活的标签），
# 只最小化（纯窗口管理，零输入）防最终全屏截图拍到，绝不关闭/绝不丢弃。
PROBE_WINDOWS: List[Tuple[int, int]] = [(984872, 10800), (1115984, 15240)]

# ---------------------------------------------------------------- 日志
def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def step(n: int, title: str) -> None:
    log(f"==== STEP {n}: {title} ====")


# ---------------------------------------------------------------- 窗口发现
def _proc_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except Exception:  # noqa: BLE001
        return ""


def visible_windows() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def _cb(hwnd: int, _acc: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001
            return True
        out.append({"hwnd": hwnd, "pid": pid, "title": title,
                    "rect": win32gui.GetWindowRect(hwnd),
                    "name": _proc_name(pid)})
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def wait_new_notepad(before_pids: set, before_hwnds: set,
                     timeout: float = 20.0) -> Dict[str, Any]:
    """/N 差集绑定：只认自己拉起的全新记事本窗口（新进程 + 新句柄）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in visible_windows():
            if (w["pid"] not in before_pids and w["name"] in NOTEPAD_NAMES):
                log(f"PID 差集绑定窗口: hwnd={w['hwnd']} pid={w['pid']} "
                    f"title={w['title']!r}")
                return w
        for w in visible_windows():  # fallback：新句柄（进程复用兜底）
            if (w["hwnd"] not in before_hwnds
                    and w["name"] in NOTEPAD_NAMES):
                log(f"句柄差集绑定窗口: hwnd={w['hwnd']} pid={w['pid']} "
                    f"title={w['title']!r}")
                return w
        time.sleep(0.4)
    raise SystemExit(f"[FAIL] {timeout}s 内未发现新记事本窗口")


# ---------------------------------------------------------------- 窗口管理
_TOPMOST_RESTORE: List[int] = []


def raise_top(hwnd: int) -> None:
    """纯窗口管理（非输入注入）：临时 HWND_TOPMOST——全屏聊天窗会压住
    目标窗，OCR/点击前必须抬到顶端；结束时统一恢复。
    被用户 Win+D/显示桌面最小化时先 SW_RESTORE（实弹：最小化后点击
    点位归属解析到桌面/任务栏 → SAFETY_BLOCKED 连环）。"""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            log(f"窗口曾被最小化（用户操作实弹），已恢复显示: {hwnd}")
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                              | win32con.SWP_SHOWWINDOW)
        if hwnd not in _TOPMOST_RESTORE:
            _TOPMOST_RESTORE.append(hwnd)
    except Exception as exc:  # noqa: BLE001
        log(f"raise_top 失败(继续): {exc}")


def restore_topmost() -> None:
    for h in list(_TOPMOST_RESTORE):
        try:
            if win32gui.IsWindow(h):
                win32gui.SetWindowPos(h, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                                      | win32con.SWP_SHOWWINDOW)
        except Exception:  # noqa: BLE001
            pass
    if _TOPMOST_RESTORE:
        log(f"已恢复 {len(_TOPMOST_RESTORE)} 个窗口的置顶状态")
        _TOPMOST_RESTORE.clear()


def place_at(hwnd: int, x: int, y: int, w: int, h: int,
             max_right: int = 2540, max_bottom: int = 1540) -> Tuple[int, ...]:
    """挪动+缩放到指定位置，完全屏内才返回（物理像素，越界即中止）。"""
    w2 = min(w, max_right - x)
    h2 = min(h, max_bottom - y)
    if w2 < 360 or h2 < 300 or x < 0 or y < 0:
        raise SystemExit(f"[FAIL] 目标区域非法: ({x},{y},{w2},{h2})")
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, x, y, w2, h2,
                          win32con.SWP_SHOWWINDOW)
    if hwnd not in _TOPMOST_RESTORE:
        _TOPMOST_RESTORE.append(hwnd)
    time.sleep(0.4)
    v = win32gui.GetWindowRect(hwnd)
    if v[0] < -5 or v[1] < 0 or v[2] > max_right + 5 or v[3] > max_bottom + 5:
        raise SystemExit(f"[FAIL] 移动后仍越界: rect={v}")
    return v


def minimize_probe_windows() -> None:
    """最小化本轮探测遗留窗（含用户个人未保存内容）：纯窗口管理，
    缓冲区原样保留，只是别让最终全屏截图拍到个人内容。"""
    for hwnd, pid in PROBE_WINDOWS:
        try:
            if not win32gui.IsWindow(hwnd):
                continue
            _tid, wpid = win32process.GetWindowThreadProcessId(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if wpid != pid or _proc_name(wpid) != "notepad.exe":
                log(f"探测遗留窗句柄已被复用，跳过: hwnd={hwnd} "
                    f"pid={wpid} title={title!r}")
                continue
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            log(f"探测遗留窗已最小化（内容原样保留）: hwnd={hwnd} "
                f"title={title!r}")
        except Exception as exc:  # noqa: BLE001
            log(f"最小化探测遗留窗失败(继续): {exc}")


# ---------------------------------------------------------------- RAG 工具
def elements_of(client: GhostClient, hwnd: int) -> List[Dict[str, Any]]:
    resp = client.vl.elements(hwnd=hwnd, backend="auto", timeout=120.0)
    return list(resp.get("elements") or [])


def activate_and_click(client: GhostClient, hwnd: int,
                       x: int, y: int, tries: int = 5) -> None:
    """置顶 + Ghost 点击文本区（点击天然激活窗口）→ 前台断言，轮询重试。
    type 的前台门禁要求点击后 fg 必须是目标窗（全屏聊天窗抢焦实弹教训）。"""
    last_fg = None
    for i in range(tries):
        raise_top(hwnd)
        time.sleep(0.3 if i == 0 else 0.6)
        try:
            client.action.click(x, y, hwnd=hwnd)
        except GhostError as exc:
            log(f"激活点击失败({exc.status} {exc.error}: {exc.detail})，重试")
            time.sleep(0.8)
            continue
        last_fg = client.environment.check().get("foreground_hwnd")
        if last_fg == hwnd:
            log(f"前台已就绪: fg={last_fg} (第{i + 1}次)")
            return
        time.sleep(0.5)
    raise RuntimeError(f"无法获得前台: fg={last_fg} want={hwnd}")


def _buffer_empty(client: GhostClient, hwnd: int) -> Tuple[bool, List[str]]:
    """缓冲区是否为空。

    权威信号 = 记事本状态栏「0个字符 / 0 characters」（内容区 OCR 会混入
    菜单/状态栏固定词与噪声碎片，直接判文本必误报——上一轮实弹）；
    状态栏读不到时保守判定：滤掉固定词后仍有文本即非空。"""
    els = elements_of(client, hwnd)
    texts = [t for t in (str(e.get("text") or "").strip() for e in els) if t]
    for t in texts:
        m = CHARS_RE.search(t)
        if m:
            return int(m.group(1) or m.group(2) or 0) == 0, texts
    rest = [t for t in texts if not STATUS_RE.search(t)]
    return not rest, texts


def ensure_empty_buffer(client: GhostClient, win: Dict[str, Any]
                        ) -> Tuple[int, int]:
    """保证活动标签是空白缓冲，返回文本区点击点。

    本机记事本「会话恢复」会把历史未保存标签（含用户个人内容）复活进
    每个新窗口——打字前两重确认：
    ① 内容区 OCR 为空（忽略状态栏/菜单固定词）→ 直接可用；
    ② 非空 → 点「+」新建空白标签 → 再断言必须为空，仍非空即中止。
    绝不覆盖任何非空缓冲（用户内容一律不碰）。"""
    hwnd = int(win["hwnd"])
    r = win32gui.GetWindowRect(hwnd)
    text_pt = ((r[0] + r[2]) // 2, r[1] + int((r[3] - r[1]) * 0.4))

    empty, texts = _buffer_empty(client, hwnd)
    if empty:
        log(f"活动标签为空白缓冲 ✓ (OCR: {texts[:5]})")
        return text_pt
    log(f"活动标签非空（疑似会话恢复内容），新建空白标签避开: {texts[:5]}")
    plus_pt = _find_plus_point(client, hwnd, r)
    if plus_pt is None:
        raise SystemExit("[FAIL] 标签条 OCR 找不到「新建标签」按钮")
    log(f"点击「新建标签」: {plus_pt}")
    activate_and_click(client, hwnd, *plus_pt)
    time.sleep(0.8)
    empty2, texts2 = _buffer_empty(client, hwnd)
    if not empty2:
        raise SystemExit(f"[REDLINE] 新标签仍非空，拒绝输入: {texts2[:6]}")
    log("新标签为空白缓冲 ✓")
    return text_pt


def _ocr_crop(client: GhostClient, crop) -> List[Tuple[str, Tuple[int, int]]]:
    """本地 rapidocr 裁剪区 OCR → [(文本, 屏幕坐标中心)]。"""
    import io

    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    shot = client.action.screenshot(monitor=1, scale=1.0, fmt="png")
    img = Image.open(io.BytesIO(shot["image_bytes"]))
    part = img.crop(crop)
    engine = RapidOCR()
    res, _elapse = engine(part)
    out: List[Tuple[str, Tuple[int, int]]] = []
    for entry in (res or []):
        box, text, _score = entry[0], str(entry[1]), entry[2]
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx = int(sum(xs) / 4) + crop[0]
        cy = int(sum(ys) / 4) + crop[1]
        out.append((text, (cx, cy, int(max(xs) - min(xs)))))
    return out


def _find_plus_point(client: GhostClient, hwnd: int,
                     rect: Tuple[int, int, int, int]
                     ) -> Optional[Tuple[int, int]]:
    """「+」新标签按钮 ≈ 最右侧已知标签的右边 35px（glyph 太小 OCR 常漏）。"""
    crop = (rect[0], rect[1], min(rect[2], rect[0] + 1500),
            min(rect[1] + 70, rect[3]))
    try:
        hits = _ocr_crop(client, crop)
    except Exception:  # noqa: BLE001
        return None
    for text, center in hits:
        if text.strip() in ("+", "＋"):
            return int(center[0]), int(center[1])
    if hits:
        rightmost = max(hits, key=lambda h: h[2][2] if len(h[2]) > 2 else h[2][0])
        rx = rightmost[2][0] + (rightmost[2][2] if len(rightmost[2]) > 2 else 60)
        return int(rx + 35), int(rightmost[2][1])
    return None


def focus_textarea(client: GhostClient, win: Dict[str, Any],
                   text_pt: Tuple[int, int], tries: int = 3) -> None:
    """点击文本区拿焦点 + caret，且点击后缓冲必须仍干净。

    实弹：用户正在真机前打字，焦点切换会把他 IME 的在途拼音提交进我方
    缓冲（ziqu 串入）——点击后再断言一次，脏了就「+」新标签重来；
    新标签的 caret 天然在编辑区，无需再点。连续被串扰则中止。"""
    hwnd = int(win["hwnd"])
    for i in range(tries):
        activate_and_click(client, hwnd, *text_pt)
        empty, texts = _buffer_empty(client, hwnd)
        if empty:
            log(f"文本区已聚焦且缓冲干净 ✓ (第{i + 1}次)")
            return
        log(f"点击后缓冲被串入内容: {texts[:4]}，新建空白标签避开")
        r = win32gui.GetWindowRect(hwnd)
        plus_pt = _find_plus_point(client, hwnd, r)
        if plus_pt is None:
            raise SystemExit("[FAIL] 串扰后找不到「新建标签」按钮")
        activate_and_click(client, hwnd, *plus_pt)
        time.sleep(0.8)
        empty2, texts2 = _buffer_empty(client, hwnd)
        if empty2:
            log("新标签干净（caret 已在编辑区）✓")
            return
    raise SystemExit("[REDLINE] 连续被 IME 串扰，无法获得干净缓冲")


# ---------------------------------------------------------------- 主流程
def main() -> int:
    client = GhostClient(agent_id="local-weather-task",
                         allowed_windows=ALLOWED_WINDOWS)
    log(f"SDK 就绪 base={client.base_url} capabilities={client.capabilities}")

    step(0, "安全体检（锁屏/UAC/冻结/Fail-Safe 角落红线）")
    env = client.environment.check()
    health = client.health.check()
    log(f"environment: input_allowed={env.get('input_allowed')} "
        f"lock_screen={env.get('lock_screen')} "
        f"uac={env.get('uac_secure_desktop')} fg={env.get('foreground_hwnd')}")
    if health.get("frozen"):
        raise SystemExit(f"[REDLINE] Ghost 冻结中: {health.get('frozen_reason')}")
    if not env.get("input_allowed"):
        raise SystemExit("[REDLINE] 桌面不可输入（锁屏/UAC），按锁屏红线中止")
    import win32api
    cx, cy = win32api.GetCursorPos()
    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    if (cx, cy) in ((0, 0), (0, sh - 1), (sw - 1, 0), (sw - 1, sh - 1)):
        raise SystemExit(f"[REDLINE] 光标在 Fail-Safe 角落 {cx,cy}，中止")
    minimize_probe_windows()  # 探测遗留窗（含用户个人内容）最小化，防入镜

    # ---- STEP 1: 拉起记事本 A（/N 全新窗口 + PID 差集绑定）----
    step(1, "打开记事本 A（裸启动新窗 + 句柄差集绑定）并输入天气行")
    before_pids = set(psutil.pids())
    before_hwnds = {w["hwnd"] for w in visible_windows()}
    subprocess.Popen(["notepad.exe"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    win_a = wait_new_notepad(before_pids, before_hwnds)
    a_hwnd = int(win_a["hwnd"])
    a_pid = int(win_a["pid"])
    raise_top(a_hwnd)
    place_at(a_hwnd, 60, 80, 1100, 1280)
    log(f"记事本 A 就位: hwnd={a_hwnd} pid={a_pid} rect="
        f"{win32gui.GetWindowRect(a_hwnd)}")

    pt_a = ensure_empty_buffer(client, win_a)
    focus_textarea(client, win_a, pt_a)
    client.action.type(WEATHER_TEXT, x=pt_a[0], y=pt_a[1], hwnd=a_hwnd)
    log(f"已输入天气行: {WEATHER_TEXT!r}")
    time.sleep(0.6)

    # ---- STEP 2: 视觉提取「26」----
    step(2, "视觉提取带 ℃ 的温度数字")
    els_a = elements_of(client, a_hwnd)
    texts_a = [str(e.get("text") or "") for e in els_a]
    log("记事本 A OCR: " + repr(texts_a[:6]))
    hits: List[Tuple[int, float, str]] = []
    for t in texts_a:
        for m in TEMP_RE.finditer(t):
            hits.append((0, float(m.group(1)), t))
    if not hits:
        log("OCR 未命中 ℃，降级本地 VLM 问图…")
        desc = client.vl.describe(
            question=("这是记事本窗口截图。文中有一句含 ℃ 的天气，请找出"
                      "℃ 前面的数字，只输出一个阿拉伯数字。"),
            hwnd=a_hwnd, backend="auto", max_tokens=32, timeout=300.0)
        answer = str(desc.get("description") or "")
        log(f"VLM 回答(raw): {answer!r}")
        m = re.search(r"-?\d+(?:\.\d+)?", answer)
        if not m:
            raise SystemExit("[FAIL] OCR/VLM 均未提取到温度数字")
        temp_val: float = float(m.group(0))
    else:
        temp_val = hits[0][1]
    if float(temp_val).is_integer():
        temp_val = int(temp_val)
    log(f"★ 提取温度 = {temp_val}")
    if abs(float(temp_val) - EXPECTED_TEMP) > 1e-6:
        raise SystemExit(f"[FAIL] 提取值 {temp_val} != 内置真值 {EXPECTED_TEMP}"
                         "（OCR 出错或输入未生效）")

    # ---- STEP 3: 拉起记事本 B，输入算式 ----
    step(3, "打开记事本 B（/N 全新窗口）并输入算式")
    result_val = float(temp_val) + 10.0
    if float(result_val).is_integer():
        result_val = int(result_val)
    calc_line = f"{temp_val} + 10 = {result_val}"
    before_pids2 = set(psutil.pids())
    before_hwnds2 = {w["hwnd"] for w in visible_windows()}
    subprocess.Popen(["notepad.exe"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    win_b = wait_new_notepad(before_pids2, before_hwnds2)
    b_hwnd = int(win_b["hwnd"])
    b_pid = int(win_b["pid"])
    raise_top(b_hwnd)
    r_a = win32gui.GetWindowRect(a_hwnd)
    place_at(b_hwnd, r_a[2] + 30, 80, 900, 1280)
    log(f"记事本 B 就位: hwnd={b_hwnd} pid={b_pid} rect="
        f"{win32gui.GetWindowRect(b_hwnd)}")

    pt_b = ensure_empty_buffer(client, win_b)
    focus_textarea(client, win_b, pt_b)
    client.action.type(calc_line, x=pt_b[0], y=pt_b[1], hwnd=b_hwnd)
    log(f"已输入算式: {calc_line!r}（跨应用焦点切换 A→B ✓）")
    time.sleep(0.6)

    # ---- STEP 4: 最终全屏截图 + 同框校验 ----
    step(4, "最终全屏截图（天气行 + 算式同框校验）")
    final_ok = False
    for round_i in range(1, 4):
        raise_top(a_hwnd)
        raise_top(b_hwnd)
        time.sleep(0.3)
        shot = client.action.screenshot(monitor=1, scale=1.0, fmt="png")
        FINAL_SHOT.write_bytes(shot["image_bytes"])
        els_a2 = [str(e.get("text") or "") for e in elements_of(client, a_hwnd)]
        els_b2 = [str(e.get("text") or "") for e in elements_of(client, b_hwnd)]
        weather_ok = any(re.search(r"\d\s*℃", t) for t in els_a2)
        calc_ok = any(re.search(re.escape(str(result_val)) + r"\s*$", t)
                      or str(result_val) in t for t in els_b2)
        log(f"第{round_i}轮同框校验: 天气行℃={weather_ok} "
            f"算式结果在屏={calc_ok}")
        if weather_ok and calc_ok:
            final_ok = True
            break
    log(f"最终全屏截图: {FINAL_SHOT} 同框校验={'PASS' if final_ok else 'FAIL'}")

    # ---- 汇总 ----
    log("==== 结果汇总 ====")
    log(f"内置天气:   {WEATHER_TEXT}")
    log(f"提取温度:   {temp_val}")
    log(f"计算式:     {temp_val} + 10")
    log(f"计算结果:   {result_val}")
    log(f"最终全屏截图: {FINAL_SHOT}")
    log(f"运行日志: {LOG_PATH}")
    log(f"窗口保持打开供查验: A hwnd={a_hwnd} pid={a_pid} / "
        f"B hwnd={b_hwnd} pid={b_pid}（用户可直接关窗；缓冲未保存，关窗时"
        "记事本会自行询问）")
    return 0 if final_ok else 4


if __name__ == "__main__":
    try:
        code = main()
    except GhostApprovalRequiredError as exc:
        log(f"[409 PENDING_APPROVAL] 命中高危词 hits={exc.hits} —— 动作未执行。"
            f"agent 不持有审批令牌。detail={exc.detail}")
        code = 10
    except GhostFrozenError as exc:
        log(f"[503 FROZEN] Ghost 冻结: {exc.detail}")
        code = 11
    except SystemNotReadableError as exc:
        log(f"[503 SYSTEM_NOT_READABLE] {exc.detail}")
        code = 12
    except GhostError as exc:
        log(f"[GhostError {exc.status}] {exc.error}: {exc.detail} data={exc.data}")
        code = 13
    except SystemExit as exc:
        log(str(exc))
        code = 1 if "REDLINE" in str(exc) else 2
    except Exception as exc:  # noqa: BLE001
        import traceback
        log(f"[UNEXPECTED] {type(exc).__name__}: {exc}\n"
            + traceback.format_exc())
        code = 99
    finally:
        restore_topmost()
    log(f"exit code = {code}")
    sys.exit(code)
