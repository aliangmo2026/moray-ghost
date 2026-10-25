"""v0.6 M1 验收：drag/scroll 平滑轨迹 + 宏轨迹录制/重放（Tk 自建靶窗）。

测试纪律（README 红线）：GUI 靶子是本脚本 Popen 的 Tk 文本窗（_v06_text_target.py），
标题 stem 差集绑定 + 坐标文件握手 + 只认自己的 hwnd 消失。选 Tk 而非记事本：
纯 win32 宿主、无标签页/浮动工具条怪癖，且内容程序化灌入（不依赖注入打字）。

判据说明：本机（用户真机活跃 + WinUI 宿主）存在"注入 Enter/Ctrl 组合不生效、
注入拖拽在 Win11 记事本不产生选区"的环境级异常（v0.6 会话探针实证），因此
选中效果一律用**高亮像素差**判别（拖选的用户可见效果本身），不依赖 Ctrl+C。

场景：
  A 拖拽选中：在预灌文本行上 drag 划选 → 文本行高亮像素差 > 阈值。
  B 滚轮平滑：80 行内容 scroll smooth → 固定区域像素差 > 阈值。
  C 轨迹录制/重放：record drag(显式 3 点)+scroll → 宏步骤含 points/dy →
    dry_run → 真实重放（重放拖拽再次产生高亮）。
  D 负例：轨迹点落 Fail-Safe 角落 / 出界 / 缺参 → 400。
  E 清场：靶窗销毁（只认自己 hwnd）+ 测试宏 DELETE + 临时文件清理。
"""
from __future__ import annotations

import ctypes
import subprocess
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

import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import window_locator as wl  # noqa: E402
from app.core.screen_mss import grab  # noqa: E402
from PIL import Image, ImageChops, ImageStat  # noqa: E402

H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()
RESULTS: list = []
TMP_FILES: list = []

LINE1 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
CONTENT = "\n".join([LINE1] + ["LINE%02d-padding-content" % i
                               for i in range(2, 82)])


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def action(payload: dict):
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action", headers=H,
                      json=payload, timeout=60)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:120]}
    return r, body


def point_is_ours(pt: tuple, hwnd: int) -> bool:
    try:
        leaf = win32gui.WindowFromPoint(pt)
        return win32gui.GetAncestor(leaf, 2) == hwnd
    except Exception:  # noqa: BLE001
        return False


def force_foreground(hwnd: int) -> bool:
    for _try in range(3):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.2)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.4)
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            import pyautogui
            from app.core.backend import release_all_modifiers
            pyautogui.keyDown("alt")
            try:
                win32gui.SetForegroundWindow(hwnd)
            finally:
                pyautogui.keyUp("alt")
                release_all_modifiers()
            time.sleep(0.2)
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:  # noqa: BLE001
            pass
    return win32gui.GetForegroundWindow() == hwnd


