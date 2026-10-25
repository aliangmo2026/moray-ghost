"""CDP 浏览器接管（v0.6 M2）：playwright connect_over_cdp 连本地 Edge/Chrome。

铁律：
- 只连 127.0.0.1/localhost 调试端口（GHOST_BROWSER_CDP_URL），绝不自动拉起浏览器、
  绝不下载捆绑浏览器（不执行 playwright install 的浏览器步骤）；
- 单一 browser 工作线程持有 sync playwright（与 screen_mss 同款收口模式：sync API
  非线程安全，所有 page 操作必须回到持有线程执行）；连接断开时下个任务自愈重连；
- 元素定位多候选绝不猜：count==0 → ELEMENT_NOT_FOUND，count>1 未给 nth →
  ELEMENT_AMBIGUOUS（把候选数报回去让主仓消歧）；
- 冻结门禁 / input_audit(channel=cdp) / ACTION_LOCK 由路由层统一执行，本模块只做
  CDP 语义的"怎么做、做了没有"。
"""
from __future__ import annotations

import base64
import logging
import queue
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .config import BROWSER_CDP_URL
from . import screen_mss

LOG = logging.getLogger("moray-ghost.browser_driver")

_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")

_connect_q: "queue.Queue[Any]" = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False

# 单次任务的整体超时（含 connect 与操作）
OP_TIMEOUT_S = 30.0


class BrowserError(Exception):
    """带 HTTP 语义的 CDP 操作失败；data 会并入响应体。"""

    def __init__(self, status: int, error: str, detail: str, **data: Any):
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail
        self.data = data


def cdp_url_ok() -> bool:
    """配置的 CDP 地址是否合法（仅 loopback http）。"""
    try:
        p = urlparse(BROWSER_CDP_URL)
        return p.scheme == "http" and (p.hostname or "") in _ALLOWED_HOSTS
    except Exception:  # noqa: BLE001
        return False


# ---------------- 工作线程（sync playwright 唯一持有者） ----------------

