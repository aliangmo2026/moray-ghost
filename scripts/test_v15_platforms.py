"""v0.15 阶段二验收：跨平台驱动抽象（app/platforms/）。

覆盖三类证据：
1. 真实 Windows 冒烟：经 platforms 门面（get_screen/get_input）抓屏、枚举、
   编码、光标往返——证明迁入后的 Windows 真身行为不退化；
2. shim 同一性：app.core.screen_mss / app.core.backend 的每个导出名与
   app.platforms.windows 里是**同一个对象**（is 判定）——单一模块状态
   （BACKEND 单例、capture 线程、修剪计数器）不分裂，旧引用零回归；
3. Mock 骨架：monkeypatch sys.platform → darwin/linux 能动态加载骨架并
   在调用时 NotImplementedError（绝不假装能跑）；未知平台 → PlatformError。

运行（Windows 桌面解锁态，本测试有轻量物理输入：光标往返）：
    .venv/Scripts/python scripts/test_v15_platforms.py
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    tag = "PASS" if cond else "FAIL"
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
    line = f"[{tag}] {name}"
    if detail and not cond:
        line += " -- " + str(detail)
    print(line)


def main() -> int:
    import app.platforms as platforms
    from app.platforms import BaseInput, BaseScreen, PlatformError

    # ---------------- U1: 平台识别 ----------------
    try:
        name = platforms.platform_name()
        check("U1 平台名=windows", name == "windows", f"got {name!r}")
    except Exception as exc:  # noqa: BLE001
        check("U1 平台名=windows", False, f"{type(exc).__name__}: {exc}")

    # ---------------- U2: 抽象类不可直接实例化 ----------------
    try:
        BaseScreen()  # type: ignore[abstract]
        check("U2a BaseScreen 不可实例化", False, "instantiated!")
    except TypeError:
        check("U2a BaseScreen 不可实例化", True)
    try:
        BaseInput()  # type: ignore[abstract]
        check("U2b BaseInput 不可实例化", False, "instantiated!")
    except TypeError:
        check("U2b BaseInput 不可实例化", True)

    # ---------------- U3: 门面单例 + 类型 ----------------
    platforms.reset_cache()
    scr = platforms.get_screen()
    inp = platforms.get_input()
    check("U3a get_screen 单例", platforms.get_screen() is scr)
    check("U3b get_input 单例", platforms.get_input() is inp)
    check("U3c screen 是 BaseScreen", isinstance(scr, BaseScreen),
          type(scr).__name__)
    check("U3d input 是 BaseInput", isinstance(inp, BaseInput),
          type(inp).__name__)

    # ---------------- U4: shim 同一性（零回归核心证据） ----------------
    from app.core import backend, screen_mss
    from app.platforms import windows as w
    pairs = [
        ("screen_mss.grab", screen_mss.grab, w.grab),
        ("screen_mss.monitors", screen_mss.monitors, w.monitors),
        ("screen_mss.monitor_rect", screen_mss.monitor_rect, w.monitor_rect),
        ("screen_mss.monitor_at", screen_mss.monitor_at, w.monitor_at),
        ("screen_mss.virtual_screen", screen_mss.virtual_screen, w.virtual_screen),
        ("screen_mss.dpi_status", screen_mss.dpi_status, w.dpi_status),
        ("screen_mss.grab_health", screen_mss.grab_health, w.grab_health),
        ("screen_mss.encode", screen_mss.encode, w.encode),
        ("screen_mss.save_bytes", screen_mss.save_bytes, w.save_bytes),
        ("screen_mss.save_audit", screen_mss.save_audit, w.save_audit),
        ("screen_mss.b64_of", screen_mss.b64_of, w.b64_of),
        ("screen_mss.new_shot_path", screen_mss.new_shot_path, w.new_shot_path),
        ("backend.BACKEND", backend.BACKEND, w.BACKEND),
        ("backend.position", backend.position, w.position),
        ("backend.move_to", backend.move_to, w.move_to),
        ("backend.click", backend.click, w.click),
        ("backend.mouse_down", backend.mouse_down, w.mouse_down),
        ("backend.mouse_up", backend.mouse_up, w.mouse_up),
        ("backend.scroll", backend.scroll, w.scroll),
        ("backend.write_text", backend.write_text, w.write_text),
        ("backend.hotkey", backend.hotkey, w.hotkey),
        ("backend.key_names", backend.key_names, w.key_names),
        ("backend.pressed_modifiers", backend.pressed_modifiers, w.pressed_modifiers),
        ("backend.point_root", backend.point_root, w.point_root),
        ("backend.foreground_hwnd", backend.foreground_hwnd, w.foreground_hwnd),
        ("backend.release_all_modifiers", backend.release_all_modifiers,
         w.release_all_modifiers),
        ("backend.release_pointer_buttons", backend.release_pointer_buttons,
         w.release_pointer_buttons),
        ("backend._mouse_event_absolute", backend._mouse_event_absolute,
         w._mouse_event_absolute),
        ("backend._mouse_event_relative", backend._mouse_event_relative,
         w._mouse_event_relative),
        ("backend.InputBackend", backend.InputBackend, w.InputBackend),
        ("backend.PyAutoGUIBackend", backend.PyAutoGUIBackend, w.PyAutoGUIBackend),
        ("backend.FailSafeException", backend.FailSafeException, w.FailSafeException),
        ("backend.pyautogui", backend.pyautogui, w.pyautogui),
        ("screen_mss.DPI_AWARE", screen_mss.DPI_AWARE, w.DPI_AWARE),
        ("screen_mss.DPI_MODE", screen_mss.DPI_MODE, w.DPI_MODE),
    ]
    bad = [n for n, a, b in pairs if a is not b]
    check("U4 shim 全量同一性 (is)", not bad, f"split objects: {bad}")

    # ---------------- U5: 真实 Windows 冒烟（经平台门面） ----------------
    try:
        img, origin = scr.grab(1, timeout=8.0)
        check("U5a 门面抓屏", img.width > 0 and img.height > 0,
              f"size={img.width}x{img.height} origin={origin}")
    except Exception as exc:  # noqa: BLE001
        check("U5a 门面抓屏", False, f"{type(exc).__name__}: {exc}")
        img = None
    try:
        mons = scr.monitors()
        v = scr.virtual_screen()
        ok = (len(mons) >= 2 and mons[0]["width"] == v["width"]
              and mons[0]["height"] == v["height"])
        check("U5b 枚举+虚拟屏一致", ok, f"mons={mons} v={v}")
    except Exception as exc:  # noqa: BLE001
        check("U5b 枚举+虚拟屏一致", False, f"{type(exc).__name__}: {exc}")
    try:
        st = scr.dpi_status()
        check("U5c dpi_status 档位在册", st.get("mode") is not None, st)
    except Exception as exc:  # noqa: BLE001
        check("U5c dpi_status 档位在册", False, f"{type(exc).__name__}: {exc}")
    if img is not None:
        try:
            data = scr.encode(img, "jpeg", 60, 0.5)
            b64 = scr.b64_of(img, "jpeg", 60, 0.5)
            check("U5d 编码+jpeg+b64", data[:2] == b"\xff\xd8" and len(b64) > 100,
                  f"jpeg_len={len(data)} b64_len={len(b64)}")
        except Exception as exc:  # noqa: BLE001
            check("U5d 编码+jpeg+b64", False, f"{type(exc).__name__}: {exc}")
    # 光标往返（轻量物理输入；重量级坐标回归在 test_v14_dpi）
    try:
        import pyautogui
        x0, y0 = inp.position()
        scr_m = scr.monitor_rect(1)
        tx = scr_m["left"] + scr_m["width"] // 2
        ty = scr_m["top"] + scr_m["height"] // 2
        inp.move_to(tx, ty)
        x1, y1 = inp.position()
        inp.move_to(x0, y0)  # 归位
        ok = abs(x1 - tx) <= 2 and abs(y1 - ty) <= 2
        check("U5e 门面 move_to 往返(≤2px)", ok,
              f"target=({tx},{ty}) got=({x1},{y1})")
    except Exception as exc:  # noqa: BLE001
        check("U5e 门面 move_to 往返(≤2px)", False, f"{type(exc).__name__}: {exc}")
        try:
            pyautogui.moveTo(x0, y0, duration=0)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- U6: Mock 骨架动态加载（darwin/linux） ----------------
    real_platform = sys.platform
    try:
        for fake, modname in (("darwin", "macos"), ("linux", "linux")):
            sys.platform = fake
            platforms.reset_cache()
            check(f"U6-{fake} platform_name", platforms.platform_name() == modname,
                  platforms.platform_name())
            s2 = platforms.get_screen()
            i2 = platforms.get_input()
            try:
                s2.grab(1)
                check(f"U6-{fake} skeleton grab NotImplementedError", False,
                      "no exception")
            except NotImplementedError:
                check(f"U6-{fake} skeleton grab NotImplementedError", True)
            except Exception as exc:  # noqa: BLE001
                check(f"U6-{fake} skeleton grab NotImplementedError", False,
                      f"{type(exc).__name__}: {exc}")
            try:
                i2.click()
                check(f"U6-{fake} skeleton click NotImplementedError", False,
                      "no exception")
            except NotImplementedError:
                check(f"U6-{fake} skeleton click NotImplementedError", True)
            except Exception as exc:  # noqa: BLE001
                check(f"U6-{fake} skeleton click NotImplementedError", False,
                      f"{type(exc).__name__}: {exc}")

        # U7: 未知平台 → PlatformError（绝不静默降级）
        sys.platform = "freebsd7"
        platforms.reset_cache()
        try:
            platforms.get_screen()
            check("U7 未知平台 PlatformError", False, "no exception")
        except PlatformError:
            check("U7 未知平台 PlatformError", True)
        except Exception as exc:  # noqa: BLE001
            check("U7 未知平台 PlatformError", False, f"{type(exc).__name__}: {exc}")
    finally:
        sys.platform = real_platform
        platforms.reset_cache()  # 回到 win32 真身单例

    # ---------------- U8: environment 快照回显平台 ----------------
    try:
        from app.core import environment
        snap = environment.snapshot(probe_grab=False)
        check("U8 snapshot.platform=windows", snap.get("platform") == "windows",
              snap.get("platform"))
    except Exception as exc:  # noqa: BLE001
        check("U8 snapshot.platform=windows", False,
              f"{type(exc).__name__}: {exc}")

    total = _PASS + _FAIL
    print(f"\n==== v15_platforms: {_PASS}/{total} PASS, {_FAIL} FAIL ====")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print(f"\n==== v15_platforms: {_PASS}/{_PASS + _FAIL} PASS "
              f"(aborted, {_FAIL} FAIL) ====")
        sys.exit(1)
