# moray-ghost v0.17.0 Release Notes

**发布日期**：2026-11-06
**主题**：并发调度与虚拟输入通道——从「全局串行单任务」到「多窗口集群 Agent 底座」

---

## 1. 一句话总结

v0.17 把桌面动作的全局串行锁拆成**按 hwnd 的细粒度窗口锁**，任务队列升级为
**默认 4 工作线程的并发池**，并新增 `channel="virtual"` 的 **PostMessage 后台输入**
（不移动物理鼠标、不抢前台），投递后用 **128px 像素/WM_GETTEXT 可观测性校验**，
无效果时**自动降级 physical 并落审计**；physical 默认路径零改动。

---

## 2. 四项交付

### 2.1 细粒度窗口锁（`app/core/action_locks.py`）

- key = `("hwnd", <root hwnd>)`，同一窗口的动作严格串行，不同窗口的动作真正并行；
  子控件 hwnd 统一归一到 `GA_ROOT`，避免「子窗/顶层各拿一把锁」。
- 引用计数注册表：无持有者/等待者立即回收，长时挂机不累积。
- `controller.ACTION_LOCK` 保留为**物理输入锁**：physical/CDP 仍持它（全系统
  只有一套鼠标键盘）；virtual 通道完全不碰。
- `/health` 新增 `action_locks`：`active_now` / `max_concurrent` / `timeouts_total`
  / `held_by_hwnd` / `held_global`。

### 2.2 任务工作池（`app/core/task_queue.py`）

- `GHOST_MAX_CONCURRENT_TASKS`（默认 **4**）个工作线程；队列仍 FIFO 取任务。
- `freeze()` 取消**所有**运行中任务（不再只有一个 `_running_task`）；
  `_take_next` 与 freeze 在同一把 `_qlock` 内完成 queued→running→登记，
  不可能漏掉「已出队未登记」的窗口期。
- `running_view()` 向后兼容（取第一个），新增 `running_views()` / `running_count()`。
- **兼容开关**：`GHOST_MAX_CONCURRENT_TASKS=1` 完全恢复 v0.16 的严格 FIFO 串行；
  v0.5 平台套件在该模式下 33/33。

### 2.3 virtual 虚拟输入通道（`app/core/virtual_input.py`）

`POST /api/ghost/execute_action` 新增：

```json
{"action":"type","text":"后台打字","hwnd":123456,"channel":"virtual",
 "capabilities":{"allowed_windows":["notepad.exe"]}}
```

- `channel`：`physical`（默认）| `virtual`。
- `virtual` 支持：click / double_click / right_click / type / drag / scroll。
  - click 系：`WM_MOUSEMOVE → WM_LBUTTONDOWN → WM_LBUTTONUP`
    （双击加 `WM_LBUTTONDBLCLK`；右键 `WM_RBUTTON*`）。
  - type：`WM_CHAR`（中文/emoji 按 UTF-16 代理对逐单元投递），目标取线程焦点
    文本控件 > 面积最大的 RichEdit/Edit/TextBox > 根窗口。
  - drag：`WM_LBUTTONDOWN → WM_MOUSEMOVE(MK_LBUTTON) → WM_LBUTTONUP`，
    全程投给同一个最深层子控件（画布语义）。
  - scroll：`WM_MOUSEWHEEL`（lParam 为屏幕坐标）。
- **不移动物理鼠标、不抢前台、不碰剪贴板**；`type` 不再要求 x/y，也不再要求
  目标在前台（消息直接投给 hwnd）。
- virtual 必须有 `hwnd`；不支持 `move` / `hotkey` → 400 `VIRTUAL_UNSUPPORTED`。
- 安全门禁一视同仁：UAC FATAL / 窗口黑名单 / 能力令牌 / Ghost Guard /
  Fail-Safe 角 / 硬超时 / 频率熔断 / 审计截图。

### 2.4 可观测性校验 + 自动降级

