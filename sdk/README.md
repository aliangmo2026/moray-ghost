# moray-ghost Python SDK

所有 Agent 接入桌面操控底座的官方客户端。**依赖仅 `requests`**，单文件
（`sdk/ghost_client.py`），把 `sdk/` 目录拷进你的项目、或把项目根目录加进
`sys.path` 即可用。

```python
from sdk import GhostClient

client = GhostClient(agent_id="my-agent")   # token 自动解析：参数 > $GHOST_TOKEN > ./.env
```

## 能力一览

| 命名空间 | 方法 | 说明 |
|---|---|---|
| `client.health` | `.check()` | 探活（免 token）：版本 / 显示器 / 光标 |
| `client.environment` | `.check()` | 桌面体检：`input_allowed` / 锁屏 / UAC / 抓屏健康 |
| `client.action` | `.click(x, y)` `.double_click()` `.right_click()` `.move()` `.type(text, hwnd=)` `.hotkey("ctrl+s")` `.screenshot()` | 单步动作，服务端统一安全管道（串行锁 + Fail-Safe + 前后审计截图） |
| `client.task` | `.create()` `.start()` `.status()` `.resume()` `.cancel()` `.list()` `.wait()` `.run()` | 任务生命周期；`run()` = 建+启+自动轮询 |
| `client.system` | `.emergency_stop()` `.release()` | 紧急冻结 / 解冻 |

## 发任务

```python
view = client.task.run(
    steps=[
        {"kind": "action", "payload": {"action": "click", "x": 640, "y": 400}},
        {"kind": "wait", "ms": 500},
        {"kind": "macro", "macro_id": "m_abc", "hwnd": 12345},
    ],
    goal="打开文件菜单",
    timeout=120,               # 轮询上限；超时抛 TaskTimeoutError（任务仍在服务端跑）
)
print(view["status"])          # done
```

步骤类型：`action`（click/double_click/right_click/move/type/hotkey）、
`macro`（重放，需 `macro_id` + `hwnd`）、`wait`（0..60000ms，可被 cancel 打断）、
`require_vision`（挂起等主仓视觉处置）。

## degraded 挂起与处置

任务在失败步骤挂起（`degraded`）而不是烂掉——`run()` 默认抛
`TaskDegradedError`，异常对象带完整快照：

```python
from sdk import TaskDegradedError

try:
    client.task.run(steps=[...])
except TaskDegradedError as exc:
    print(exc.task["fail"])        # fail_class / step_id / detail / 截图路径
    # …主仓/Agent 做视觉处置后…
    client.task.resume(exc.task["task_id"])
    client.task.wait(exc.task["task_id"])
```

## 多 Agent 并发

- 构造时传 `agent_id`（或环境变量 `GHOST_AGENT_ID`）→ 所有请求自动带
  `X-Agent-Id` 头。
- 多个 Agent 同时发任务：Ghost 内部按 **FIFO 队列串行** 执行（一个任务上桌面时
  其他排队），`status()` 里返回 `queue_position` 排位。
- **状态隔离**：带 `agent_id` 的客户端只能看到/操作自己的任务；跨 Agent 访问抛
  `GhostForbiddenError`（403）。不带 `agent_id` = 本机运维方，全量可见。

## 视觉感知 / GUI RAG（v0.12）

```python
# 1) 带编号的元素表（OCR+CV+win32；bbox 全部来自像素/句柄）
els = client.vl.elements(window="无标题 - 记事本")
# {"elements":[{"id":2,"text":"用户名","type":"input","bbox":[...],...}], ...}

# 2) VLM 只选号：target_id → 真实 bbox（模型不生成坐标）
hit = client.vl.ground("保存成功", window="无标题 - 记事本", strategy="auto")
# {"method":"text_exact"|"vlm_text", "target_id":9, "element":{...}}

# 3) 自由读图（v0.9.1）
print(client.vl.describe("这张图里画了什么？").get("description"))

# 4) 旧版通用控件表 / 定向 grounding
client.vl.analyze(window="无标题 - 记事本")
client.vl.analyze(window="无标题 - 记事本", target="保存成功")
```

危险目标（转账/删除/格式化/退出登录…）的点击会被服务端 Ghost Guard 拦截，
`action.click` / 任务步骤返回 `PENDING_APPROVAL`（409）；审批令牌只应放在
人工控制台环境，SDK/Agent 侧不要配置。

## 异常体系

```
GhostError                 # 基类：status / error / detail / data
├─ GhostAuthError          # 401 token 缺失或错误
├─ GhostForbiddenError     # 403 跨 Agent 访问
├─ GhostRedlineError       # 403 窗口/键盘红线或能力令牌拒绝
├─ GhostApprovalRequiredError  # 409 PENDING_APPROVAL（动态高危词，动作未执行；.hits）
├─ GhostFrozenError        # 503 emergency stop 冻结中（system.release() 解冻）
├─ SystemNotReadableError  # 503 锁屏/安全桌面/抓屏失败，勿盲操作
└─ TaskDegradedError       # 任务挂起（.task 快照）
TaskTimeoutError           # 轮询超时（任务仍在服务端，可继续 wait）
GhostConnectionError       # 连不上（status=0）
```

## 紧急停止

```python
client.system.emergency_stop()   # 清空队列 + 冻结：所有输入接口立即 503
client.system.release()          # 解冻
```

浏览器打开 `http://127.0.0.1:8030/dashboard` 有同款大按钮。

## 快速验证

```powershell
.venv\Scripts\python sdk_example.py
```
