"""隔离实验：/screenshot 管道是否移动真实鼠标（极简诊断，只读不写）。

三阶段高频监控（GetCursorPos 5ms 采样，进程内独立线程）：
  A. 空闲基线 3s（不发任何请求）
  B. 连续 GET /screenshot x20
  C. 空闲 3s
  D. 连续 GET /dashboard/state x10 + /health x10（面板轮询面）
输出 pos_a/pos_b/pos_c、全部位移事件（带阶段与相对时间戳），并与每次
抓图的起止时刻对齐，可直接判断"位移是否恰好发生在请求窗口内"。

用法：.venv\\Scripts\\python.exe -X utf8 scripts\\_diag_mouse.py
"""
import ctypes
import ctypes.wintypes
import threading
import time

import requests

N_SHOTS = 20
SAMPLE_S = 0.005

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 物理像素，与 ghost 服务坐标一致
except OSError:
    pass

user32 = ctypes.windll.user32


def cursor_pos():
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (int(pt.x), int(pt.y))


def get_json(path):
    return requests.get(f"http://127.0.0.1:8030/api/ghost{path}", timeout=15).json()


stop = threading.Event()
events = []  # (t_rel, kind, payload)


def monitor(t0):
    last = cursor_pos()
    events.append((time.perf_counter() - t0, "pos", last))
    while not stop.is_set():
        p = cursor_pos()
        if p != last:
            events.append((time.perf_counter() - t0, "pos", p))
            last = p
        time.sleep(SAMPLE_S)


def main():
    t0 = time.perf_counter()
    phase = ["init"]
    mon = threading.Thread(target=monitor, args=(t0,), daemon=True)
    mon.start()

    def mark(name):
        phase[0] = name
        events.append((time.perf_counter() - t0, "phase", name))

    mark("A_idle_3s")
    time.sleep(3.0)
    pos_a = cursor_pos()

    mark("B_shots_x20")
    lat = []
    shot_windows = []
    for i in range(N_SHOTS):
        s = time.perf_counter()
        body = get_json("/screenshot?monitor=1&scale=0.35&quality=55")
        e = time.perf_counter()
        lat.append((e - s) * 1000)
        shot_windows.append(((s - t0), (e - t0), body.get("cursor")))
        time.sleep(0.1)
    pos_b = cursor_pos()

    mark("C_idle_3s")
    time.sleep(3.0)
    pos_c = cursor_pos()

    mark("D_panel_poll")
    for _ in range(10):
        get_json("/dashboard/state")
        time.sleep(0.05)
    for _ in range(10):
        get_json("/health")
        time.sleep(0.05)
    pos_d = cursor_pos()
    mark("END")

    stop.set()
    mon.join(timeout=1.0)

    print("pos_a=%s pos_b=%s pos_c=%s pos_d=%s" % (pos_a, pos_b, pos_c, pos_d))
    print("pos_a != pos_b (shot window moved cursor): %s" % (pos_a != pos_b))
    print("shot latency ms: min=%.0f avg=%.0f max=%.0f" % (
        min(lat), sum(lat) / len(lat), max(lat)))
    print("---- timeline (t=0 at script start; MOVE lines only on change) ----")
    cur_phase = "?"
    for t, kind, data in events:
        if kind == "phase":
            cur_phase = data
            print("[%8.3fs] == PHASE %s ==" % (t, data))
        elif kind == "pos":
            # 找最近的 shot 窗口，标注相关性
            nearest = min(shot_windows,
                          key=lambda w: min(abs(t - w[0]), abs(t - w[1])),
                          default=None)
            if nearest is not None:
                gap = min(abs(t - nearest[0]), abs(t - nearest[1]))
                tag = ("inside-shot-window"
                       if nearest[0] <= t <= nearest[1]
                       else "nearest-shot-gap=%.3fs" % gap)
            else:
                tag = "no-shots"
            print("[%8.3fs] MOVE -> %s  (phase=%s, %s)" % (t, data, cur_phase, tag))
    print("---- server-reported cursor per shot (first 5 / last 5) ----")
    for w in shot_windows[:5] + shot_windows[-5:]:
        print("  shot t=%.3f..%.3fs server_cursor=%s" % w)


if __name__ == "__main__":
    main()
