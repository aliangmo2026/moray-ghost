"""v0.14 长时运行资源压测：analyze_screen / draw_trajectory 各 N 次（默认 100）。

模拟挂机数天场景，监控**服务端进程**（泄漏都在服务端：抓屏/VLM/安全管道）
的资源曲线，每 10 次打印一行：
    GDI 句柄数 / USER 句柄数 / 服务端 RSS / nvidia-smi 显存 / 截图文件数
（外加本测试进程 RSS 做对照；截图文件数盯的就是"审计 before/after 落盘
无界增长"——实测已累积 13,594 张 / 3.4GB，v0.14 加了 SHOT_MAX_FILES 修剪。）

泄漏判据（warmup 后基线 vs 收尾，中位数对比）：
    ΔGDI ≤ +30、ΔUSER ≤ +30、ΔRSS ≤ +120MB、ΔGPU ≤ +256MB、截图数 ≤ 上限
超阈 → FAIL 并打印哪一段（analyze/draw）在涨，供定点排查。

压测靶窗自建：Tk 画布 "v14-stress-target"，draw_trajectory 的笔迹全部落在
自己窗口内（capabilities=["python.exe"]，前台门禁每次 force_foreground），
绝不在用户桌面上乱画；is_painting=true 豁免频率熔断（v0.11 语义，其余门禁
一道不少）。analyze 每次以该窗口为靶，VLM 走服务端真实后端。

用法：
    python scripts/test_v14_stress.py                # N=100 全量
    python scripts/test_v14_stress.py --n 3 --backend ocr   # 快速探测
    python scripts/test_v14_stress.py --backend llama       # 指定感知后端
"""
from __future__ import annotations

import argparse
import ctypes
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402

import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import config  # noqa: E402

TOKEN = config.ensure_token()
CAPS = {"allowed_windows": ["python.exe"]}
BASE = "http://127.0.0.1:8030/api/ghost"
WINDOW_TITLE = "v14-stress-target"

# 泄漏判据（warmup 基线 → 收尾中位数）
TH_GDI = 30
TH_USER = 30
TH_RSS_MB = 120
TH_GPU_MB = 256

RESULTS: list = []
PROCS: list = []
SAMPLES: list = []   # {phase, i, gdi, user, rss_mb, gpu_mb, shots}
PROBE_SERVER_PID: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return bool(ok)


def hdr() -> dict:
    return {"X-Ghost-Token": TOKEN}


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def kill_by_port(port: int) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(0.5)
    except Exception:  # noqa: BLE001
        pass


def spawn(args: list) -> subprocess.Popen:
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    PROCS.append(proc)

    def _drain() -> None:
        try:
            for _ in proc.stdout:
                pass
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_drain, daemon=True).start()
    return proc


def wait_health(timeout: float = 40.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(f"{BASE}/health", timeout=2)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return r.json()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.35)
    return {}


def cleanup() -> None:
    """杀整个进程树（venv python.exe 是启动器，真解释器是 child）。"""
    for p in PROCS:
        try:
            parent = psutil.Process(p.pid)
            tree = parent.children(recursive=True) + [parent]
        except psutil.Error:
            tree = []
        for proc in tree:
            try:
                proc.terminate()
            except psutil.Error:
                pass
        gone, alive = psutil.wait_procs(tree, timeout=6)
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                pass


# ---------------- 资源采样 ----------------

_K32 = ctypes.windll.kernel32
_U32 = ctypes.windll.user32


def server_tree_pids() -> list:
    """服务端进程树（venv python.exe 是启动器：真解释器在 children 里）。"""
    pids = []
    try:
        proc = PROCS[0]
        p = psutil.Process(proc.pid)
        pids.append(proc.pid)
        for c in p.children(recursive=True):
            try:
                if c.is_running():
                    pids.append(c.pid)
            except psutil.Error:
                pass
    except Exception:  # noqa: BLE001
        pass
    if not PROBE_SERVER_PID and pids:
        PROBE_SERVER_PID.extend(pids)
    return pids


