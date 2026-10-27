"""军工级安全红线（v0.12 绝对防御）：窗口黑名单 / 键盘黑名单 / 能力令牌 +
动态视觉门禁（Ghost Guard）。

前三道门禁在 controller 宏重放、任务步骤、直接 /execute_action 的唯一咽喉上
强制，无一例外。本模块只做判定与解析（纯函数 + win32 只读查询），不产生任何
输入；HTTP 语义错误由 controller 的 ActionError 子类承载。

1. 窗口黑名单：目标窗口进程 ∈ HIGH_RISK_PROCESSES，或标题含高危字样 → 拒绝
   任何点击与输入（controller 侧 403 HIGH_RISK_WINDOW）。
2. 键盘黑名单：系统级热键（win 系 / ctrl+shift+esc / ctrl+alt+delete）与
   破坏性文本（format / shutdown / 格式化 / rm -rf …）→ 403 HIGH_RISK_INPUT。
3. 能力令牌：执行任何会打进窗口的输入前，目标窗口进程必须 ∈
   capabilities.allowed_windows；未携带令牌或进程不在名单 → 403 CAPABILITY_DENIED。
4. 动态视觉门禁（v0.12）：dynamic_guard() 对 OCR/DOM 文本做高危词检测
   （转账/删除/格式化/退出登录…）；controller 点击前只 OCR 落点控件内部，
   命中 → 409 PENDING_APPROVAL（零输入），配置 GHOST_APPROVAL_TOKEN 后
   人工审批令牌才放行。规则与边界见 docs/v0.12-design.md。
5. System1 弹窗规则库（v0.15 快思考）：system1_match() 纯文本/几何判定
   「弹窗特征词 + 关闭类按钮 同窗共现」，供 vl_driver.system1_reflex 在
   任务挂起前本地消解弹窗——本层仍只判定不产生输入，点击走 controller。

实证（本机 Win11 探针，2026-10）：cmd.exe 的可见控制台窗口宿主是
WindowsTerminal.exe（class CASCADIA_HOSTING_WINDOW_CLASS；ConsoleWindowClass
窗口存在但不可见），legacy 宿主是 conhost.exe——三者与 pwsh.exe 一并入黑名单，
否则"目标是 cmd"会因解析到 Terminal 而漏网。

设计取舍（宁可错杀，绝不漏杀）：
- 标题黑名单按子串匹配（UAC/格式化/删除/用户账户控制）：正常业务窗口误伤可由
  主仓改用白名单内窗口或改标题化解；漏杀一次=不可逆破坏。
- \bformat\b 用词边界（"information" 不误伤），但单词 "format" 本身一律拒绝。
- alt+f4 不入键盘黑名单：属应用级关闭（有保存确认），且测试/业务高频正当使用；
  关错窗口的兜底是窗口白名单 + 审计截图，不在本红线范围。
"""
from __future__ import annotations

import hmac
import re
from typing import Any, Dict, Iterable, List, Optional

import psutil
import win32gui
import win32process

from . import config

# ---- 1) 窗口黑名单 -------------------------------------------------------

# 直系红线（用户钦定）：cmd / powershell / regedit。
# 同族补充（本机实证的宿主链，缺一即漏）：pwsh（PowerShell 7）、
# WindowsTerminal（Win11 控制台默认可见宿主）、conhost（legacy 控制台宿主）。
HIGH_RISK_PROCESSES = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "regedit.exe",
    "windowsterminal.exe", "conhost.exe",
})

# 标题黑名单：ASCII 词按 lower() 匹配子串，中文按原样子串。
HIGH_RISK_TITLE_TOKENS = ("uac", "用户账户控制", "格式化", "删除")

# ---- 1b) UAC 提权弹窗 = FATAL 级（v0.7.2，高于普通黑名单） ----------------
# UAC 提权窗口（consent.exe 宿主 / 「用户账户控制」标题族）一旦被点击即可能
# 在安全桌面上放行提权——属于"绝对不可逆灾难"级目标：任何动作直接 FATAL，
# 且 controller 侧先物理释放按键鼠标再拒绝（零输入）。
UAC_FATAL_PROCESSES = frozenset({"consent.exe"})
UAC_FATAL_TITLE_TOKENS = ("用户账户控制", "用户帐户控制", "user account control")