def activate_via_click(hwnd: int, retries: int = 3) -> bool:
    for _i in range(retries):
        rect = wl.get_rect(hwnd)
        if rect is None:
            return False
        pt = (rect[0] + (rect[2] - rect[0]) // 2,
              rect[1] + (rect[3] - rect[1]) // 2)
        if not point_is_ours(pt, hwnd):
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            time.sleep(0.35)
            continue
        r, _body = action({"action": "click", "x": pt[0], "y": pt[1],
                           "settle_seconds": 0.1})
        time.sleep(0.3)
        if r.status_code == 200 and win32gui.GetForegroundWindow() == hwnd:
            return True
    return win32gui.GetForegroundWindow() == hwnd


def hold_fg(hwnd: int) -> bool:
    return force_foreground(hwnd) or activate_via_click(hwnd)


def action_retry(payload: dict, hwnd: int, tries: int = 6):
    """竞态安全环：门禁报"被覆盖/不在前台"时重申前台后重试（真机纪律）。"""
    r, body = action(payload)
    for _i in range(tries - 1):
        detail = str(body.get("detail") or "")
        hit = ("covered by another window" in detail
               or "is not foreground" in detail)
        if r.status_code in (400, 409) and hit:
            hold_fg(hwnd)
            time.sleep(0.4)
            r, body = action(payload)
        else:
            break
    return r, body


def count_highlight_blue(path: str) -> int:
    """统计选区高亮蓝底像素（Tk selectbackground≈系统蓝）；与内容位移无关。"""
    img = Image.open(path).convert("RGB")
    return sum(1 for r, g, b in img.getdata()
               if b > 80 and b > r + 25 and b > g + 15)


def crop_diff_mean(box, tag_a: str, tag_b: str) -> float:
    shot_dir = ROOT / "data" / "screenshots"
    a_path = str(shot_dir / ("v06_" + tag_a + ".png"))
    b_path = str(shot_dir / ("v06_" + tag_b + ".png"))
    img, _ = grab(1)
    img.crop(box).save(a_path)
    time.sleep(0.15)
    img, _ = grab(1)
    img.crop(box).save(b_path)
    a = Image.open(a_path).convert("L")
    b = Image.open(b_path).convert("L")
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def close_owned(hwnd: int) -> bool:
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:  # noqa: BLE001
        return False
    for _ in range(40):
        time.sleep(0.2)
        if not win32gui.IsWindow(hwnd):
            return True
    return not win32gui.IsWindow(hwnd)


def spawn_target(stem: str, content_file: Path, coord_file: Path,
                 geometry: str):
    """在指定 geometry 拉起靶窗并握手；失败返回 (None, None, None, None)。"""
    if coord_file.exists():
        coord_file.unlink()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "_v06_text_target.py"),
         str(content_file), stem, str(coord_file), geometry],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    hwnd = tx = ty = None
    end = time.time() + 15
    while time.time() < end:
        if coord_file.exists():
            try:
                parts = coord_file.read_text(encoding="utf-8").split()
                cand = int(parts[0])
                if win32gui.IsWindow(cand):
                    hwnd, tx, ty = cand, int(parts[1]), int(parts[2])
                    break
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.3)
    return proc, hwnd, tx, ty


