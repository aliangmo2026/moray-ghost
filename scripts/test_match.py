"""宏特征匹配验证（任务 3 第一步）——四场景 + 性能。

坐标系自检铁律：全程物理像素、内部零缩放；crop.size 必须断言；匹配中心与
参考坐标误差必须 ≤2px，超限说明某处发生隐式缩放（或窗口位移竞态），立即失败。

场景（实机桌面是活的，A/B 都带重试；重试耗尽才判 FAIL）：
  A 自我匹配（鼠标处裁 64x64，验证算法基线，score>0.99，误差≤2px）
  B 真实 UI（打开记事本，裁"文件"按钮区域，二次抓屏后匹配；窗口位置稳定后才
    裁剪，且两次抓屏之间复核窗口未移动，规避开窗动画竞态；找不到记事本则
    明确报"未测试真实 UI"）
  C 负样本（200x200 纯白块：零方差模板应被防线判 not_found；附加随机噪声块）
  D 边界 clamp（裁剪点距边缘 <32px 时不越界、中心符合 clamp 预期）

用法：.venv\\Scripts\\python scripts\\test_match.py
"""
from __future__ import annotations

import base64
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

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pyautogui  # noqa: E402

from app.core.macro_matcher import crop_around, find  # noqa: E402  生产匹配代码
from app.core.screen_mss import grab  # noqa: E402  生产抓屏代码

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {name}"
    if detail:
        line += f"  | {detail}"
    print(line, flush=True)
    return ok


def res_detail(res: dict) -> str:
    """对 matched/not_found 两种返回都安全的详情字符串。"""
    if res.get("status") == "matched":
        return f"score={res['score']:.4f} center={res['center']}"
    return (f"status={res.get('status')} "
            f"best_score={float(res.get('best_score') or 0.0):.4f} "
            f"reason={res.get('reason')}")


def png_size(b64: str) -> tuple:
    arr = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
    im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return im.shape[1], im.shape[0]  # (w, h)