- 投递后默认校验（`verify=true`）：
  - type 优先 `WM_GETTEXT` 文本比对；
  - 其他动作抓 128px 客户区（`PrintWindow(PW_RENDERFULLCONTENT)`，失败退
    窗口 DC BitBlt）做灰度像素比对；
  - 基线先等画面稳定（`stable_signature`），避免上一笔延迟渲染被误判成本次成功。
- 无可观测变化：
  - `virtual_fallback=true`（默认）→ 自动走 physical；响应
    `channel:"physical"`、`fallback_from_virtual:true`、
    `physical_pointer_used:true`，写 `virtual_fallback` 审计事件；
  - `virtual_fallback=false` → **409 `VIRTUAL_INPUT_UNVERIFIED`**，零物理输入，
    绝不假装成功。
- 响应 `virtual` 字段：`mode`（`postmessage` / `synthetic_pointer`）、
  `messages_posted`、`verify`（`verified`/`method`/`changed_pixels`/`attempts`）。

---

## 3. 本机实弹结论（诚实边界）

| 目标 | virtual 结果 | 说明 |
|---|---|---|
| Win11 记事本（RichEditD2DPT） | ✅ 成功 | WM_CHAR 直达；WM_GETTEXT 回读验证；物理鼠标零位移 |
| 经典 Win32 画布（WM_* 窗口过程） | ✅ 成功 | PostMessage 拖拽真实落墨；128px 像素校验通过 |
| Win11 新版画图（WinUI 内容岛） | ⚠️ 降级 | 忽略 PostMessage/SendMessage/WM_POINTER；409 或 physical 降级（审计可见） |
| 合成笔 `InjectSyntheticPointerInput` | 🧪 实验 | 本机 4 笔仅 1 笔落墨；`GHOST_VIRTUAL_POINTER=false` 默认关闭 |

结论：**「绝不抢占物理鼠标」在传统 Win32 文本/画布上已可用；WinUI 目标必须
走审计降级**。合成笔不拿偶尔成功冒充 100%，默认关闭。

---

## 4. 验收账本

| 套件 | 结果 |
|---|---|
| `scripts/test_v17_concurrent_virtual.py` | **42/42** |
| `scripts/test_v16_industrial_draw.py` | **65/65** |
| `scripts/test_v15_platforms.py` | **21/21** |
| `scripts/test_v15_system1.py` | **47/47** |
| `scripts/test_v15_skills.py` | **28/28** |
| `scripts/test_v14_dpi.py` | **20/20** |
| `scripts/test_v12_gui_rag.py` | **44/44** |
| `scripts/test_v072_extreme.py` | **40/40** |
| `scripts/test_v071_security.py` | **37/37** |
| `scripts/test_v07_redline.py` | **40/40** |
| `scripts/test_v06_actionlog.py` | **9/9** |
| `scripts/test_v050_platform.py`（`GHOST_MAX_CONCURRENT_TASKS=1`） | **33/33** |
| `scripts/test_breaker_r3.py`（默认 4 workers，5 任务并发） | **16/16** |

v0.17 套件 D 段：线程 A 记事本 virtual 打字 ×4 + 线程 B 经典画布 virtual 落墨 ×4，
同时物理鼠标沿屏幕底部移动 **141 次采样，0 次偏离（≤4px）**；服务端
`action_locks.max_concurrent=2`，结束后 `held_by_hwnd={}` 无泄漏。

### 4.1 虚拟通道与并发实测明细（2026-11-06，Windows 11 26200 / 2560×1600）

