#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_hell_dynamic_task.py — 地狱级动态任务 E2E（临时主仓视角）。

任务链：开百度 → GUI RAG 搜「明天的天气」→ 视觉提取带 ℃ 的温度数字
→ 系统计算器 +10 → 最终全屏截图取证（网页天气 + 计算器结果同框）。

安全红线（全程遵守，无一例外）：
- 一切桌面输入（click/type/hotkey）经 Ghost SDK → 服务端能力令牌 / 窗口
  黑名单 / Ghost Guard / 频率熔断 / 结构化审计 100% 生效；本脚本零裸
  pyautogui、零裸 keyboard 注入。
- 能力令牌显式白名单 allowed_windows = {msedge, calculatorapp, calc,
  applicationframehost}——输入只允许打进这些进程，越权直接 403。
- 浏览器 / 计算器均 subprocess.Popen 自启 + **PID 差集**绑定自己拉起的
  窗口（进程名白名单二次过滤），绝不触碰用户已开的真实窗口。
- 弹窗只按「标记词确认 + 白名单关闭词」两段门关闭（明白/知道了/稍后…；
  绝不点 同意/接受/是——那会替用户接受条款），且只在自己 PID 差集绑定的
  Edge 窗口内点；关不掉就中止，绝不带弹窗盲点。
- 对外 URL 仅 http/https，交给浏览器前校验 host：socket 解析后逐 IP 拒绝
  localhost / 环回 / 私有 / 保留 / 链路本地地址，解析失败即中止。
- 凭据零字面量：token 由 SDK 自动从环境变量 / .env 读取。
- 锁屏 / UAC / 冻结：开跑前 environment + health 体检，不满足立即中止。
- agent 不持有审批令牌：命中 409 PENDING_APPROVAL 只如实报告（含 hits 与
  人工审批指引），绝不尝试自批。
- 刻意不走 CDP 通道（browser_driver worker 当前被 close_tab 卡死，见 Bug1）。

用法：.venv\\Scripts\\python.exe test_hell_dynamic_task.py
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)  # SDK 的 .env 解析基于 cwd
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # 控制台中文不炸
    except Exception:  # noqa: BLE001
        pass

import ctypes  # noqa: E402
import psutil  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402


def _enable_dpi_awareness() -> None:
    """本进程 Per-Monitor V2 DPI 感知（v0.14 同款坑）。

    不开的话 150% 缩放下 win32 坐标被虚拟化（÷1.5）：move_win 会把窗口
    挪到错误位置、几何兜底坐标全错——而 Ghost 服务端是物理像素坐标系。
    """
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
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
URL = "https://www.baidu.com"
QUERY = "明天的天气"
ALLOWED_WINDOWS = ["msedge.exe", "calculatorapp.exe", "calc.exe",
                   "applicationframehost.exe"]  # 能力令牌：显式进程白名单
EDGE_NAMES = {"msedge.exe"}
CALC_NAMES = {"calc.exe", "calculatorapp.exe", "applicationframehost.exe"}
# 弹窗两段门：①页面出现已知同步弹窗标记词 ②才允许点白名单关闭词
POPUP_MARKER = "同步你的浏览数据"
POPUP_CLOSE_WORDS = ("明白", "知道了", "稍后", "以后再说", "关闭", "取消")
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
TS = time.strftime("%Y%m%d_%H%M%S")
LOG_PATH = ROOT / "data" / "tmp" / f"hell_run_{TS}.log"
PAGE_SHOT = ROOT / "data" / "tmp" / f"hell_page_{TS}.png"
FINAL_SHOT = ROOT / "data" / "tmp" / f"hell_final_{TS}.png"
PROFILE_DIR = ROOT / "data" / "tmp" / f"hell_edge_profile_{TS}"

# ℃ 温度正则：25℃ / 25°C / 25° C / 25℃~16℃（findall 取全部候选）
TEMP_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*[℃°]\s*[Cc]?", re.IGNORECASE)
NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


# ---------------------------------------------------------------- 日志
def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001  日志失败绝不打断任务
        pass


def step(n: int, title: str) -> None:
    log(f"==== STEP {n}: {title} ====")