def main() -> int:
    ok_all = True

    # ---------- 场景 A：自我匹配（基线，带重试 + 平坦区回退） ----------
    res_a = None
    crop_a = None
    center_a = None
    img2 = None
    a_ms = 0.0
    used_point = None
    used_fallback = False
    mouse0 = pyautogui.position()
    for attempt in range(8):
        img1, _ = grab(1)
        mouse = pyautogui.position()
        # 鼠标可能正停在纯色区（std≈0 的退化模板，会被防线正确拒绝）——依次回退
        # 到预设候选点取第一块有纹理（std>2）的区域，A 的目的（屏幕自匹配基线）不变。
        candidates = [(mouse.x, mouse.y), (img1.width // 2, img1.height // 2),
                      (img1.width // 3, img1.height // 3),
                      (img1.width * 2 // 3, img1.height * 2 // 3)]
        chosen = None
        for idx0, (px, py) in enumerate(candidates):
            c_tmp, c_center = crop_around(img1, px, py, 64)
            arr_tmp = cv2.imdecode(
                np.frombuffer(base64.b64decode(c_tmp), dtype=np.uint8), cv2.IMREAD_COLOR)
            if float(arr_tmp.std()) > 2.0:
                chosen = (c_tmp, c_center, (px, py), idx0)
                break
        if chosen is None:
            print(f"  (A attempt {attempt}: 鼠标处与候选点均为纯色区，重试)", flush=True)
            time.sleep(0.3)
            continue
        crop_a, center_a, used_point, used_idx = chosen
        used_fallback = used_idx > 0
        img2, _ = grab(1)
        t0 = time.perf_counter()
        res_a = find(img2, crop_a, 0.80)
        a_ms = (time.perf_counter() - t0) * 1000
        if (res_a.get("status") == "matched" and res_a["score"] > 0.99
                and max(abs(res_a["center"][0] - center_a[0]),
                        abs(res_a["center"][1] - center_a[1])) <= 2):
            break
        print(f"  (A attempt {attempt}: {res_detail(res_a)} -> 区域在动画，重试)", flush=True)
        res_a = None
        time.sleep(0.2)
    if res_a is None:
        ok_all &= check("A: 自我匹配（8 次尝试全失败）", False,
                        "候选区域持续动画或全为纯色，需人工复核")
    else:
        w_a, h_a = png_size(crop_a)
        ok_all &= check("A: crop.size == (64,64)", (w_a, h_a) == (64, 64),
                        f"size={w_a}x{h_a} clamped_center={center_a}")
        err_a = max(abs(res_a["center"][0] - center_a[0]),
                    abs(res_a["center"][1] - center_a[1]))
        pt_txt = f"point={used_point}" + ("（鼠标处为纯色区，已回退候选点）" if used_fallback else "（鼠标处）")
        ok_all &= check("A: score>0.99 & 中心误差≤2px（vs 裁剪中心）",
                        res_a["score"] > 0.99 and err_a <= 2,
                        f"score={res_a['score']:.4f} center={res_a['center']} "
                        f"err={err_a}px mouse=({mouse0.x},{mouse0.y}) {pt_txt} {a_ms:.0f}ms")

    # ---------- 场景 B：真实 UI（记事本"文件"按钮） ----------
    hwnds_before = {w._hWnd for w in pyautogui.getAllWindows()}
    subprocess.Popen(["notepad.exe"])
    win = None
    for _ in range(60):
        time.sleep(0.25)
        cands = [w for w in pyautogui.getAllWindows()
                 if w._hWnd not in hwnds_before
                 and ("记事本" in w.title or "notepad" in w.title.lower())]
        if cands:
            win = cands[0]
            break
    if win is None:
        ok_all &= check("B: 未测试真实 UI（未找到新记事本窗口，跳过，需人工复核）",
                        False, "no new notepad window")
    else:
        # 等开窗动画结束：连续 3 次采样（~0.75s）窗口 box 不变才算稳定
        prev_box = None
        stable = 0
        for _ in range(60):
            cur = win.box
            if cur == prev_box:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            prev_box = cur
            time.sleep(0.25)
        time.sleep(0.5)

        res_b = None
        anchor = None
        crop_b = None
        tw_b = th_b = 0
        for _attempt in range(6):
            box1 = win.box
            img_b1, _ = grab(1)
            box2 = win.box
            if box1 != box2:
                print(f"  (B attempt {_attempt}: window moved during grab1, retrying)",
                      flush=True)
                continue
            # Win11 新记事本（深色主题）："文件"按钮相对窗口左上角约 (12,62) 96x48
            dx, dy, tw_b, th_b = (12, 62, 96, 48)
            cx = box1[0] + dx + tw_b // 2
            cy = box1[1] + dy + th_b // 2
            crop_b, center_b = crop_around(img_b1, cx, cy, (tw_b, th_b))
            img_b2, _ = grab(1)
            box3 = win.box
            if box2 != box3:
                print(f"  (B attempt {_attempt}: window moved during grab2, retrying)",
                      flush=True)
                continue
            res_b = find(img_b2, crop_b, 0.80)
            anchor = center_b
            if res_b.get("status") == "matched":
                err_b = max(abs(res_b["center"][0] - center_b[0]),
                            abs(res_b["center"][1] - center_b[1]))
                if err_b > 2:
                    print(f"  (B attempt {_attempt}: matched but shifted {err_b}px "
                          f"-> window race, retrying)", flush=True)
                    continue
            else:
                print(f"  (B attempt {_attempt}: {res_detail(res_b)} -> 画面在变化，重试)",
                      flush=True)
                res_b = None
                continue
            break

        if res_b is None:
            ok_all &= check("B: 真实 UI 匹配", False,
                            "6 次尝试未取到稳定的匹配对（实机画面持续变化/未测试真实 UI）")
        else:
            wb_b, hb_b = png_size(crop_b)
            ok_all &= check("B: template size 自适应裁剪不越界",
                            (wb_b, hb_b) == (tw_b, th_b), f"size={wb_b}x{hb_b}")
            err_b = max(abs(res_b["center"][0] - anchor[0]),
                        abs(res_b["center"][1] - anchor[1]))
            ok_all &= check("B: 真实 UI（记事本-文件按钮）matched & 中心误差≤2px",
                            res_b.get("status") == "matched"
                            and res_b.get("score", 0) >= 0.80 and err_b <= 2,
                            f"{res_detail(res_b)} anchor={anchor} err={err_b}px")
            dbg = ROOT / "data" / "screenshots" / "debug_template_b.png"
            dbg.write_bytes(base64.b64decode(crop_b))
            print(f"  (B 模板已存 {dbg.name} 供目视核对)", flush=True)
        win.close()
        time.sleep(1.0)
        gone = not any(w._hWnd == win._hWnd for w in pyautogui.getAllWindows())
        ok_all &= check("B: 记事本已关闭（只关本次新开的窗口）", gone, f"hwnd={win._hWnd}")

    # ---------- 场景 C：负样本（阈值有效性 + 退化模板防线） ----------
    if img2 is None:
        img2, _ = grab(1)
    white = np.full((200, 200, 3), 255, dtype=np.uint8)
    ok_enc, buf_c = cv2.imencode(".png", white)
    crop_c = base64.b64encode(buf_c.tobytes()).decode("ascii") if ok_enc else ""
    res_c = find(img2, crop_c, 0.80)
    c_score = res_c.get("best_score", res_c.get("score"))
    ok_all &= check("C: 纯白块（零方差模板）不产生伪匹配 & 分数<0.5",
                    res_c.get("status") == "not_found" and float(c_score or 0.0) < 0.5,
                    f"{res_detail(res_c)}")
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 256, size=(64, 64, 3), dtype=np.uint8)
    ok_enc2, buf_c2 = cv2.imencode(".png", noise)
    crop_c2 = base64.b64encode(buf_c2.tobytes()).decode("ascii") if ok_enc2 else ""
    res_c2 = find(img2, crop_c2, 0.80)
    ok_all &= check("C+: 64x64 随机噪声块 not_found（真实低分证据）",
                    res_c2.get("status") == "not_found",
                    f"best_score={res_c2.get('best_score'):.4f}")

    # ---------- 场景 D：边界 clamp ----------
    img_d = img2
    for ex, ey in [(10, 800), (5, 5), (img_d.width - 5, img_d.height - 5)]:
        crop_d, center_d = crop_around(img_d, ex, ey, 64)
        wd, hd = png_size(crop_d)
        exp_left = max(0, min(ex - 32, img_d.width - 64))
        exp_top = max(0, min(ey - 32, img_d.height - 64))
        exp_center = [exp_left + 32, exp_top + 32]
        ok_all &= check(f"D: 边界clamp @({ex},{ey}) size==64x64 & center 符合预期",
                        (wd, hd) == (64, 64) and center_d == exp_center,
                        f"center={center_d} expect={exp_center}")

    # ---------- 性能：纯 matchTemplate vs 全流程 find() ----------
    tpl = cv2.imdecode(np.frombuffer(base64.b64decode(crop_a), dtype=np.uint8),
                       cv2.IMREAD_COLOR)
    screen = cv2.cvtColor(np.ascontiguousarray(np.asarray(img_d.convert("RGB"))),
                          cv2.COLOR_RGB2BGR)
    raw_times = []
    for _ in range(5):
        t0 = time.perf_counter()
        cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
        raw_times.append((time.perf_counter() - t0) * 1000)
    raw_times.sort()
    full_times = []
    for _ in range(5):
        t0 = time.perf_counter()
        find(img_d, crop_a, 0.80)
        full_times.append((time.perf_counter() - t0) * 1000)
    full_times.sort()
    print(f"perf: 纯 matchTemplate {screen.shape[1]}x{screen.shape[0]} 64x64模板 "
          f"min={raw_times[0]:.0f}ms median={raw_times[2]:.0f}ms max={raw_times[-1]:.0f}ms",
          flush=True)
    print(f"perf: 全流程 find()（含 PIL→numpy→BGR 转换 + 匹配） "
          f"min={full_times[0]:.0f}ms median={full_times[2]:.0f}ms max={full_times[-1]:.0f}ms",
          flush=True)

    passed = sum(1 for o in RESULTS if o)
    print(f"== test_match: {passed}/{len(RESULTS)} passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