def uac_fatal_reason(info: Optional[Dict[str, Any]]) -> Optional[str]:
    """UAC 提权弹窗判定（FATAL 级）：命中返回理由，放行 None。只读。"""
    if not info:
        return None
    proc = str(info.get("process") or "").strip().lower()
    if proc in UAC_FATAL_PROCESSES:
        return ("UAC elevation prompt process " + repr(info.get("process"))
                + " (FATAL: elevation dialogs must never be clicked)")
    title = str(info.get("title") or "")
    lowered = title.lower()
    for tok in UAC_FATAL_TITLE_TOKENS:
        if tok in lowered:
            return ("UAC elevation window title contains " + repr(tok)
                    + ": " + repr(title)
                    + " (FATAL: elevation dialogs must never be clicked)")
    return None


def window_info(hwnd: int) -> Optional[Dict[str, Any]]:
    """hwnd → {"hwnd","pid","process","title"}；无效窗口 → None。只读，不缓存。"""
    try:
        h = int(hwnd)
        if not h or not win32gui.IsWindow(h):
            return None
        _, pid = win32process.GetWindowThreadProcessId(h)
        try:
            name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001  进程刚退出等竞态：当作不可识别
            name = ""
        return {"hwnd": h, "pid": int(pid), "process": name,
                "title": win32gui.GetWindowText(h)}
    except Exception:  # noqa: BLE001
        return None


def redline_window(info: Optional[Dict[str, Any]]) -> Optional[str]:
    """窗口黑名单判定：命中返回拒绝理由（str），放行返回 None。"""
    if not info:
        return None
    proc = str(info.get("process") or "").strip().lower()
    if proc in HIGH_RISK_PROCESSES:
        return ("window process " + repr(info.get("process"))
                + " is on the high-risk blacklist")
    title = str(info.get("title") or "")
    lowered = title.lower()
    for tok in HIGH_RISK_TITLE_TOKENS:
        if tok in lowered:
            return ("window title contains high-risk token " + repr(tok)
                    + ": " + repr(title))
    return None


# ---- 2) 键盘黑名单 -------------------------------------------------------

# 系统级热键（组合含 win 键一律拒绝；ctrl+shift+esc / ctrl+alt+delete 同类）
_BLOCKED_COMBOS = (
    (frozenset({"ctrl", "shift", "esc"}), "ctrl+shift+esc (task manager)"),
    (frozenset({"ctrl", "alt", "del"}), "ctrl+alt+delete (secure sequence)"),
    (frozenset({"ctrl", "alt", "delete"}), "ctrl+alt+delete (secure sequence)"),
)


def redline_hotkey(keys: List[str]) -> Optional[str]:
    """热键黑名单判定：命中返回拒绝理由，放行 None。keys 为已规范化小写键名。"""
    for k in keys:
        if k.startswith("win"):
            return "system-level hotkey with win modifier is forbidden: " + "+".join(keys)
    keyset = frozenset(keys)
    for need, label in _BLOCKED_COMBOS:
        if need <= keyset:
            return "system-level hotkey is forbidden: " + label
    return None


# 破坏性文本：英文按词边界（\bformat\b 不误伤 information），中文按原样子串。
_DESTRUCTIVE_TEXT: tuple = (
    (re.compile(r"\bformat\b", re.I), "format"),
    (re.compile(r"\bshutdown\b", re.I), "shutdown"),
    (re.compile(r"\bpoweroff\b", re.I), "poweroff"),
    (re.compile(r"\bdel\b\s+/[a-z]*[fs]", re.I), "del /f|/s"),
    (re.compile(r"\brd\b\s+/s", re.I), "rd /s"),
    (re.compile(r"\brm\b[^.\n]*\s-{1,2}[a-z]*r", re.I), "rm -r|-rf"),
    (re.compile(r"\bmkfs\w*", re.I), "mkfs"),
    (re.compile(r"\bdiskpart\b", re.I), "diskpart"),
    (re.compile(r"\breg\s+delete\b", re.I), "reg delete"),
    (re.compile(r"格式化"), "格式化"),
)


def redline_text(text: str) -> Optional[str]:
    """type 文本黑名单判定：命中返回拒绝理由，放行 None。"""
    for pat, label in _DESTRUCTIVE_TEXT:
        if pat.search(text):
            return "destructive string " + repr(label) + " is forbidden in typed text"
    return None


