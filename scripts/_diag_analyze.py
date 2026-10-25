"""分析 _diag_mouse.py 的轨迹输出：找"单样本大跳变"（机器 SetCursorPos 特征）。

人手移动：每 5ms 采样 1~15px 平滑小步；机器拉扯：单样本几十~几百像素的瞬跳。
用法：把 TRACE_LOG 常量改成 _diag_mouse.py 的输出文件，直接运行。
"""
import re

TRACE_LOG = r"C:\Users\莫\AppData\Local\Temp\zcode-bash-lvSxyC\session_5f5cc2d7458d35782b8339bd12d510d8\call_5aa5e78862b14adc934e45dc-stdout.log"

rx = re.compile(r"\[\s*([\d.]+)s\] MOVE -> \((\d+), (\d+)\)")
rows = []
with open(TRACE_LOG, encoding="utf-8") as f:
    for line in f:
        m = rx.search(line)
        if m:
            rows.append((float(m.group(1)), int(m.group(2)), int(m.group(3))))

pairs = list(zip(rows, rows[1:]))
jumps = []
for (t1, x1, y1), (t2, x2, y2) in pairs:
    d = max(abs(x2 - x1), abs(y2 - y1))
    jumps.append((t1, t2, d, (x1, y1), (x2, y2)))
jumps.sort(key=lambda j: -j[2])

print("samples:", len(rows), " pairs:", len(pairs))
print("top single-sample deltas (shot phase = 3.0..11.1s):")
for t1, t2, d, p1, p2 in jumps[:10]:
    tag = "IN-SHOT-WINDOW" if 3.0 <= t1 <= 11.1 else "outside"
    print("  t=%.3f..%.3fs delta=%dpx %s -> %s  %s" % (t1, t2, d, p1, p2, tag))
big = [j for j in jumps if j[2] > 40]
print("samples with delta>40px (machine-jump threshold):", len(big))