| 场景 | 实测数据 |
|---|---|
| 记事本 virtual type | 20 字符 → **20 条 WM_CHAR**；WM_GETTEXT **第 1 次轮询命中**（`attempts=1`, `text_before_len=0`, `text_after_len=20`）；物理光标 0 位移 |
| 经典 Win32 画布 virtual drag | 30 点 → **29 段笔迹 + down/up 2 次**；128px 像素校验通过；物理光标 0 位移 |
| Win11 画图 virtual drag（`fallback=false`） | **409 `VIRTUAL_INPUT_UNVERIFIED`**；`changed_pixels=0`；物理光标 0 位移 |
| Win11 画图 virtual drag（默认 fallback） | `channel=physical`、`fallback_from_virtual=true`、`physical_pointer_used=true`；真实落墨；`virtual_fallback` 审计事件落盘 |
| 并发 4 打字 + 4 落墨 | **4.33s** 完成；画布新增 **116 段**；物理鼠标 **141 采样 0 偏离（≤4px）**；`max_concurrent=2`；结束后 `held_by_hwnd={}` |
| 5 任务并发（breaker r3，默认 4 workers） | **4 个同时 running，第 5 个 queued**；**9.0s 全部 done**；状态机无非法组合、无 `INTERNAL_ERROR` |
| v0.16 FIFO 兼容 | `GHOST_MAX_CONCURRENT_TASKS=1` → **v0.5 平台套件 33/33** |
| 合成笔实验通道 | Win11 画图 4 笔仅 1 笔落墨 → `GHOST_VIRTUAL_POINTER=false` 默认关闭 |


---

## 5. 兼容与迁移

- **physical 默认零变化**：不传 `channel` 的旧脚本/宏/任务/SDK 调用行为与
  v0.16 完全一致。
- **任务 FIFO 语义变化**：默认 4 workers 后，不同任务可并发；同一窗口的动作
  仍由窗口锁串行。需要 v0.16 严格串行的部署设
  `GHOST_MAX_CONCURRENT_TASKS=1`。
- **virtual 最小权限**：仍需 `capabilities.allowed_windows` 且必须给 `hwnd`；
  `type` 可省略 x/y。
- **SDK**：`client.action.click(..., channel="virtual", hwnd=...)` /
  `client.action.type(text, hwnd=..., channel="virtual")` 通过 `**kw` 直接可用。
- **实验开关**：`GHOST_VIRTUAL_POINTER=true` 才启用合成笔层；默认关闭。

---

## 6. 已知限制

- Win11 新版画图/部分 Electron/WinUI 控件不接收合成鼠标消息——这是应用层
  事实，不是锁或通道缺陷；用 fallback 或换经典 Win32 目标。
- `PrintWindow` 对挂死窗口没有超时；极端情况下校验可能拖到动作硬超时，
  由现有 408 + 冻结兜底。可对这类目标传 `verify=false`。
- physical 降级会移动真实鼠标并可能把目标带到前台（审计事件
  `virtual_fallback` 明确记录），这是「保证成功」的代价。
- 合成笔层会短暂激活目标窗口；默认关闭，不纳入 SLA。
- **宏录制/重放仍固定 physical**：录制步骤不写 `channel`，宏重放走原有的
  前台门禁与物理输入；`channel="virtual"` 目前适用于直接
  `/execute_action` 和任务里的 `kind="action"` 步骤（任务 payload 原样透传）。
  需要虚拟宏回放时应在主仓侧用任务 action 步骤编排。

---

## 7. 文件清单

新增：
- `app/core/action_locks.py`
- `app/core/virtual_input.py`
- `scripts/test_v17_concurrent_virtual.py`
- `docs/RELEASE_v0.17.0.md`

修改：
- `app/core/controller.py`（channel 路由 / 窗口锁 / virtual 执行 / 降级审计）
- `app/core/task_queue.py`（工作池 + 多运行任务冻结）
- `app/core/config.py`（v0.17 开关 + VERSION 0.17.0）
- `app/api/routes.py`（ExecuteRequest channel/virtual_fallback/verify + health 观测）
- `app/api/dashboard_routes.py`（running_tasks / running_count）
- `app/core/backend.py`（virtual_input 再导出）
- `app/core/icon_grid.py`（去掉盘符绝对路径，E-1 开源标准）
- `sdk/__init__.py`（__version__ 0.17.0）
- `README.md`（v0.17 changelog / API / health 示例）
- `scripts/test_v16_industrial_draw.py` / `scripts/test_v12_gui_rag.py` /
  `scripts/test_v071_security.py`（回归夹具与过期断言修正）