class _Driver:
    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None

    def close(self) -> None:
        for obj in (self._browser, self._pw):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        self._browser = None
        self._pw = None

    def connect(self) -> None:
        self.close()
        if not cdp_url_ok():
            raise BrowserError(400, "BROWSER_URL_INVALID",
                               "GHOST_BROWSER_CDP_URL must be http://127.0.0.1:<port> "
                               "(loopback only), got " + repr(BROWSER_CDP_URL))
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError(503, "BROWSER_DEPS_MISSING",
                               "playwright is not installed: " + str(exc)) from exc
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.connect_over_cdp(BROWSER_CDP_URL,
                                                               timeout=5000)
        except Exception as exc:  # noqa: BLE001  连接失败=环境问题，语义化 503
            self.close()
            raise BrowserError(503, "BROWSER_NOT_CONNECTED",
                               "cannot connect to CDP at " + BROWSER_CDP_URL
                               + " (start the browser with --remote-debugging-port): "
                               + type(exc).__name__ + ": " + str(exc)) from exc

    def ensure(self) -> Any:
        if self._browser is None:
            self.connect()
        return self._browser

    def context(self) -> Any:
        browser = self.ensure()
        ctxs = browser.contexts
        if not ctxs:
            raise BrowserError(503, "BROWSER_NOT_CONNECTED",
                               "connected browser has no contexts")
        return ctxs[0]

    def pages(self) -> List[Any]:
        return list(self.context().pages)

    def pick_page(self, tab: Optional[Dict[str, Any]]) -> Any:
        pages = self.pages()
        if not pages:
            raise BrowserError(409, "TAB_NOT_FOUND", "browser has no open tabs")
        if tab and tab.get("url_contains"):
            needle = str(tab["url_contains"])
            matched = [p for p in pages if needle in (p.url or "")]
            if not matched:
                raise BrowserError(409, "TAB_NOT_FOUND",
                                   "no tab url contains " + repr(needle),
                                   tabs=[(p.url or "") for p in pages])
            return matched[0]
        return pages[-1]  # 缺省：最后面的 tab（近似活跃 tab）

    def tabs_info(self) -> List[Dict[str, Any]]:
        out = []
        for i, p in enumerate(self.pages()):
            try:
                out.append({"index": i, "url": p.url, "title": p.title()})
            except Exception:  # noqa: BLE001  刚关掉的 tab
                out.append({"index": i, "url": None, "title": None})
        return out

    def _locator(self, page: Any, sel: Optional[Dict[str, Any]]):
        if not isinstance(sel, dict) or not sel.get("value"):
            raise BrowserError(400, "BAD_SELECTOR",
                               "selector {by,value} is required for this op")
        by = str(sel.get("by") or "css").lower()
        value = str(sel.get("value"))
        nth = sel.get("nth")
        if by == "css":
            loc = page.locator(value)
        elif by == "role":
            loc = page.get_by_role(value)
        elif by == "text":
            loc = page.get_by_text(value, exact=bool(sel.get("exact")))
        elif by == "placeholder":
            loc = page.get_by_placeholder(value)
        elif by == "label":
            loc = page.get_by_label(value)
        elif by == "testid":
            loc = page.get_by_test_id(value)
        else:
            raise BrowserError(400, "BAD_SELECTOR",
                               "unknown selector by=" + repr(by)
                               + " (valid: css/role/text/placeholder/label/testid)")
        count = loc.count()
        if count == 0:
            raise BrowserError(409, "ELEMENT_NOT_FOUND",
                               "no element matches " + repr(by) + "=" + repr(value))
        if nth is None and count > 1:
            raise BrowserError(409, "ELEMENT_AMBIGUOUS",
                               str(count) + " elements match " + repr(by) + "="
                               + repr(value) + "; pass selector.nth to disambiguate",
                               candidates=count)
        if nth is not None:
            nth = int(nth)
            if nth < 0 or nth >= count:
                raise BrowserError(400, "BAD_SELECTOR",
                                   "selector.nth " + str(nth) + " out of range 0.."
                                   + str(count - 1))
            loc = loc.nth(nth)
        return loc, count

    def shot_b64(self, page: Any) -> str:
        data = page.screenshot(type="png")
        return base64.b64encode(data).decode("ascii")

    def shot_save(self, page: Any, purpose: str) -> Optional[str]:
        try:
            data = page.screenshot(type="png")
            return str(screen_mss.save_bytes(data, purpose, "png"))
        except Exception as exc:  # noqa: BLE001  审计图失败不掩盖主错误
            LOG.warning("browser audit shot failed: %s", exc)
            return None

    def run_op(self, body: Dict[str, Any]) -> Dict[str, Any]:
        op = str(body.get("op") or "").lower()
        try:
            return self._run_op_once(op, body)
        except BrowserError:
            raise
        except Exception as exc:  # noqa: BLE001  连接断开：自愈重连一次
            msg = str(exc)
            if ("Target closed" in msg or "has been closed" in msg
                    or "Connection closed" in msg or "not connected" in msg):
                LOG.warning("browser connection lost, reconnecting: %s", exc)
                self.close()
                try:
                    return self._run_op_once(op, body)
                except BrowserError:
                    raise
                except Exception as exc2:  # noqa: BLE001
                    raise BrowserError(503, "BROWSER_NOT_CONNECTED",
                                       type(exc2).__name__ + ": " + str(exc2)) from exc2
            raise BrowserError(500, "INTERNAL_ERROR",
                               type(exc).__name__ + ": " + str(exc))

    def _run_op_once(self, op: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if op == "tabs":
            return {"ok": True, "op": op, "tabs": self.tabs_info()}
        page = self.pick_page(body.get("tab"))
        out: Dict[str, Any] = {"ok": True, "op": op,
                               "tab_url": page.url, "tab_title": page.title()}
        if op == "close_tab":
            out["closed_url"] = page.url
            page.close()
            return out
        before_path = self.shot_save(page, "browser_before")
        out["before_screenshot_path"] = before_path
        out["before_image_b64"] = self.shot_b64(page)
        if op in ("click", "dblclick", "hover", "fill"):
            loc, count = self._locator(page, body.get("selector"))
            out["selector_count"] = count
            loc.scroll_into_view_if_needed(timeout=5000)
            box = loc.bounding_box()
            out["element_box"] = ({"x": round(box["x"]), "y": round(box["y"]),
                                   "width": round(box["width"]),
                                   "height": round(box["height"])}
                                  if box else None)
            if op == "click":
                loc.click(timeout=5000)
            elif op == "dblclick":
                loc.dblclick(timeout=5000)
            elif op == "hover":
                loc.hover(timeout=5000)
            elif op == "fill":
                if body.get("text") is None:
                    raise BrowserError(400, "BAD_REQUEST", "fill requires text")
                loc.fill(str(body.get("text")), timeout=5000)
        elif op == "press":
            if not body.get("text"):
                raise BrowserError(400, "BAD_REQUEST",
                                   "press requires text (a key like 'Enter')")
            if body.get("selector"):  # 先聚焦目标元素再按键
                loc, _count = self._locator(page, body.get("selector"))
                loc.click(timeout=5000)
            page.keyboard.press(str(body.get("text")))
        elif op == "scroll":
            dx = int(body.get("dx") or 0)
            dy = int(body.get("dy") or 0)
            if dx == 0 and dy == 0:
                raise BrowserError(400, "BAD_REQUEST", "scroll requires dx or dy")
            if body.get("selector"):
                loc, _count = self._locator(page, body.get("selector"))
                loc.hover(timeout=5000)
            page.mouse.wheel(dx, dy)
            out["scroll"] = {"dx": dx, "dy": dy}
        elif op == "drag":
            raw = body.get("points")
            if not isinstance(raw, list) or len(raw) < 2:
                raise BrowserError(400, "BAD_REQUEST",
                                   "drag requires points [[x,y],...] (page CSS px)")
            pts = [(float(p[0]), float(p[1])) for p in raw]
            page.mouse.move(pts[0][0], pts[0][1])
            page.mouse.down()
            try:
                for px, py in pts[1:]:
                    page.mouse.move(px, py, steps=2)
            finally:
                page.mouse.up()
            out["drag"] = {"points": len(pts)}
        else:
            raise BrowserError(400, "BAD_REQUEST",
                               "unknown op " + repr(op) + " (valid: click/dblclick/"
                               "hover/fill/press/scroll/drag/tabs/close_tab)")
        out["after_screenshot_path"] = self.shot_save(page, "browser_after")
        out["after_image_b64"] = self.shot_b64(page)
        return out


_driver = _Driver()


def _worker() -> None:
    while True:
        item = _connect_q.get()
        if item is None:
            break
        op, body, box = item
        try:
            box["out"] = _driver.run_op(body)
        except Exception as exc:  # noqa: BLE001  交给调用方翻译成 HTTP
            box["exc"] = exc
        finally:
            _connect_q.task_done()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, name="moray-ghost-browser",
                         daemon=True).start()
        _worker_started = True


def run_op(body: Dict[str, Any]) -> Dict[str, Any]:
    """在 browser 工作线程执行一个 CDP 操作（阻塞至完成或超时）。"""
    _ensure_worker()
    box: Dict[str, Any] = {}
    _connect_q.put((str(body.get("op") or ""), body, box))
    deadline = OP_TIMEOUT_S
    import time
    end = time.monotonic() + deadline
    while "exc" not in box and "out" not in box:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise BrowserError(504, "BROWSER_TIMEOUT",
                               "CDP op did not finish within "
                               + str(int(deadline)) + "s")
        time.sleep(min(0.02, remaining))
    if "exc" in box:
        raise box["exc"]
    return box["out"]


def tabs() -> Dict[str, Any]:
    """列出已打开的 tab（只读）。"""
    return run_op({"op": "tabs"})


def shutdown() -> None:
    _driver.close()