def gdi_user_of_tree() -> tuple:
    """进程树 GDI/USER 句柄数之和（GetGuiResources 逐 pid）。"""
    gdi = usr = 0
    any_ok = False
    for pid in server_tree_pids():
        # PROCESS_QUERY_INFORMATION(0x0400) | PROCESS_VM_READ(0x0010)
        h = _K32.OpenProcess(0x0410, False, int(pid))
        if not h:
            h = _K32.OpenProcess(0x1000, False, int(pid))  # LIMITED 兜底
        if not h:
            continue
        try:
            g = int(_U32.GetGuiResources(h, 0))
            u = int(_U32.GetGuiResources(h, 1))
            if g or u:
                any_ok = True
            gdi += g
            usr += u
        finally:
            _K32.CloseHandle(h)
    if not any_ok:
        return None, None
    return gdi, usr


def rss_mb_of_tree() -> float:
    total = 0
    for pid in server_tree_pids():
        try:
            total += psutil.Process(pid).memory_info().rss
        except psutil.Error:
            pass
    return round(total / (1024 * 1024), 1)


_GPU_CACHE: dict = {"val": None, "ts": 0.0}


def gpu_used_mb() -> "int | None":
    """nvidia-smi 全卡显存占用（MB；全局值，别的进程也会算——看增量）。"""
    now = time.time()
    if now - _GPU_CACHE["ts"] < 4.0 and _GPU_CACHE["val"] is not None:
        return _GPU_CACHE["val"]
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
        vals = [int(float(x)) for x in out.stdout.replace(",", " ").split()
                if x.strip().isdigit() or x.strip().replace(".", "", 1).isdigit()]
        val = sum(vals) if vals else None
    except Exception:  # noqa: BLE001
        val = None
    _GPU_CACHE.update(val=val, ts=now)
    return val


def shot_files() -> int:
    try:
        return sum(1 for p in config.SCREENSHOT_DIR.glob("*") if p.is_file())
    except OSError:
        return -1


def sample(phase: str, i: int, tag: str = "") -> dict:
    gdi, usr = gdi_user_of_tree()
    row = {"phase": phase, "i": i,
           "gdi": gdi, "user": usr,
           "rss_mb": rss_mb_of_tree(),
           "gpu_mb": gpu_used_mb(),
           "client_mb": round(psutil.Process().memory_info().rss
                              / (1024 * 1024), 1),
           "shots": shot_files()}
    SAMPLES.append(row)
    print(f"[stress] {phase} iter={i}{tag} gdi={row['gdi']} user={row['user']} "
          f"rss={row['rss_mb']}MB gpu={row['gpu_mb']}MB "
          f"shots={row['shots']} client={row['client_mb']}MB", flush=True)
    return row


# ---------------- 靶窗（自建，只在自己窗口里画） ----------------