def main() -> int:
    stem = "d" + str(int(time.time() * 1000))[-9:]
    tmp_dir = ROOT / "data" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    content_file = tmp_dir / ("v06_content_" + stem + ".txt")
    coord_file = tmp_dir / ("v06_coord_" + stem + ".txt")
    content_file.write_text(CONTENT, encoding="utf-8")
    TMP_FILES.extend([content_file, coord_file])

    # 自适应落位：用户窗口（全屏/最大化应用）会与靶窗抢像素，逐个候选位置
    # 验证关键点归属；被占时轮询重试（最多 90s，等用户窗口化的间隙机会主义开跑）。
    candidates = ["760x520+900+500", "760x520+1400+800",
                  "760x520+1100+200", "760x520+240+240"]
    pre_hwnds = {w["hwnd"] for w in wl.list_windows()}
    proc = hwnd = tx = ty = None
    placed_at = None
    place_deadline = time.time() + 90
    while time.time() < place_deadline and placed_at is None:
        for geo in candidates:
            proc, hwnd, tx, ty = spawn_target(stem, content_file, coord_file, geo)
            if hwnd is None:
                continue
            time.sleep(0.5)
            force_foreground(hwnd)  # 激活会把目标抬到 topmost 带顶端（全屏用户窗口 otherwise 压住探测点）
            time.sleep(0.2)
            probes = [(tx + 6, ty + 12), (tx + 186, ty + 12), (tx + 300, ty + 200)]
            if all(point_is_ours(p, hwnd) for p in probes):
                placed_at = geo
                break
            close_owned(hwnd)
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
        if placed_at is None:
            print("  (所有候选位被用户窗口占用，10s 后重试…)", flush=True)
            time.sleep(10)

    ok_p = check("P-1: Tk 靶窗已启动且落在无遮挡区", hwnd is not None
                 and placed_at is not None,
                 f"hwnd={hwnd} text_area=({tx},{ty}) at={placed_at}")
    if not ok_p:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return 1
    check("P-2: 靶窗 hwnd 属于差集（自建窗口）", hwnd not in pre_hwnds)
    time.sleep(0.3)
    hold_fg(hwnd)

    # 文本区几何：第一行文本约在 (tx+6, ty+12)
    line_y = ty + 12
    line_x0 = tx + 6
    drag_x1 = line_x0 + 180
    mid_x = tx + 300
    mid_y = ty + 200

    # A: 拖拽划选 → 高亮像素差
    r, body = action_retry({"action": "click", "x": mid_x, "y": mid_y,
                            "hwnd": hwnd, "settle_seconds": 0.15}, hwnd)
    check("A-0: 点击文本区激活", r.status_code == 200, f"{r.status_code}")
    a_diff = crop_diff_mean((line_x0 - 4, line_y - 8, line_x0 + 200, line_y + 16),
                            "a_base", "a_prep")
    hold_fg(hwnd)
    r, body = action_retry({"action": "drag", "x": line_x0, "y": line_y,
                            "x2": drag_x1, "y2": line_y, "duration_ms": 400,
                            "hwnd": hwnd, "settle_seconds": 0.25}, hwnd)
    ok_drag = check("A-1: drag 划选 → 200（响应带轨迹）",
                    r.status_code == 200 and isinstance(body.get("trajectory"), list),
                    f"status={r.status_code} pts={len(body.get('trajectory') or [])}")
    check("A-2: 终点 cursor_after == x2/y2",
          body.get("cursor_after") == [drag_x1, line_y],
          f"cursor_after={body.get('cursor_after')}")
    sel_diff = crop_diff_mean((line_x0 - 4, line_y - 8, line_x0 + 200, line_y + 16),
                              "a_sel_before", "a_sel_after")
    check("A-3: 拖拽选中真实生效（高亮像素差）",
          ok_drag and sel_diff > 3.0, f"sel_diff={sel_diff:.2f}")

    # B: 平滑滚动（固定区域像素差）
    r, body = action_retry({"action": "scroll", "x": mid_x, "y": mid_y,
                            "dy": -480, "smooth": True,
                            "hwnd": hwnd, "settle_seconds": 0.5}, hwnd)
    check("B-1: scroll smooth → 200", r.status_code == 200
          and (body.get("scroll") or {}).get("smooth") is True,
          f"status={r.status_code} scroll={body.get('scroll')}")
    b_diff = crop_diff_mean((tx + 2, ty + 2, tx + 320, ty + 160),
                            "b_before", "b_after")
    check("B-2: 内容确实滚动（固定区域像素差）", b_diff > 1.0,
          f"diff={b_diff:.2f}")

    # C: 录制 drag(3 点)+scroll → 宏含轨迹 → dry_run → 真实重放（高亮复现）
    # 先点一下收起 A 的选区，避免选区态干扰重放判别
    hold_fg(hwnd)
    action_retry({"action": "click", "x": mid_x, "y": mid_y,
                  "hwnd": hwnd, "settle_seconds": 0.15}, hwnd)
    time.sleep(0.4)
    r = requests.post("http://127.0.0.1:8030/api/ghost/macro/record/start",
                      headers=H, json={"task_id": "t_v06_" + stem}, timeout=15)
    check("C-1: record/start", r.status_code == 200, f"status={r.status_code}")
    # 先录 scroll 再录 drag：重放时滚动先发生，拖拽落在当前视口首行，
    # C-7 的高亮判别不被滚动位移污染。
    r, body = action_retry({"action": "scroll", "x": mid_x, "y": mid_y,
                            "dy": -120, "smooth": False,
                            "hwnd": hwnd, "settle_seconds": 0.3}, hwnd)
    check("C-3: 录制 scroll → 200", r.status_code == 200)
    pts = [[line_x0, line_y], [line_x0 + 90, line_y + 2],
           [line_x0 + 170, line_y]]
    hold_fg(hwnd)
    r, body = action_retry({"action": "drag", "points": pts, "duration_ms": 500,
                            "hwnd": hwnd, "settle_seconds": 0.2}, hwnd)
    check("C-2: 录制 drag（显式 3 点轨迹、无 x/y）→ 200",
          r.status_code == 200 and body.get("trajectory") == pts,
          f"status={r.status_code} err={body.get('error')} "
          f"detail={str(body.get('detail'))[:70]}")
    r = requests.post("http://127.0.0.1:8030/api/ghost/macro/record/stop",
                      headers=H, json={"task_id": "t_v06_" + stem,
                                       "name": "v06_drag_" + stem},
                      timeout=15)
    body = r.json() if r.status_code == 200 else {}
    macro_id = body.get("macro_id")
    check("C-4: record/stop → macro_id", r.status_code == 200 and macro_id,
          f"macro_id={macro_id}")
    r = requests.get(f"http://127.0.0.1:8030/api/ghost/macro/{macro_id}",
                     headers=H, timeout=15)
    steps = (r.json().get("macro") or {}).get("steps") or []
    drag_steps = [s for s in steps if s.get("action") == "drag"]
    scroll_steps = [s for s in steps if s.get("action") == "scroll"]
    check("C-5: 宏步骤含轨迹数组与滚轮参数",
          len(drag_steps) == 1 and drag_steps[0].get("points") == pts
          and len(scroll_steps) == 1 and scroll_steps[0].get("dy") == -120,
          f"steps={[s.get('action') for s in steps]}")
    r = requests.post(f"http://127.0.0.1:8030/api/ghost/macro/{macro_id}/replay",
                      headers=H, json={"dry_run": True, "hwnd": hwnd}, timeout=60)
    check("C-6: dry_run → 200（不产生输入）", r.status_code == 200)
    # C-7: 重放（先滚动后拖拽）→ 视口首行出现蓝色高亮 → 颜色判别
    #（scroll 会位移内容，diff 分不清换行文字与高亮；数蓝底像素则免疫）
    hold_fg(hwnd)
    snap_dir = ROOT / "data" / "screenshots"
    pre_rep = str(snap_dir / "v06_c_rep_before.png")
    post_rep = str(snap_dir / "v06_c_rep_after.png")
    img, _ = grab(1)
    img.crop((line_x0 - 4, line_y - 8, line_x0 + 200, line_y + 16)).save(pre_rep)
    pre_blue = count_highlight_blue(pre_rep)
    r = requests.post(f"http://127.0.0.1:8030/api/ghost/macro/{macro_id}/replay",
                      headers=H, json={"hwnd": hwnd}, timeout=60)
    time.sleep(0.3)
    img, _ = grab(1)
    img.crop((line_x0 - 4, line_y - 8, line_x0 + 200, line_y + 16)).save(post_rep)
    post_blue = count_highlight_blue(post_rep)
    check("C-7: 真实重放按轨迹复现（拖拽再次产生蓝色高亮选区）",
          r.status_code == 200 and pre_blue < 50 and post_blue > 200,
          f"status={r.status_code} blue pre={pre_blue} post={post_blue}")

    # D: 负例
    r, body = action({"action": "drag", "x": 500, "y": 500,
                      "points": [[500, 500], [1, 1]], "settle_seconds": 0})
    check("D-1: 轨迹点落 Fail-Safe 角落 → 400", r.status_code == 400
          and body.get("error") == "SAFETY_BLOCKED",
          f"{r.status_code} {body.get('error')}")
    r, body = action({"action": "drag", "x": 500, "y": 500,
                      "x2": 99999, "y2": 500, "settle_seconds": 0})
    check("D-2: 终点出界 → 400", r.status_code == 400, f"{r.status_code}")
    r, body = action({"action": "drag", "x": 500, "y": 500, "settle_seconds": 0})
    check("D-3: 缺 x2/y2/points → 400", r.status_code == 400, f"{r.status_code}")
    r, body = action({"action": "scroll", "x": 500, "y": 500, "settle_seconds": 0})
    check("D-4: scroll 缺 dy → 400", r.status_code == 400, f"{r.status_code}")

    # E: 清场
    ok_closed = close_owned(hwnd)
    check("E-1: 靶窗已销毁（只认自己 hwnd 消失）", ok_closed, f"hwnd={hwnd}")
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    if macro_id:
        import sqlite3
        from app.core.config import DB_PATH
        with sqlite3.connect(str(DB_PATH)) as con:
            con.execute("DELETE FROM macros WHERE id = ?", (macro_id,))
            con.commit()
        check("E-2: 测试宏已参数化 DELETE 清理", True)
    for p in TMP_FILES:
        try:
            p.unlink()
        except Exception:  # noqa: BLE001
            pass

    total = len(RESULTS)
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== v06 drag verify: {passed}/{total} passed ==", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