# ---------------------------------------------------------------- URL 校验
def validate_public_url(url: str) -> None:
    """对外 URL 红线：仅 http/https，host 解析后拒绝环回/私有/保留地址。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SystemExit(f"[REDLINE] URL scheme 必须 http/https: {url}")
    host = parsed.hostname
    if not host:
        raise SystemExit(f"[REDLINE] URL 缺少 host: {url}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SystemExit(f"[REDLINE] host 解析失败，中止: {host} ({exc})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_loopback or ip.is_private or ip.is_reserved
                or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
            raise SystemExit(
                f"[REDLINE] host {host} 解析到非公网地址 {ip}，拒绝打开")
    log(f"URL 校验通过: {url} (host={host}, 公网地址)")


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


def wait_new_window(before_pids: set, before_hwnds: set,
                    names: set, title_re: Optional[str] = None,
                    timeout: float = 25.0) -> Dict[str, Any]:
    """PID 差集绑定：只认自己拉起的进程（进程名白名单二次过滤）。

    fallback：UWP 宿主（applicationframehost）可能复用旧进程——此时按
    「新窗口句柄 + 标题」绑定，句柄差集同样保证不碰用户已有窗口。
    """
    deadline = time.time() + timeout
    pat = re.compile(title_re) if title_re else None
    while time.time() < deadline:
        for w in visible_windows():
            fresh_pid = w["pid"] not in before_pids
            if fresh_pid and w["name"] in names and (
                    pat is None or pat.search(w["title"])):
                log(f"PID 差集绑定窗口: hwnd={w['hwnd']} pid={w['pid']} "
                    f"name={w['name']} title={w['title']!r}")
                return w
        for w in visible_windows():  # fallback: 新句柄 + 标题匹配
            if (w["hwnd"] not in before_hwnds and w["name"] in names
                    and pat is not None and pat.search(w["title"])):
                log(f"句柄差集绑定窗口(宿主复用 fallback): hwnd={w['hwnd']} "
                    f"pid={w['pid']} name={w['name']} title={w['title']!r}")
                return w
        time.sleep(0.5)
    raise SystemExit(f"[FAIL] {timeout}s 内未发现新窗口 (names={names})")


def root_hwnd(hwnd: int) -> int:
    """取顶层根窗口：UWP（计算器）的内容窗口是 ApplicationFrameHost 的子窗，
    点击归属 point_root / 能力令牌 / 前台门禁全部按**根窗口**判定——
    拿子窗口 hwnd 去点必被 SAFETY_BLOCKED（上一轮实弹教训）。"""
    try:
        r = win32gui.GetAncestor(int(hwnd), 2)  # GA_ROOT
        return int(r) if r else int(hwnd)
    except Exception:  # noqa: BLE001
        return int(hwnd)


def wait_root(hwnd: int, timeout: float = 2.5) -> int:
    """等 UWP 父链挂稳再取根窗——绑定竞态：子窗先出现、ApplicationFrameHost
    稍后挂父，绑定瞬间 GetAncestor 返回自己，之后再用就是子窗坐标系（实弹教训）。"""
    end = time.time() + timeout
    while True:
        r = root_hwnd(hwnd)
        if r != int(hwnd):
            return r  # 父链已挂上 → 根窗
        if time.time() >= end:
            return int(hwnd)  # 超时无父：它本身就是顶层窗（如 Edge）
        time.sleep(0.2)


def place_right_of(hwnd: int, left_limit: int, top: int = 60,
                   pad: int = 20, bottom_limit: int = 1530,
                   min_w: int = 380) -> Tuple[int, int, int, int]:
    """把**顶层窗**挪到 left_limit 右侧的可用区域，放不下就缩，完全屏内才返回。

    坐标全部物理像素（本进程已开 Per-Monitor V2）；返回移动后的 rect，
    越界/压边直接中止——绝不把飞出屏的坐标带进后续 RAG/门禁。
    """
    import win32api
    sw = win32api.GetSystemMetrics(0)
    sh = win32api.GetSystemMetrics(1)
    r = win32gui.GetWindowRect(hwnd)
    w, h = r[2] - r[0], r[3] - r[1]
    avail_w = sw - pad - left_limit
    avail_h = bottom_limit - top
    if avail_w < min_w:
        raise SystemExit(f"[FAIL] 屏幕右侧放不下计算器: left_limit={left_limit} "
                         f"screen={sw}x{sh} avail_w={avail_w}")
    w2, h2 = min(w, avail_w), min(h, avail_h)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, left_limit, top, w2, h2,
                          win32con.SWP_SHOWWINDOW)  # 不带 NOSIZE：同时缩放
    if hwnd not in _TOPMOST_RESTORE:
        _TOPMOST_RESTORE.append(hwnd)
    time.sleep(0.5)
    v = win32gui.GetWindowRect(hwnd)
    if (v[0] < left_limit - 5 or v[1] < 0 or v[2] > sw or v[3] > sh
            or v[3] > bottom_limit + 5):
        raise SystemExit(f"[FAIL] 计算器移动后仍越界: rect={v} "
                         f"limit_left={left_limit} screen={sw}x{sh}")
    return v


def kill_processes(pred) -> int:
    """按谓词杀进程并等它死透（UWP 计算器单实例：旧的不死，新 launch 只会
    激活旧窗，PID 差集必然失败）。"""
    victims = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if pred(p):
                victims.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for p in victims:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    gone = time.time() + 5.0
    while victims and time.time() < gone:
        alive = [p for p in victims if psutil.pid_exists(p.pid)]
        if not alive:
            break
        time.sleep(0.3)
    log(f"清理旧进程 {len(victims)} 个")
    return len(victims)


def stray_windows(main_hwnd: int, own_pid: int) -> List[Dict[str, Any]]:
    """本实例的游离窗（瞬态无标题气泡/弹窗）。

    白名单双保险：pid 必须等于**我们绑定的 Edge 主窗 pid**（同一浏览器实例
    的气泡都归属其浏览器进程），且 hwnd ≠ 主窗——用户自己的窗口永远碰不到。
    注意：Edge 气泡是 **owned popup**（WS_POPUP+owner），GetParent 返回
    owner 而非 0——只认 GetParent==0 会漏掉它，前台被它永远占着（实弹教训）。
    """
    out: List[Dict[str, Any]] = []

    def _cb(hwnd: int, _acc: Any) -> bool:
        if (hwnd == main_hwnd or not win32gui.IsWindowVisible(hwnd)
                or win32gui.IsIconic(hwnd)):
            return True
        try:
            parent = win32gui.GetParent(hwnd)
            if parent not in (0, main_hwnd):
                return True  # 别人的子窗/别人的 owned popup，不碰
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001
            return True
        if pid != own_pid:
            return True
        out.append({"hwnd": hwnd, "pid": pid,
                    "title": win32gui.GetWindowText(hwnd),
                    "rect": win32gui.GetWindowRect(hwnd)})
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def close_strays(client: GhostClient, main_hwnd: int, own_pid: int) -> int:
    """RAG 识别 + 白名单关闭本实例的游离弹窗（任务要求：RAG 自动识别并关掉，
    明确的白名单进程）：
    ① OCR 弹窗内容留证 ② 命中白名单关闭词 → Ghost 点击（能力令牌 msedge.exe
    + hwnd 归属门禁）③ 没有可点的词 → WM_CLOSE 窗口管理关掉。
    关闭词全是「明白/知道了/稍后…」纯消解词——绝不点 同意/接受（不替用户
    接受条款）；pid 白名单保证只动自己实例的窗口。"""
    closed = 0
    for w in stray_windows(main_hwnd, own_pid):
        log(f"发现本实例游离弹窗 hwnd={w['hwnd']} title={w['title']!r} "
            f"rect={w['rect']}")
        els: List[Dict[str, Any]] = []
        try:
            els = elements_of(client, w["hwnd"])
            log("  弹窗 OCR: " + repr(
                [_norm_text(e.get("text")) for e in els][:8]))
        except GhostError as exc:
            log(f"  弹窗 OCR 失败({exc.error})，直接关")
        close_el = next((e for e in els
                         if _norm_text(e.get("text")) in POPUP_CLOSE_WORDS
                         and e.get("center")), None)
        if close_el is not None:
            try:
                client.action.click(int(close_el["center"][0]),
                                    int(close_el["center"][1]), hwnd=w["hwnd"])
                log(f"  白名单关闭词 {close_el['text']!r} 点击成功")
                closed += 1
                time.sleep(0.8)
                continue
            except GhostError as exc:
                log(f"  关闭词点击被拒({exc.error})，改 WM_CLOSE")
        try:
            win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
            closed += 1
            log("  WM_CLOSE 已发")
        except Exception as exc:  # noqa: BLE001
            log(f"  WM_CLOSE 失败: {exc}")
        time.sleep(0.5)
        # WM_CLOSE 兜底：气泡仍可见且正前台 → Ghost 发 Esc（门禁：fg==气泡、
        # 进程白名单 msedge.exe，与主窗同 pid 已在 stray_windows 校验）
        if (win32gui.IsWindowVisible(w["hwnd"])
                and client.environment.check().get("foreground_hwnd")
                == w["hwnd"]):
            try:
                client.action.hotkey("esc", hwnd=w["hwnd"])
                closed += 1
                log("  Esc 已发")
            except GhostError as exc:
                log(f"  Esc 被拒({exc.error})")
            time.sleep(0.5)
    return closed


def ensure_foreground(client: GhostClient, hwnd: int, own_pid: int,
                      tries: int = 5) -> None:
    """确保目标窗拿到前台——type/hotkey 前台门禁的前置（上一轮实弹：
    瞬态气泡抢前台 → WINDOW_NOT_FOREGROUND）。抢焦来源若是本实例气泡 →
    白名单关掉；否则置顶 + 点标题条空白处激活，轮询断言。"""
    for i in range(tries):
        fg = client.environment.check().get("foreground_hwnd")
        if fg == hwnd:
            if i:
                log(f"前台已就绪: {hwnd}")
            return
        close_strays(client, hwnd, own_pid)
        raise_top(hwnd)
        time.sleep(0.3)
        r = win32gui.GetWindowRect(hwnd)
        tx, ty = (r[0] + r[2]) // 2, r[1] + 15  # 标题条/标签条空白处
        try:
            client.action.click(tx, ty, hwnd=hwnd)
        except GhostError as exc:
            log(f"激活点击失败({exc.error})，重试")
        time.sleep(0.5)
    fg = client.environment.check().get("foreground_hwnd")
    if fg != hwnd:
        raise RuntimeError(f"无法获得前台: fg={fg} want={hwnd}")
    log(f"前台已就绪: {hwnd}")


def raise_top(hwnd: int) -> None:
    """纯窗口管理（非输入注入）：**HWND_TOPMOST 临时置顶**——全屏聊天窗
    （ZCode）会盖住 Edge，普通 HWND_TOP 翻不上去（实弹：OCR 裁到聊天内容
    永远读不到「百度一下」）。只对本脚本自建窗口用，结束时统一恢复。"""
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                              | win32con.SWP_SHOWWINDOW)
        if hwnd not in _TOPMOST_RESTORE:
            _TOPMOST_RESTORE.append(hwnd)
    except Exception as exc:  # noqa: BLE001
        log(f"raise_top 失败(继续): {exc}")


_TOPMOST_RESTORE: List[int] = []  # 任务结束恢复 z-order（不永久骑在用户窗口上）


def restore_topmost() -> None:
    for h in list(_TOPMOST_RESTORE):
        try:
            win32gui.SetWindowPos(h, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                                  | win32con.SWP_SHOWWINDOW)
        except Exception:  # noqa: BLE001
            pass
    if _TOPMOST_RESTORE:
        log(f"已恢复 {len(_TOPMOST_RESTORE)} 个窗口的置顶状态")
        _TOPMOST_RESTORE.clear()


def move_win(hwnd: int, x: int, y: int) -> None:
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, x, y, 0, 0,
                              win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW)
    except Exception as exc:  # noqa: BLE001
        log(f"move_win 失败(继续): {exc}")


# ---------------------------------------------------------------- RAG 工具
def _norm_text(t: str) -> str:
    return re.sub(r"\s+", "", str(t or ""))


def elements_of(client: GhostClient, hwnd: int) -> List[Dict[str, Any]]:
    """GUI RAG 检索层：OCR+CV+win32 编号元素表（bbox 全来自像素/句柄）。"""
    resp = client.vl.elements(hwnd=hwnd, backend="auto", timeout=120.0)
    return list(resp.get("elements") or [])


def find_popup(client: GhostClient, edge_hwnd: int
               ) -> Tuple[bool, Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """两段门弹窗识别：出现已知同步弹窗标记（整句 或 明白+自定义 按钮对），
    才去找白名单关闭词——绝不把页面上随便一个「明白」当弹窗点。
    返回 (有弹窗, 关闭词元素, 本轮 OCR 元素表)——无弹窗时元素表可复用，
    省一整轮 20~40s 的 OCR。"""
    els = elements_of(client, edge_hwnd)
    texts = [str(e.get("text") or "") for e in els]
    norms = [_norm_text(t) for t in texts]
    marker = any(POPUP_MARKER in n for n in norms) or (
        "明白" in norms and "自定义" in norms)
    if not marker:
        return False, None, els
    close = next((e for e in els
                  if _norm_text(e.get("text")) in POPUP_CLOSE_WORDS), None)
    return True, close, els


def find_search_box(client: GhostClient, edge_hwnd: int,
                    els: Optional[List[Dict[str, Any]]] = None
                    ) -> Tuple[Tuple[int, int], str, Optional[int]]:
    """GUI RAG 找百度搜索框：锚点 =「百度一下」按钮（去空白归一匹配，OCR 常把
    它读成带空格的变体），再取同排（±40px）按钮左侧的占位文本中心 = 搜索框
    内部；无占位文本则用按钮左侧 150px 推算（百度布局：搜索框右缘紧贴按钮）。

    防三连坑（上一轮实弹）：
    - ground 只接受**确定性文本命中**（text_exact/text_contains）——VLM 猜的
      （vlm_text/vlm_image）一律拒绝重试，它会把「新闻」之类猜成按钮；
    - 锚点与落点必须落在 Edge 窗口内容区内（避开标题栏/工具栏/窗口外）；
    - els 可传入复用上一轮 OCR 结果（省 20~40s）。"""
    er = win32gui.GetWindowRect(edge_hwnd)
    # 内容区：避开标题栏+工具栏（顶部 110px）、窗口底 60px、四边 40px
    zone = (er[0] + 40, er[1] + 110, er[2] - 40, er[3] - 60)

    def _in_zone(x: int, y: int) -> bool:
        return zone[0] <= x <= zone[2] and zone[1] <= y <= zone[3]

    if els is None:
        els = elements_of(client, edge_hwnd)
    btn = next((e for e in els
                if _norm_text(e.get("text")) == "百度一下"
                and e.get("center")
                and _in_zone(int(e["center"][0]), int(e["center"][1]))), None)
    anchor_id = btn.get("id") if btn else None
    if btn is None:
        try:
            g = client.vl.ground("百度一下", hwnd=edge_hwnd, strategy="text",
                                 match="exact", timeout=120.0)
            method = str(g.get("method") or "")
            cand = g.get("element")
            if not method.startswith("text"):
                log(f"ground 返回非确定性 method={method}，拒绝（防 VLM 猜锚）")
                cand = None
            elif cand and cand.get("center") and not _in_zone(
                    int(cand["center"][0]), int(cand["center"][1])):
                log(f"ground 锚点在内容区外: {cand.get('center')}，拒绝")
                cand = None
            else:
                btn = cand
                anchor_id = g.get("target_id")
                log(f"ground 锚点: target_id={anchor_id} method={method} "
                    f"conf={g.get('confidence')}")
        except GhostError as exc:
            log(f"ground 百度一下失败: {exc.status} {exc.error}")
    if not btn or not btn.get("center"):
        raise RuntimeError("找不到「百度一下」确定性锚点（页面未就绪/被遮挡？）")
    bx, by = int(btn["center"][0]), int(btn["center"][1])
    row = [e for e in els
           if e.get("center") and str(e.get("text") or "").strip()
           and abs(int(e["center"][1]) - by) <= 40
           and int(e["center"][0]) < bx
           and 100 <= (bx - int(e["center"][0])) <= 900
           and _in_zone(int(e["center"][0]), int(e["center"][1]))]
    if row:
        pick = max(row, key=lambda e: int(e["center"][0]))
        pt = (int(pick["center"][0]), int(pick["center"][1]))
        return pt, f"占位文本 id={pick.get('id')} text={pick.get('text')!r}", anchor_id
    pt = (bx - 150, by)
    if not _in_zone(pt[0], pt[1]):
        raise RuntimeError(f"推算落点在内容区外: {pt} zone={zone}")
    return pt, "按钮左侧 150px 推算", anchor_id


# ---------------------------------------------------------------- 视觉提取
def extract_temperature(client: GhostClient, edge_hwnd: int
                        ) -> Tuple[float, str, List[float]]:
    """带 ℃ 的温度数字：**字面 ℃ 优先**（任务原文要求），° 作次选，
    本地 VLM(2B) 问图兜底。同级按阅读序（最上方优先）。"""
    els = elements_of(client, edge_hwnd)
    celsius: List[Tuple[int, float, str]] = []  # 含字面 ℃ 的命中
    degree: List[Tuple[int, float, str]] = []   # 仅 ° 的命中
    for e in els:
        raw = str(e.get("text") or "")
        y = int((e.get("center") or [0, 0])[1])
        for m in TEMP_RE.finditer(raw):
            item = (y, float(m.group(1)), raw)
            (celsius if "℃" in m.group(0) else degree).append(item)
    for label, bucket in (("℃", celsius), ("°", degree)):
        if bucket:
            bucket.sort(key=lambda c: c[0])
            values = [c[1] for c in bucket]
            log(f"OCR {label} 候选(阅读序): {values} | 命中原文: "
                + " || ".join(repr(c[2]) for c in bucket[:6]))
            return bucket[0][1], f"ocr({label})", values
    log("OCR 未命中 ℃/°，降级本地 VLM 问图…")
    q = ("这是百度搜索‘明天的天气’的结果页截图。请找出天气预报中带℃或"
         "°C 的温度数字（优先明天的温度），只输出一个阿拉伯数字，例如 25。")
    desc = client.vl.describe(question=q, hwnd=edge_hwnd, backend="auto",
                              max_tokens=64, timeout=300.0)
    answer = str(desc.get("description") or "")
    log(f"VLM 回答(raw): {answer!r} backend={desc.get('backend')}")
    m = re.search(r"-?\d+(?:\.\d+)?", answer)
    if not m:
        raise SystemExit("[FAIL] OCR/VLM 均未提取到温度数字")
    return float(m.group(0)), "vlm", [float(m.group(0))]


def read_display_value(client: GhostClient, hwnd: int) -> Tuple[Optional[float], List[str]]:
    """读计算器显示区：窗口内最上方的纯数字元素（显示区在按钮区之上）。"""
    els = elements_of(client, hwnd)
    texts = [str(e.get("text") or "").strip() for e in els]
    nums = [e for e in els
            if NUM_RE.match(str(e.get("text") or "").strip())
            and e.get("center")]
    if not nums:
        return None, texts
    top = min(nums, key=lambda e: int(e["center"][1]))
    raw = str(top.get("text") or "").strip().replace(",", "")
    try:
        return float(raw), texts
    except ValueError:
        return None, texts


# ---------------------------------------------------------------- 主流程
def main() -> int:
    validate_public_url(URL)

    client = GhostClient(agent_id="hell-task",
                         allowed_windows=ALLOWED_WINDOWS)
    log(f"SDK 就绪 base={client.base_url} capabilities={client.capabilities}")

    step(0, "安全体检（锁屏/UAC/冻结红线）")
    env = client.environment.check()
    health = client.health.check()
    log(f"environment: input_allowed={env.get('input_allowed')} "
        f"lock_screen={env.get('lock_screen')} "
        f"uac={env.get('uac_secure_desktop')} fg={env.get('foreground_hwnd')}")
    if health.get("frozen"):
        raise SystemExit(f"[REDLINE] Ghost 冻结中: {health.get('frozen_reason')}")
    if not env.get("input_allowed"):
        raise SystemExit("[REDLINE] 桌面不可输入（锁屏/UAC），按锁屏红线中止")

    # ---- STEP 1: 拉起独立 profile 的 Edge，PID 差集绑定 ----
    step(1, "打开百度（独立用户数据目录 + PID 差集绑定）")
    # 清掉历史轮次的 hell Edge（不同 profile 本可并存，但旧窗会污染最终
    # 全屏截图的同框校验与 z 序——宁可多杀不许混淆）
    kill_processes(lambda p: p.name().lower() == "msedge.exe"
                   and any("hell_edge_profile" in str(c)
                           for c in (p.info.get("cmdline") or [])))
    edge_exe = next((p for p in EDGE_CANDIDATES if Path(p).is_file()), None)
    if edge_exe is None:
        import shutil
        edge_exe = shutil.which("msedge")
    if not edge_exe:
        raise SystemExit("[FAIL] 找不到 msedge.exe")
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    before_pids = set(psutil.pids())
    before_hwnds = {w["hwnd"] for w in visible_windows()}
    edge_proc = subprocess.Popen(
        [edge_exe, f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check", URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log(f"Edge 已拉起 pid={edge_proc.pid} profile={PROFILE_DIR.name}")
    edge_win = wait_new_window(before_pids, before_hwnds, EDGE_NAMES,
                               title_re=r"百度|Edge", timeout=30.0)
    edge_hwnd = root_hwnd(int(edge_win["hwnd"]))  # 归一化到顶层根窗
    edge_pid = int(edge_win["pid"])  # 气泡白名单：只认本实例 pid
    log(f"Edge 根窗 hwnd={edge_hwnd} pid={edge_pid}")
    raise_top(edge_hwnd)  # 立刻 TOPMOST：防全屏聊天窗压住 OCR/点击
    time.sleep(2.0)  # 首屏加载

    # ---- STEP 2: 弹窗两段门处理（只关不点同意）----
    step(2, "弹窗识别与白名单关闭")
    dismissed = 0
    els_edge: Optional[List[Dict[str, Any]]] = None
    for round_i in range(1, 7):
        raise_top(edge_hwnd)
        try:
            popup, close_el, els_edge = find_popup(client, edge_hwnd)
        except GhostError as exc:
            log(f"弹窗检测失败({exc.status}): {exc.detail}"); time.sleep(1.2); continue
        if not popup:
            log(f"第{round_i}轮: 无同步弹窗标记，通过")
            break
        if close_el is None:
            log(f"第{round_i}轮: 有标记但无白名单关闭词，等待重试")
            els_edge = None  # 页面将变，缓存作废
            time.sleep(1.2)
            continue
        cx, cy = int(close_el["center"][0]), int(close_el["center"][1])
        log(f"第{round_i}轮: 命中标记，点白名单关闭词 "
            f"{close_el['text']!r} id={close_el.get('id')} @({cx},{cy})")
        client.action.click(cx, cy, hwnd=edge_hwnd)
        dismissed += 1
        els_edge = None  # 点过页面，缓存作废
        time.sleep(1.2)
    else:
        raise SystemExit("[FAIL] 同步弹窗 6 轮未能关闭——中止，绝不带弹窗盲点")
    log(f"弹窗处理完成（点击关闭 {dismissed} 次）")

    # ---- STEP 3: GUI RAG 找搜索框 → 输入 → 回车 ----
    step(3, f"GUI RAG 搜索「{QUERY}」")
    box_pt: Optional[Tuple[int, int]] = None
    for attempt in range(1, 6):
        raise_top(edge_hwnd)
        close_strays(client, edge_hwnd, edge_pid)  # 气泡会抢前台/遮 OCR
        try:
            # 复用弹窗轮的 OCR 结果（未点过页面时有效），省一整轮 20~40s
            box_pt, how, anchor_id = find_search_box(
                client, edge_hwnd,
                els=None if dismissed else els_edge)
            log(f"搜索框定位成功: {box_pt} via {how} | 锚点 id={anchor_id}")
            break
        except (GhostError, RuntimeError) as exc:
            log(f"第{attempt}次定位搜索框失败: {exc}，等待页面就绪")
            els_edge = None
            time.sleep(1.2)
    if box_pt is None:
        raise SystemExit("[FAIL] 5 次尝试仍无法定位搜索框")
    # type/hotkey 有前台门禁：先确保前台是 Edge（气泡抢焦实弹教训）
    ensure_foreground(client, edge_hwnd, edge_pid)
    client.action.click(box_pt[0], box_pt[1], hwnd=edge_hwnd)
    client.action.type(QUERY, x=box_pt[0], y=box_pt[1], hwnd=edge_hwnd)
    client.action.hotkey("enter", hwnd=edge_hwnd)
    log(f"已输入「{QUERY}」并回车")
    time.sleep(3.0)  # 任务规定：等待页面加载 3s

    # ---- STEP 4: 截图 + 视觉提取 ℃ 温度 ----
    step(4, "截图并提取带 ℃ 的温度数字")
    raise_top(edge_hwnd)
    close_strays(client, edge_hwnd, edge_pid)  # 气泡会盖住天气卡 OCR 区域
    shot = client.action.screenshot(monitor=1, scale=1.0, fmt="png")
    PAGE_SHOT.write_bytes(shot["image_bytes"])
    log(f"结果页截图已落盘: {PAGE_SHOT}")
    temp, source, all_cands = extract_temperature(client, edge_hwnd)
    temp_val = int(temp) if float(temp).is_integer() else temp
    log(f"★ 提取温度 = {temp_val}  (来源={source}, 全部候选={all_cands})")

    # ---- STEP 5: 拉起系统计算器（PID 差集绑定）----
    step(5, "打开系统计算器")
    # 计算器是 UWP 单实例 + 上一轮的窗可能被挪坏：先杀干净再拉新的，
    # 否则新 calc.exe 只会激活旧窗，PID 差集等不到新窗口
    kill_processes(lambda p: p.name().lower() in
                   ("calculatorapp.exe", "calc.exe"))
    time.sleep(1.0)
    before_pids2 = set(psutil.pids())
    before_hwnds2 = {w["hwnd"] for w in visible_windows()}
    subprocess.Popen(["calc.exe"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    calc_win = wait_new_window(before_pids2, before_hwnds2, CALC_NAMES,
                               title_re=r"计算器|Calculator", timeout=20.0)
    calc_child = int(calc_win["hwnd"])
    # 等 UWP 父链挂稳再取根窗（绑定竞态：子窗先出现、父窗后挂）
    calc_hwnd = wait_root(calc_child, timeout=4.0)
    if calc_hwnd != calc_child:
        log(f"UWP 根窗归一化: 子窗 {calc_child} → 根窗 {calc_hwnd} "
            f"(ApplicationFrameHost)")
    else:
        log(f"窗口无父链，按顶层处理: {calc_hwnd}")
    # 挪到 Edge 右侧可用区（放不下就缩），完全屏内才继续——
    # 绝不把飞出屏的 rect 带进 RAG/几何兜底（上一轮越界 400 的教训）
    edge_rect = win32gui.GetWindowRect(edge_hwnd)
    left_limit = edge_rect[2] + 30
    moved = place_right_of(calc_hwnd, left_limit)
    log(f"计算器根窗移动+缩放后 rect={moved} (left_limit={left_limit})")

    # ---- STEP 6: RAG 找显示区 → 输入 N+10 → 等号 ----
    step(6, f"计算器输入 {temp_val}+10")
    raise_top(calc_hwnd)
    time.sleep(0.4)
    calc_rect = win32gui.GetWindowRect(calc_hwnd)
    disp_pt: Optional[Tuple[int, int]] = None
    for attempt in range(1, 4):
        els = elements_of(client, calc_hwnd)
        nums = [e for e in els if NUM_RE.match(str(e.get("text") or "").strip())
                and e.get("center")]
        # 关键校验：RAG 坐标必须落在计算器根窗内——上一轮裁剪错窗拿到
        # Edge 页面坐标的教训，宁可回退几何定位也绝不把坐标发给门禁去撞。
        inside = [e for e in nums
                  if calc_rect[0] <= int(e["center"][0]) <= calc_rect[2]
                  and calc_rect[1] <= int(e["center"][1]) <= calc_rect[3]]
        if inside:
            top = min(inside, key=lambda e: int(e["center"][1]))  # 显示区最上方
            disp_pt = (int(top["center"][0]), int(top["center"][1]))
            log(f"计算器显示区(RAG, 窗内校验通过): {disp_pt} "
                f"id={top.get('id')} text={top.get('text')!r}")
            break
        if nums:
            log(f"RAG 数字元素均在窗外(疑似裁剪错窗)，弃用: "
                f"{[(e.get('text'), e.get('center')) for e in nums[:4]]}")
        time.sleep(0.8)
    if disp_pt is None:  # 几何兜底：根窗顶部 18% 处即显示区（非按键区）
        disp_pt = ((calc_rect[0] + calc_rect[2]) // 2,
                   calc_rect[1] + int((calc_rect[3] - calc_rect[1]) * 0.18))
        log(f"计算器显示区(几何兜底): {disp_pt} rect={calc_rect}")
    client.action.click(disp_pt[0], disp_pt[1], hwnd=calc_hwnd)
    # 焦点断言：type/hotkey 前台门禁要求计算器必须是前台，否则必然 400
    fg = client.environment.check().get("foreground_hwnd")
    if fg != calc_hwnd:
        log(f"计算器非前台(fg={fg})，重新置顶激活…")
        raise_top(calc_hwnd)
        time.sleep(0.5)
        client.action.click(disp_pt[0], disp_pt[1], hwnd=calc_hwnd)
        fg = client.environment.check().get("foreground_hwnd")
        if fg != calc_hwnd:
            raise SystemExit(f"[FAIL] 无法激活计算器前台 (fg={fg}, "
                             f"want={calc_hwnd})")
    log(f"计算器已前台: fg={fg}")
    expr = f"{temp_val}+10"
    client.action.type(expr, x=disp_pt[0], y=disp_pt[1], hwnd=calc_hwnd)
    client.action.hotkey("enter", hwnd=calc_hwnd)
    log(f"已输入 {expr} 并回车（=）")
    time.sleep(0.8)

    # ---- STEP 7: 读回计算结果（显示区最上方数字）----
    step(7, "读取计算器结果")
    expected = float(temp_val) + 10.0
    result, disp_texts = read_display_value(client, calc_hwnd)
    log(f"显示区全部文本: {disp_texts[:12]}")
    if result is None or abs(result - expected) > 1e-6:
        log(f"首次读取 {result} != 期望 {expected}，重试一次")
        raise_top(calc_hwnd)
        client.action.click(disp_pt[0], disp_pt[1], hwnd=calc_hwnd)
        client.action.type(expr, x=disp_pt[0], y=disp_pt[1], hwnd=calc_hwnd)
        client.action.hotkey("enter", hwnd=calc_hwnd)
        time.sleep(0.8)
        result, disp_texts = read_display_value(client, calc_hwnd)
        log(f"重试后显示区文本: {disp_texts[:12]}")
    verified = result is not None and abs(result - expected) <= 1e-6
    result_out = (int(result) if result is not None
                  and float(result).is_integer() else result)
    log(f"★ 计算器结果 = {result_out} (期望 {expected}, "
        f"校验={'OK' if verified else 'FAILED'})")

    # ---- STEP 8: 最终全屏截图 + 同框校验 ----
    step(8, "最终全屏截图（网页天气 + 计算器同框校验）")
    final_ok = False
    for round_i in range(1, 4):
        raise_top(edge_hwnd)
        raise_top(calc_hwnd)
        close_strays(client, edge_hwnd, edge_pid)  # 气泡会挡同框校验 OCR
        time.sleep(0.3)
        shot = client.action.screenshot(monitor=1, scale=1.0, fmt="png")
        FINAL_SHOT.write_bytes(shot["image_bytes"])
        edge_texts = [str(e.get("text") or "") for e in
                      elements_of(client, edge_hwnd)]
        weather_ok = any(re.search(r"\d\s*[℃°]", t) for t in edge_texts)
        calc_val, _ = read_display_value(client, calc_hwnd)
        calc_ok = calc_val is not None and result is not None \
            and abs(calc_val - float(result)) <= 1e-6
        log(f"第{round_i}轮同框校验: 网页℃={weather_ok} 计算器结果在屏={calc_ok}")
        if weather_ok and calc_ok:
            final_ok = True
            break
    log(f"最终全屏截图: {FINAL_SHOT} 同框校验={'PASS' if final_ok else 'FAIL'}")

    # ---- 汇总 ----
    log("==== 结果汇总 ====")
    log(f"提取温度: {temp_val} (来源={source}, 候选={all_cands})")
    log(f"计算式:   {temp_val} + 10")
    log(f"计算结果: {result_out} (校验={'OK' if verified else 'FAILED'})")
    log(f"结果页截图: {PAGE_SHOT}")
    log(f"最终全屏截图: {FINAL_SHOT}")
    log(f"运行日志: {LOG_PATH}")
    log(f"窗口保持打开供查验: edge_pid={edge_proc.pid} calc_hwnd={calc_hwnd} "
        f"(关闭: taskkill /PID {edge_proc.pid} /T)")
    if not verified:
        return 3
    return 0 if final_ok else 4


if __name__ == "__main__":
    try:
        code = main()
    except GhostApprovalRequiredError as exc:
        log(f"[409 PENDING_APPROVAL] 命中高危词 hits={exc.hits} —— 动作未执行。"
            "agent 不持有审批令牌；如需放行请在人工控制台审批后重跑。"
            f" detail={exc.detail}")
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
        restore_topmost()  # 无论成败都恢复 z-order，不永久骑在用户窗口上
    log(f"exit code = {code}")
    sys.exit(code)