# ---- 3) 能力令牌（最小权限） ---------------------------------------------

CAPABILITIES_REQUIRED_KEYS = ("allowed_windows",)


def parse_capabilities(raw: Any) -> Dict[str, Any]:
    """规范化 capabilities 对象；非法抛 ValueError（调用方转 4xx 语义）。

    结构：{"allowed_windows": ["notepad.exe", ...]}（进程名，大小写不敏感，
    规范化为 lower）；额外键原样保留（前向兼容）。
    """
    if not isinstance(raw, dict):
        raise ValueError(
            'capabilities must be an object like {"allowed_windows": ["notepad.exe"]}')
    allowed = raw.get("allowed_windows")
    if not isinstance(allowed, list):
        raise ValueError("capabilities.allowed_windows must be an array of "
                         "process-name strings (e.g. [\"notepad.exe\"])")
    normed: List[str] = []
    for item in allowed:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("capabilities.allowed_windows entries must be "
                             "non-empty strings, got " + repr(item))
        normed.append(item.strip().lower())
    out = dict(raw)
    out["allowed_windows"] = normed
    return out


def capability_allows(capabilities: Optional[Dict[str, Any]],
                      process_name: str) -> bool:
    """进程名是否在令牌白名单内（大小写不敏感）。无令牌 → False（默认拒绝）。"""
    if not capabilities:
        return False
    allowed = capabilities.get("allowed_windows") or []
    return str(process_name or "").strip().lower() in allowed


def has_input_steps(steps: List[Dict[str, Any]]) -> bool:
    """任务是否包含会打进窗口的输入步骤（action/macro）——有则令牌必须非空。"""
    return any(s.get("kind") in ("action", "macro") for s in steps)


# ---- 4) 动态视觉门禁（v0.12.0 Ghost Guard） -------------------------------
#
# 与 1)/2) 的静态黑名单互补：窗口标题/键入文本之外，屏幕上 OCR 读到的控件
# 标签同样可能代表不可逆操作（转账/删除/格式化/退出登录…）。本层只做纯
# 文本判定（不产生输入、不依赖窗口句柄），由：
#   - vl_driver.extract_ui_elements() 标注元素级 dangerous；
#   - controller 点击前对落点附近 OCR 文本做实时复核并 409 PENDING_APPROVAL。
# 中文按去空白后的子串匹配（OCR 常把「删除」读成「删 除」）；英文按词边界，
# 避免 "display" 误伤 "pay"、"information" 误伤 "format"。
#
# 刻意不把裸英文 "format" 列入：Paint/Word 的功能区「Format / 格式」是正常
# 排版操作，误杀会阻断重型实战；中文「格式化」与 "format c:" 仍拦截。
GUARD_RULES: tuple = (
    ("payment", ("转账", "付款", "支付", "汇款", "打款", "收款",
                 "transfer", "send money", "pay now", "pay", "payment")),
    ("deletion", ("删除", "移除", "格式化", "清空", "抹除", "销毁", "卸载",
                  "重置", "恢复出厂", "delete", "remove", "wipe",
                  "uninstall", "reset")),
    ("session", ("退出登录", "注销", "登出", "退出账号",
                 "logout", "log out", "sign out", "signout")),
    ("system", ("shutdown", "poweroff", "diskpart", "reg delete",
                "rm -rf", "format c")),
)

# 安全复合词：先剔除再匹配，避免「删除线」（排版功能）被「删除」误伤。
GUARD_SAFE_COMPOUNDS = ("删除线",)

_GUARD_VERSION = "v0.12"


def _compact(text: str) -> str:
    return "".join(str(text or "").split()).lower()


def _english_token_hit(token: str, lowered: str) -> bool:
    parts = [re.escape(p) for p in re.split(r"\s+", token.strip()) if p]
    if not parts:
        return False
    return re.search(r"\b" + r"\s+".join(parts) + r"\b", lowered) is not None


