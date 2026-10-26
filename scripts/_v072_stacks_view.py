"""一次性诊断：打印 logs/stacks.log 最新一轮转储的全部线程摘要。"""
from __future__ import annotations

import sys
from pathlib import Path

text = (Path(__file__).resolve().parents[1] / "logs" / "stacks.log").read_text(
    encoding="utf-8", errors="replace")
last = "Timeout (" + text.split("Timeout (")[-1]
print("LAST_DUMP_BYTES", len(last), flush=True)
blocks = last.split("Thread 0x")[1:]
for b in blocks:
    lines = b.splitlines()
    head = lines[0][:70]
    stack = [l.strip()[:110] for l in lines[1:8] if l.strip()]
    print("=== " + head, flush=True)
    for s in stack:
        print("    " + s, flush=True)
