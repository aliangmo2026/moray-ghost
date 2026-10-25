"""v0.6 M3 验收：驾驶舱动作日志（recent 入账 + thumb 白名单 + 路径穿越负例）。

场景：
  A 零位移 move（对当前光标位再 move，不产生可见移动）→ recent 出现
    channel=native、ok=true、带缩略图 basename 的条目。
  B 失败动作（出界坐标）→ recent 出现 ok=false 条目（失败也入账）。
  C thumb 接口：合法 basename → 200 image/jpeg；ghost.db / 带分隔符 / ..
    等穿越负例 → 404（白名单生效）。
  D cdp 通道条目由 test_v06_browser 覆盖，本套不拉浏览器。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()
RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def recent(limit: int = 50):
    r = requests.get(f"http://127.0.0.1:8030/api/ghost/actions/recent?limit={limit}",
                     timeout=15)
    return r.status_code, (r.json() if r.status_code == 200 else {})


def main() -> int:
    # A: 零位移 move（先读 /health 当前光标，再 move 到同一点）
    r = requests.get("http://127.0.0.1:8030/api/ghost/health", timeout=10)
    cur = r.json().get("cursor") or [10, 10]
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers=H,
                      json={"action": "move", "x": cur[0], "y": cur[1],
                            "settle_seconds": 0.05},
                      timeout=30)
    check("A-1: 零位移 move → 200", r.status_code == 200,
          f"status={r.status_code}")
    code, body = recent()
    actions = body.get("actions") or []
    move_entries = [a for a in actions
                    if a.get("channel") == "native" and a.get("action") == "move"
                    and a.get("ok") is True]
    check("A-2: recent 出现 native move 成功条目", code == 200 and move_entries,
          f"count={len(actions)}")
    entry = move_entries[0] if move_entries else {}
    thumb = entry.get("thumb")
    check("A-3: 条目带缩略图且为 basename（无路径成分）",
          bool(thumb) and "/" not in thumb and "\\" not in thumb
          and ".." not in thumb, f"thumb={thumb!r}")
    if thumb:
        rt = requests.get(f"http://127.0.0.1:8030/api/ghost/actions/thumb/{thumb}",
                          timeout=15)
        check("A-4: thumb 接口 → 200 image/jpeg",
              rt.status_code == 200 and rt.headers.get("content-type", "")
              .startswith("image/"),
              f"status={rt.status_code} type={rt.headers.get('content-type')}")
    else:
        check("A-4: thumb 接口 → 200 image/jpeg", False, "no thumb to fetch")

    # B: 失败动作也入账
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers=H,
                      json={"action": "click", "x": 999999, "y": 500,
                            "settle_seconds": 0},
                      timeout=30)
    check("B-1: 出界 click → 400", r.status_code == 400, f"{r.status_code}")
    code, body = recent()
    fail_entries = [a for a in (body.get("actions") or [])
                    if a.get("ok") is False and a.get("status") == 400]
    check("B-2: 失败动作入账（ok=false status=400）", bool(fail_entries),
          f"fails={len(fail_entries)}")

    # C: thumb 白名单负例（401/404 都算拒绝：路径被中间件/白名单任一环节拦下）
    for name, label in [("ghost.db", "db 文件"),
                        ("..%5Cghost.db", "编码反斜杠穿越"),
                        ("../../ghost.db", "点段穿越")]:
        rt = requests.get(f"http://127.0.0.1:8030/api/ghost/actions/thumb/{name}",
                          timeout=15)
        check("C-" + label + " → 拒绝", rt.status_code in (401, 404),
              f"status={rt.status_code}")

    total = len(RESULTS)
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== v06 actionlog verify: {passed}/{total} passed ==", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