def dynamic_guard(texts: Iterable[Any]) -> Dict[str, Any]:
    """OCR 文本 → 动态门禁判定。

    texts 可以是字符串或含 text 字段的 dict（元素表直接传入）。
    返回 {"verdict": "allow"|"pending_approval", "requires_approval",
          "hits": [{"token","category","text"}], "categories": [...]}
    """
    if isinstance(texts, str):
        items: List[Any] = [texts]
    else:
        items = list(texts or [])
    hits: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in items:
        text = raw.get("text") if isinstance(raw, dict) else raw
        text = str(text or "").strip()
        if not text:
            continue
        compact = _compact(text)
        for safe in GUARD_SAFE_COMPOUNDS:
            compact = compact.replace(safe, "")
        lowered = " ".join(str(text).lower().split())
        for category, tokens in GUARD_RULES:
            for token in tokens:
                if any("\u4e00" <= ch <= "\u9fff" for ch in token):
                    matched = _compact(token) in compact
                else:
                    matched = _english_token_hit(token, lowered)
                if not matched:
                    continue
                key = (category, token, text)
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"token": token, "category": category,
                             "text": text, "version": _GUARD_VERSION})
    categories = sorted({h["category"] for h in hits})
    return {
        "verdict": "pending_approval" if hits else "allow",
        "requires_approval": bool(hits),
        "hits": hits,
        "categories": categories,
        "version": _GUARD_VERSION,
    }


def approval_configured() -> bool:
    """是否配置了人类审批令牌（只回布尔，绝不泄露令牌内容）。"""
    return bool(config.APPROVAL_TOKEN)


def approval_matches(token: Any) -> bool:
    """常量时间比较审批令牌；未配置/空值一律 False（永久 PENDING）。"""
    if not config.APPROVAL_TOKEN:
        return False
    return hmac.compare_digest(str(token or ""), config.APPROVAL_TOKEN)


# ---- 5) System1 弹窗规则库（v0.15 快思考） -------------------------------
#
# 与 1)-4) 的关系：前面都是"拒绝/审批"型门禁；System1 是"主动消解"型反射
# ——任务在挂起边界（require_vision / 遮挡失败）上，先本地判定"这是不是一个
# 可以自动关掉的弹窗"，是则由 vl_driver.system1_reflex 走 controller 安全
# 管道点击关闭。本层仍只做纯文本判定，不产生任何输入。
#
# 命中 = 特征词 AND 关闭按钮（同窗共现，缺一不判——宁可漏关，不可乱点）：
# - 特征词：窗口标题或任一 OCR 文本**包含**触发词（子串；英文大小写不敏感）；
# - 关闭按钮：按钮样式短文本（尺寸见 SYSTEM1_BTN_*）归一化后**精确等于**
#   关闭类词——正文句子里出现"关闭"不算按钮（防止点到正文中的词）。
SYSTEM1_TRIGGER_TOKENS = (
    # 用户钦定四词 + 同族扩展（更新/隐私/每日提示类弹窗）
    "软件更新", "软件升级", "发现新版本", "有新版本", "立即升级", "升级提醒",
    "广告", "隐私协议", "隐私政策", "每日提示", "每日一言",
    # 英文（_s1_contains 大小写不敏感）
    "software update", "software upgrade", "new version", "upgrade now",
    "privacy policy", "privacy agreement", "daily tip",
)

# 关闭类按钮：列表顺序 = 点击优先级。关闭/× 最安全排最前；"同意"垫底
# （同意=接受条款，只在没有任何关闭/推迟/取消按钮时才作为最后手段，
# 且点击后必须复检弹窗是否真的消失，否则按未消解回退挂起）。
SYSTEM1_DISMISS_BUTTONS = (
    "关闭", "关闭窗口", "×", "✕", "✖", "x",
    "close",
    "稍后", "稍后再说", "以后再说", "暂不", "稍后提醒", "later", "not now",
    "取消", "拒绝", "cancel", "decline",
    "我知道了", "知道了",
    "同意", "agree",
)

# 按钮样式启发式（OCR 元素 bbox 尺寸，物理像素）：过小不像按钮，
# 过大说明是文本块（正文里的"关闭"整句被读成一个元素时靠它排除）。
SYSTEM1_BTN_MIN_W, SYSTEM1_BTN_MAX_W = 10, 360
SYSTEM1_BTN_MIN_H, SYSTEM1_BTN_MAX_H = 8, 90

# 弹窗窗口特征：面积占所在屏比例上限（对话框体量；最大化浏览器/主窗口
# 即便含触发词也直接排除，防在大窗里乱点）。
SYSTEM1_MAX_WINDOW_AREA_RATIO = 0.45