class StressTarget:
    def __init__(self) -> None:
        self.hwnd = 0
        self.canvas_hwnd = 0
        self.motions = 0
        self._lock = threading.Lock()
        self._root: "tk.Tk | None" = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        if not self._ready.wait(10):
            raise RuntimeError("Tk stress target did not come up")

    def _run(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title(WINDOW_TITLE)
        root.geometry("720x520+200+160")
        canvas = tk.Canvas(root, bg="white", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.bind("<B1-Motion>", self._on_motion)
        canvas.bind("<ButtonPress-1>", self._on_motion)
        for _ in range(100):
            root.update()
            if canvas.winfo_width() > 200 and canvas.winfo_height() > 200:
                break
            time.sleep(0.05)
        self.cw = canvas.winfo_width()
        self.ch = canvas.winfo_height()
        frame = root.frame()
        self.hwnd = int(frame, 16) if isinstance(frame, str) else int(frame)
        self.canvas_hwnd = int(canvas.winfo_id())
        # 压测只读事件计数，不画持久墨迹（画布保持空白，干净可复跑）
        self._ready.set()
        root.mainloop()

    def _on_motion(self, _event) -> None:
        with self._lock:
            self.motions += 1

    def motion_count(self) -> int:
        with self._lock:
            return self.motions

    def stroke_points(self, i: int) -> list:
        """画布内的 5 点波浪线（物理像素）；逐次偏移避免熔断指纹重复。"""
        ox, oy = win32gui.ClientToScreen(self.canvas_hwnd, (0, 0))
        dy = (i % 40) - 20  # y 轴小幅轮换
        base_y = self.ch * 0.30
        pts = []
        for k, fx in enumerate((0.15, 0.32, 0.5, 0.68, 0.85)):
            lx = int(self.cw * fx)
            ly = int(base_y + (60 if k % 2 else 0) + dy)
            ly = max(6, min(self.ch - 6, ly))
            pts.append([ox + lx, oy + ly])
        return pts

    def place_center(self) -> None:
        v = _virtual_screen()
        x = v["left"] + (v["width"] - 740) // 2
        y = v["top"] + (v["height"] - 540) // 2
        win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOP, x, y, 0, 0,
                              win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
        time.sleep(0.3)

    def close(self) -> None:
        # 不跨线程调 Tk（after/destroy 从别的线程调会 Tcl_AsyncDelete 警告）：
        # 线程是 daemon，进程退出时随解释器一起收掉。
        self._root = None


def _virtual_screen() -> dict:
    from app.core import screen_mss
    return screen_mss.virtual_screen()


def force_foreground(hwnd: int) -> bool:
    for _ in range(5):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            try:
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.15)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


# ---------------- 两个压测相位 ----------------

def phase_analyze(n: int, backend_name: str) -> tuple:
    """analyze_screen × N：/vl/analyze 打真实感知后端（服务端执行）。"""
    ok = fail = 0
    first_err = None
    t0 = time.perf_counter()
    for i in range(1, n + 1):
        try:
            r = requests.post(f"{BASE}/vl/analyze",
                              headers=hdr(),
                              json={"window": WINDOW_TITLE,
                                    "backend": backend_name},
                              timeout=300)
            body = r.json()
        except Exception as exc:  # noqa: BLE001
            body = {"ok": False, "error": type(exc).__name__, "detail": str(exc)}
        if body.get("ok") or r.status_code == 200 and body.get("controls"):
            ok += 1
        else:
            fail += 1
            if first_err is None:
                first_err = f"http={r.status_code} " + \
                    f"{body.get('error')}: {str(body.get('detail'))[:160]}"
        if i % 10 == 0:
            sample("analyze", i,
                   f" ok={ok} fail={fail} "
                   f"avg={ (time.perf_counter()-t0)/i :.2f}s")
    return ok, fail, first_err


def phase_draw(n: int, target: StressTarget) -> tuple:
    """draw_trajectory × N：真实拖拽，全部落在自建画布内。"""
    ok = fail = 0
    landed = 0
    first_err = None
    t0 = time.perf_counter()
    for i in range(1, n + 1):
        before = target.motion_count()
        force_foreground(target.hwnd)
        pts = target.stroke_points(i)
        body = {"points": pts, "hwnd": target.hwnd, "duration_ms": 150,
                "smooth_iterations": 2, "settle_seconds": 0.05,
                "monitor": 1, "is_painting": True, "capabilities": CAPS}
        result = None
        for attempt in (1, 2):  # 前台被抢 → 恢复重试一次
            force_foreground(target.hwnd)
            try:
                r = requests.post(f"{BASE}/draw_trajectory", headers=hdr(),
                                  json=body, timeout=120)
                result = r.json()
                result["http_status"] = r.status_code
            except Exception as exc:  # noqa: BLE001
                result = {"http_status": 0, "ok": False,
                          "error": type(exc).__name__, "detail": str(exc)}
            if result.get("http_status") == 200:
                break
            if attempt == 1 and result.get("http_status") in (400, 409, 503):
                time.sleep(0.6)
                continue
            break
        if result.get("http_status") == 200 and result.get("ok"):
            ok += 1
        else:
            fail += 1
            if first_err is None:
                first_err = f"http={result.get('http_status')} " + \
                    f"{result.get('error')}: {str(result.get('detail'))[:160]}"
        # 笔迹必须真的落进画布（输入到达目标应用，而不是仅返回 200）
        deadline = time.time() + 1.2
        while time.time() < deadline:
            if target.motion_count() > before:
                landed += 1
                break
            time.sleep(0.05)
        if i % 10 == 0:
            sample("draw", i,
                   f" ok={ok} fail={fail} landed={landed} "
                   f"avg={(time.time() - t0) / i:.2f}s")
    return ok, fail, landed, first_err


# ---------------- 泄漏判定 ----------------

def verdict() -> bool:
    rows = [s for s in SAMPLES if s["gdi"] is not None]
    warm = [s for s in rows if s["i"] > 0]  # 跳过 i=0（模型冷启动前后跳变）
    # 基线 = warmup 后最早 2 个样本（首相位 10/20 次，模型已加载）；
    # 收尾 = 最后 3 个样本（第二相位末尾）。
    base = warm[:2]
    tail = warm[-3:]
    if not base or not tail:
        check("V 采样足够（基线+收尾）", False, f"samples={len(rows)}")
        return False

    def med(key):
        return statistics.median(s[key] for s in base if s[key] is not None), \
            statistics.median(s[key] for s in tail if s[key] is not None)

    ok_all = True
    for key, th, unit in (("gdi", TH_GDI, "句柄"),
                          ("user", TH_USER, "句柄"),
                          ("rss_mb", TH_RSS_MB, "MB"),
                          ("gpu_mb", TH_GPU_MB, "MB")):
        b, t = med(key)
        if b is None or t is None:
            print(f"[info] {key}: 无数据（跳过）", flush=True)
            continue
        delta = t - b
        ok = delta <= th
        ok_all &= ok
        check(f"V {key} 无持续上涨（{b:.0f} → {t:.0f}, Δ{delta:+.0f}"
              f"{unit} ≤ +{th}）", ok)
    # 截图文件数 ≤ 配置上限（看收尾样本：i=0 的基线是修剪前历史存量，
    # 首次落盘即触发修剪——判定以"运行结束时"为准）
    shots = [s["shots"] for s in rows if s["shots"] is not None]
    if shots:
        cap = config.SHOT_MAX_FILES
        final = shots[-1]
        ok = cap <= 0 or final <= cap + 20
        ok_all &= ok
        check(f"V 截图文件数有界（收尾={final} ≤ {cap}+20，"
              f"起始存量={shots[0]}）", ok,
              "SHOT_MAX_FILES 修剪生效" if ok else "修剪未生效")
    return ok_all


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="每相位迭代次数")
    ap.add_argument("--backend", default="auto",
                    help="analyze 感知后端 auto|native|ocr|llama|mock")
    args = ap.parse_args()

    print(f"v0.14 长时压测 · analyze × {args.n} (backend={args.backend})"
          f" + draw × {args.n} · SHOT_MAX_FILES={config.SHOT_MAX_FILES}")
    try:
        if not port_free(8030):
            kill_by_port(8030)
            time.sleep(1.0)
        spawn([sys.executable, str(ROOT / "run.py")])
        health = wait_health()
        if health.get("status") != "ok":
            check("S 服务启动", False, str(health)[:200])
            return 1
        check("S 服务启动", True,
              f"vl_backend={health.get('vl_backend')}")

        target = StressTarget()
        target.place_center()

        sample("baseline", 0)
        a_ok, a_fail, a_err = phase_analyze(args.n, args.backend)
        check(f"A analyze×{args.n} 全部成功", a_fail == 0,
              f"ok={a_ok} fail={a_fail}" + (f" first={a_err}" if a_err else ""))
        sample("analyze", args.n, " (phase end)")

        d_ok, d_fail, landed, d_err = phase_draw(args.n, target)
        check(f"D draw×{args.n} 全部 200", d_fail == 0,
              f"ok={d_ok} fail={d_fail}" + (f" first={d_err}" if d_err else ""))
        check(f"D draw 笔迹真实落进画布 ≥{args.n * 9 // 10}",
              landed >= args.n * 9 // 10, f"landed={landed}")
        sample("draw", args.n, " (phase end)")

        verdict()
        target.close()
    finally:
        cleanup()

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r)
    print(f"\n===== v14 压测：{passed}/{total} PASS · 样本 {len(SAMPLES)} =====")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