# 窗口类排除：控制台宿主不是弹窗（测试/运维控制台输出里可能恰好同时出现
# 特征词与"关闭"字样——绝不允许反射脑去点控制台）。
SYSTEM1_EXCLUDED_WINDOW_CLASSES = frozenset({
    "ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS",
    "VirtualConsoleClass",
})


def _s1_compact(text: Any) -> str:
    """去所有空白（OCR 常把「关 闭」读成带空格）。"""
    return "".join(str(text or "").split())


def _s1_is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def _s1_contains(haystack: str, token: str) -> bool:
    """特征词包含判定：中文子串；英文大小写不敏感（两侧都去空白）。"""
    h, t = _s1_compact(haystack), _s1_compact(token)
    if not t:
        return False
    if _s1_is_ascii(t):
        return t.lower() in h.lower()
    return t in h


def _s1_button_hit(elem_text: str, token: str) -> bool:
    """按钮词精确判定：归一化后相等（英文忽略大小写）——子串不算。"""
    e, t = _s1_compact(elem_text), _s1_compact(token)
    if not e or not t:
        return False
    if _s1_is_ascii(t):
        return e.lower() == t.lower()
    return e == t


def system1_title_hit(title: str) -> Optional[str]:
    """窗口标题触发词预筛（零 OCR 成本）：命中返回特征词，否则 None。"""
    for tok in SYSTEM1_TRIGGER_TOKENS:
        if _s1_contains(str(title or ""), tok):
            return tok
    return None


def system1_match(elements: Iterable[Any],
                  title: Optional[str] = None) -> Dict[str, Any]:
    """弹窗规则判定（纯函数）：特征词 + 关闭按钮 同窗共现。

    elements: [{"text","bbox","center"}, ...]（OCR 元素表，物理屏幕坐标）；
    title: 窗口标题（win32 直读，不依赖 OCR，常含「软件更新」等触发词）。
    返回 {"matched","reason","trigger","buttons"}：
    - matched=True 时 buttons 按点击优先级排序，每项
      {"text","bbox","center","token","priority"}（center 为点击落点）；
    - reason: ok | no_trigger | no_button。
    """
    items = [e for e in (elements or [])
             if str((e or {}).get("text") or "").strip()]
    # 1) 特征词：标题优先（可靠、免 OCR），其次任一元素文本
    trigger: Optional[Dict[str, str]] = None
    for tok in SYSTEM1_TRIGGER_TOKENS:
        if _s1_contains(str(title or ""), tok):
            trigger = {"token": tok, "source": "title"}
            break
    if trigger is None:
        for e in items:
            for tok in SYSTEM1_TRIGGER_TOKENS:
                if _s1_contains(str(e.get("text") or ""), tok):
                    trigger = {"token": tok, "source": "text"}
                    break
            if trigger:
                break
    if trigger is None:
        return {"matched": False, "reason": "no_trigger",
                "trigger": None, "buttons": []}
    # 2) 关闭按钮：精确匹配 + 按钮尺寸启发式，按优先级排序
    found: List[Dict[str, Any]] = []
    for prio, tok in enumerate(SYSTEM1_DISMISS_BUTTONS):
        for e in items:
            bbox = e.get("bbox") or [0, 0, 0, 0]
            w, h = int(bbox[2]), int(bbox[3])
            if not (SYSTEM1_BTN_MIN_W <= w <= SYSTEM1_BTN_MAX_W
                    and SYSTEM1_BTN_MIN_H <= h <= SYSTEM1_BTN_MAX_H):
                continue
            if not _s1_button_hit(str(e.get("text") or ""), tok):
                continue
            center = e.get("center") or [int(bbox[0]) + w // 2,
                                         int(bbox[1]) + h // 2]
            found.append({"text": str(e.get("text") or "").strip(),
                          "bbox": [int(v) for v in bbox],
                          "center": [int(center[0]), int(center[1])],
                          "token": tok, "priority": prio})
    if not found:
        return {"matched": False, "reason": "no_button",
                "trigger": trigger, "buttons": []}
    found.sort(key=lambda b: b["priority"])  # 稳定排序：同优先级保持 OCR 顺序
    return {"matched": True, "reason": "ok", "trigger": trigger,
            "buttons": found}
